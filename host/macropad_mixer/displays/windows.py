"""Windows display brightness: external monitors via DDC/CI, laptop panels via WMI.

Design notes
------------
* **External monitors (DDC/CI)** use the Monitor Configuration API in dxva2.dll:
  ``EnumDisplayMonitors`` -> per HMONITOR ``GetNumberOfPhysicalMonitorsFromHMONITOR`` +
  ``GetPhysicalMonitorsFromHMONITOR`` -> ``GetMonitorBrightness`` / ``SetMonitorBrightness``
  -> ``DestroyPhysicalMonitors``. The monitor's min..max range is scaled to 0-100. A monitor
  whose ``GetMonitorBrightness`` fails doesn't speak DDC/CI and is not listed.
  Ids are ``ddc:<GDI device>:<index>`` (``ddc:DISPLAY2:0``); names come from
  ``EnumDisplayDevicesW`` (DeviceString) or the PHYSICAL_MONITOR description, falling back to
  ``Display N`` when both are the useless "Generic PnP Monitor".
* **Built-in laptop panel (WMI)**: ``root\\WMI`` ``WmiMonitorBrightness.CurrentBrightness`` to
  read, ``WmiMonitorBrightnessMethods.WmiSetBrightness(Timeout, Brightness)`` to write. Done
  through comtypes (late-bound ``winmgmts:`` scripting objects) when it imports, else -- or
  after the first COM failure -- by running PowerShell's ``Get-CimInstance`` /
  ``Invoke-CimMethod``. Id ``"builtin"``, name ``"Built-in"``. Desktops have no instance, so
  nothing is listed. If the panel also answers DDC (rare), the DDC entry with the same PnP
  hardware id (``BOE0123``) is dropped in favour of the WMI one.
* **Coalescing**: ``set_brightness`` only records the value; one worker thread writes the
  latest value per display, at most every ``DDC_WRITE_INTERVAL`` seconds per DDC display
  (monitors choke on faster writes, and a write itself can take 50+ ms).
* **Caching** (``list_displays`` is polled every ~2 s): physical-monitor handles are kept
  and the monitor list (and their DDC values) refreshed at most every ``REFRESH_INTERVAL``
  seconds, destroying the old handles. In between we report the last value read or set. A
  re-read shortly after our own write keeps the value we set (the monitor may lag).
* Public methods never raise for runtime failures: they log and skip the display.

Everything Windows-specific (ctypes.WinDLL, wintypes structs, comtypes) is created inside
functions, so this module -- and the pure helpers at the top, which the tests exercise --
import fine on macOS/Linux.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .base import DisplayInfo

log = logging.getLogger(__name__)

BUILTIN_ID = "builtin"
BUILTIN_NAME = "Built-in"
MAX_NAME_LEN = 20

REFRESH_INTERVAL = 10.0        # re-enumerate monitors / re-read DDC values at most this often
BUILTIN_ABSENT_RECHECK = 60.0  # re-probe WMI this often when there was no built-in panel
DDC_WRITE_INTERVAL = 0.1       # min seconds between DDC writes to one monitor
WMI_WRITE_INTERVAL = 0.05
SET_GRACE = 3.0                # after a set, trust our value over a re-read for this long
POWERSHELL_TIMEOUT = 10.0
CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW only exists on Windows

GENERIC_MONITOR_NAMES = {
    "generic pnp monitor",
    "generic non-pnp monitor",
    "default monitor",
    "plug and play monitor",
}


# --------------------------------------------------------------------------- #
# Pure helpers (no Windows imports; unit-tested on any OS)
# --------------------------------------------------------------------------- #


def clamp_percent(value: Any) -> int:
    return max(0, min(100, int(round(float(value)))))


def scale_to_percent(cur: int, lo: int, hi: int) -> int:
    """Raw DDC value in lo..hi -> 0-100."""
    if hi <= lo:
        return clamp_percent(cur)
    return clamp_percent((cur - lo) * 100 / (hi - lo))


def percent_to_raw(pct: int, lo: int, hi: int) -> int:
    """0-100 -> raw DDC value in lo..hi."""
    pct = clamp_percent(pct)
    if hi <= lo:
        return lo if hi < lo else hi
    return lo + int(round(pct * (hi - lo) / 100))


def short_device_name(device: str) -> str:
    r"""``\\.\DISPLAY2`` -> ``DISPLAY2``."""
    d = (device or "").strip()
    if d.startswith("\\\\.\\"):
        d = d[4:]
    return d


def ddc_display_id(device: str, index: int) -> str:
    return f"ddc:{short_device_name(device) or '?'}:{index}"


def display_number(device: str) -> int | None:
    r"""``\\.\DISPLAY2`` -> 2."""
    m = re.search(r"(\d+)$", short_device_name(device))
    return int(m.group(1)) if m else None


def is_generic_monitor_name(name: str | None) -> bool:
    if not name or not name.strip():
        return True
    return name.strip().lower() in GENERIC_MONITOR_NAMES


def choose_monitor_name(friendly: str | None, description: str | None, fallback_number: int) -> str:
    """First non-generic of EnumDisplayDevices DeviceString / PHYSICAL_MONITOR description,
    else ``Display N``; truncated for the OLED."""
    for cand in (friendly, description):
        if not is_generic_monitor_name(cand):
            return cand.strip()[:MAX_NAME_LEN]  # type: ignore[union-attr]
    return f"Display {fallback_number}"[:MAX_NAME_LEN]


def hardware_id(instance: str | None) -> str | None:
    r"""PnP hardware id from a device instance path, upper-cased.

    ``DISPLAY\BOE0867\4&2f1d3f05&0&UID8388688_0`` (WMI InstanceName) and
    ``MONITOR\BOE0867\{4d36e96e-...}\0001`` (EnumDisplayDevices DeviceID) -> ``BOE0867``.
    """
    if not instance:
        return None
    parts = [p for p in instance.replace("/", "\\").split("\\") if p]
    if len(parts) < 2 or parts[0].upper() not in ("DISPLAY", "MONITOR"):
        return None
    return parts[1].upper() or None


def parse_wmi_brightness_output(text: str) -> list[tuple[str, int]]:
    """Parse ``InstanceName|CurrentBrightness`` lines (our PowerShell read command)."""
    out: list[tuple[str, int]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        inst, sep, val = line.rpartition("|")
        if not sep:
            inst, val = "", line
        try:
            out.append((inst.strip(), clamp_percent(int(val.strip()))))
        except ValueError:
            continue
    return out


POWERSHELL_EXE = "powershell"


def powershell_read_command() -> list[str]:
    script = (
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "-ErrorAction SilentlyContinue | "
        'ForEach-Object { "$($_.InstanceName)|$($_.CurrentBrightness)" }'
    )
    return [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script]


def powershell_write_command(value: int) -> list[str]:
    v = clamp_percent(value)
    script = (
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
        "ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout=1; Brightness=[byte]{v}}} }} | Out-Null"
    )
    return [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script]


def reconcile(read_value: int | None, last_set: tuple[int, float] | None, pending: bool,
              now: float, grace: float = SET_GRACE) -> int | None:
    """Value to report after a re-read: our own value if a write is pending or was made within
    ``grace`` seconds (the display may not reflect it yet), else what we read."""
    if last_set is not None and (pending or now - last_set[1] < grace):
        return last_set[0]
    return read_value


@dataclass
class DdcEntry:
    """One DDC/CI-capable physical monitor."""

    id: str
    name: str
    handle: Any = None
    hw_id: str | None = None
    lo: int = 0
    hi: int = 100
    value: int | None = None  # 0-100


def merge_displays(builtin: DisplayInfo | None, builtin_hw_ids: set[str],
                   ddc: list[DdcEntry]) -> list[DisplayInfo]:
    """Built-in first, then DDC monitors; a DDC monitor that is the built-in panel is dropped."""
    out: list[DisplayInfo] = []
    if builtin is not None:
        out.append(builtin)
    seen = {d.id for d in out}
    for e in ddc:
        if builtin is not None and e.hw_id and e.hw_id in builtin_hw_ids:
            continue
        if e.id in seen:
            continue
        seen.add(e.id)
        out.append(DisplayInfo(id=e.id, name=e.name, brightness=e.value))
    return out


class Coalescer:
    """One worker thread; the latest value per key wins; per-key minimum write interval.

    ``write(key, value)`` runs on the worker thread; exceptions from it are logged. The
    interval is measured from the end of one write to the start of the next for that key.
    """

    def __init__(self, write: Callable[[str, int], Any],
                 interval_for: Callable[[str], float] = lambda _k: DDC_WRITE_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 on_thread_start: Callable[[], Any] | None = None) -> None:
        self._write = write
        self._interval_for = interval_for
        self._clock = clock
        self._on_thread_start = on_thread_start
        self._cond = threading.Condition()
        self._pending: dict[str, int] = {}
        self._last_write: dict[str, float] = {}
        self._closed = False
        self._thread: threading.Thread | None = None

    def submit(self, key: str, value: int) -> None:
        with self._cond:
            if self._closed:
                return
            self._pending[key] = value
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="display-brightness",
                                                daemon=True)
                self._thread.start()
            self._cond.notify()

    def is_pending(self, key: str) -> bool:
        with self._cond:
            return key in self._pending

    def take_ready(self, now: float) -> tuple[list[tuple[str, int]], float | None]:
        """Pop pending writes whose interval has elapsed. Returns (ready, seconds until the
        next one becomes ready or None). Caller holds the lock (or is a test)."""
        ready: list[tuple[str, int]] = []
        wait: float | None = None
        for key in list(self._pending):
            last = self._last_write.get(key)
            interval = self._interval_for(key)
            remaining = 0.0 if last is None else interval - (now - last)
            if remaining <= 1e-6:  # float slack; cond.wait may also wake a hair early
                ready.append((key, self._pending.pop(key)))
            else:
                wait = remaining if wait is None else min(wait, remaining)
        return ready, wait

    def _run(self) -> None:
        if self._on_thread_start is not None:
            try:
                self._on_thread_start()
            except Exception:
                log.debug("display worker init failed", exc_info=True)
        while True:
            with self._cond:
                while True:
                    if self._closed:
                        return
                    ready, wait = self.take_ready(self._clock())
                    if ready:
                        break
                    self._cond.wait(wait)
            for key, value in ready:
                try:
                    self._write(key, value)
                except Exception:
                    log.warning("brightness write to %s failed", key, exc_info=True)
                with self._cond:
                    self._last_write[key] = self._clock()

    def close(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._closed = True
            self._pending.clear()
            self._cond.notify_all()
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)


# --------------------------------------------------------------------------- #
# Windows-only: DDC/CI via dxva2 (ctypes, created lazily)
# --------------------------------------------------------------------------- #


def _physical_monitor_struct():
    """PHYSICAL_MONITOR (PhysicalMonitorEnumerationAPI.h, declared under #pragma pack(1)):
    { HANDLE hPhysicalMonitor; WCHAR szPhysicalMonitorDescription[128]; }"""
    import ctypes
    from ctypes import wintypes

    class PHYSICAL_MONITOR(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("hPhysicalMonitor", wintypes.HANDLE),
                    ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]

    return PHYSICAL_MONITOR


@dataclass
class _PhysGroup:
    """The physical monitors of one HMONITOR; destroyed together."""

    device: str
    count: int
    array: Any  # PHYSICAL_MONITOR * count
    entries: list[DdcEntry] = field(default_factory=list)


class _Dxva:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        self.PHYSICAL_MONITOR = PM = _physical_monitor_struct()
        LPPM = ctypes.POINTER(PM)

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]  # CCHDEVICENAME

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                        ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                        ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

        self.MONITORINFOEXW = MONITORINFOEXW
        self.DISPLAY_DEVICEW = DISPLAY_DEVICEW
        # BOOL CALLBACK MonitorEnumProc(HMONITOR, HDC, LPRECT, LPARAM)
        self.MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        d = self.dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

        u.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                          self.MONITORENUMPROC, wintypes.LPARAM]
        u.EnumDisplayMonitors.restype = wintypes.BOOL
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD]
        u.EnumDisplayDevicesW.restype = wintypes.BOOL

        d.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR,
                                                              ctypes.POINTER(wintypes.DWORD)]
        d.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
        d.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, LPPM]
        d.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
        d.GetMonitorBrightness.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(wintypes.DWORD)]
        d.GetMonitorBrightness.restype = wintypes.BOOL
        d.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        d.SetMonitorBrightness.restype = wintypes.BOOL
        d.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, LPPM]
        d.DestroyPhysicalMonitors.restype = wintypes.BOOL

    def hmonitors(self) -> list[int]:
        found: list[int] = []

        def cb(hmon, _hdc, _rect, _lparam):
            if hmon:
                found.append(hmon)
            return True

        proc = self.MONITORENUMPROC(cb)  # keep a reference for the duration of the call
        if not self.user32.EnumDisplayMonitors(None, None, proc, 0):
            log.debug("EnumDisplayMonitors failed: %d", self._ct.get_last_error())
        return found

    def device_name(self, hmon: int) -> str:
        info = self.MONITORINFOEXW()
        info.cbSize = self._ct.sizeof(self.MONITORINFOEXW)
        if not self.user32.GetMonitorInfoW(hmon, self._ct.byref(info)):
            return ""
        return info.szDevice

    def monitor_devices(self, device: str) -> list[tuple[str, str]]:
        """(DeviceString, DeviceID) of the monitors attached to a GDI device, in order."""
        out: list[tuple[str, str]] = []
        if not device:
            return out
        for i in range(8):
            dd = self.DISPLAY_DEVICEW()
            dd.cb = self._ct.sizeof(self.DISPLAY_DEVICEW)
            if not self.user32.EnumDisplayDevicesW(device, i, self._ct.byref(dd), 0):
                break
            out.append((dd.DeviceString, dd.DeviceID))
        return out

    def physical_monitors(self, hmon: int) -> tuple[int, Any]:
        n = self._wt.DWORD(0)
        if not self.dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, self._ct.byref(n)) \
                or n.value == 0:
            return 0, None
        arr = (self.PHYSICAL_MONITOR * n.value)()
        if not self.dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n.value, arr):
            return 0, None
        return n.value, arr

    def get_brightness(self, handle: Any) -> tuple[int, int, int] | None:
        lo, cur, hi = self._wt.DWORD(), self._wt.DWORD(), self._wt.DWORD()
        for _attempt in range(2):  # DDC reads fail spuriously now and then
            if self.dxva2.GetMonitorBrightness(handle, self._ct.byref(lo), self._ct.byref(cur),
                                               self._ct.byref(hi)):
                return lo.value, cur.value, hi.value
        return None

    def set_brightness(self, handle: Any, raw: int) -> bool:
        return bool(self.dxva2.SetMonitorBrightness(handle, raw))

    def destroy(self, group: _PhysGroup) -> None:
        if group.array is not None and group.count:
            if not self.dxva2.DestroyPhysicalMonitors(group.count, group.array):
                log.debug("DestroyPhysicalMonitors(%s) failed", group.device)
            group.array = None

    def enumerate(self) -> list[_PhysGroup]:
        """Every physical monitor, with DDC-capable ones (brightness readable) as entries."""
        groups: list[_PhysGroup] = []
        ordinal = 0
        for hmon in self.hmonitors():
            try:
                device = self.device_name(hmon)
                count, arr = self.physical_monitors(hmon)
            except Exception:
                log.debug("skipping HMONITOR %r", hmon, exc_info=True)
                continue
            if not count:
                continue
            group = _PhysGroup(device=device, count=count, array=arr)
            groups.append(group)
            try:
                attached = self.monitor_devices(device)
            except Exception:
                attached = []
            for i in range(count):
                ordinal += 1
                try:
                    pm = arr[i]
                    reading = self.get_brightness(pm.hPhysicalMonitor)
                    if reading is None:
                        continue  # no DDC/CI (or brightness unsupported)
                    lo, cur, hi = reading
                    friendly, dev_id = attached[i] if i < len(attached) else ("", "")
                    number = display_number(device) or ordinal
                    if i > 0:
                        number = ordinal
                    group.entries.append(DdcEntry(
                        id=ddc_display_id(device, i),
                        name=choose_monitor_name(friendly, pm.szPhysicalMonitorDescription,
                                                 number),
                        handle=pm.hPhysicalMonitor,
                        hw_id=hardware_id(dev_id),
                        lo=lo, hi=hi,
                        value=scale_to_percent(cur, lo, hi),
                    ))
                except Exception:
                    log.debug("skipping physical monitor %d of %s", i, device, exc_info=True)
        return groups


# --------------------------------------------------------------------------- #
# Windows-only: built-in panel via WMI
# --------------------------------------------------------------------------- #


class _Wmi:
    """WmiMonitorBrightness via comtypes (late-bound winmgmts:) or PowerShell."""

    def __init__(self, use_com: bool = True) -> None:
        self._tls = threading.local()
        self._use_com = use_com
        if use_com:
            try:
                import comtypes  # noqa: F401  # type: ignore[import-not-found]
                import comtypes.client  # noqa: F401  # type: ignore[import-not-found]
            except Exception:
                log.debug("comtypes unavailable; using PowerShell for WMI brightness")
                self._use_com = False

    # ---- COM ---------------------------------------------------------------

    def _services(self) -> Any:
        svc = getattr(self._tls, "svc", None)
        if svc is None:
            import comtypes  # type: ignore[import-not-found]
            import comtypes.automation  # type: ignore[import-not-found]
            import comtypes.client.dynamic  # type: ignore[import-not-found]

            if not getattr(self._tls, "com_ready", False):
                try:
                    comtypes.CoInitialize()
                except OSError as e:  # RPC_E_CHANGED_MODE: already initialised; usable
                    log.debug("CoInitialize: %s", e)
                self._tls.com_ready = True
            punk = comtypes.CoGetObject("winmgmts:root\\WMI", comtypes.automation.IDispatch)
            svc = comtypes.client.dynamic.Dispatch(punk)
            self._tls.svc = svc
        return svc

    def _query(self, wql: str) -> list[Any]:
        objs = self._services().ExecQuery(wql)
        return [objs.ItemIndex(i) for i in range(int(objs.Count))]

    def _com_read(self) -> list[tuple[str, int]]:
        out = []
        for obj in self._query("SELECT InstanceName, CurrentBrightness, Active "
                               "FROM WmiMonitorBrightness"):
            props = obj.Properties_
            if props.Item("Active").Value is False:
                continue
            out.append((str(props.Item("InstanceName").Value or ""),
                        clamp_percent(props.Item("CurrentBrightness").Value)))
        return out

    def _com_write(self, value: int) -> None:
        objs = self._query("SELECT * FROM WmiMonitorBrightnessMethods")
        if not objs:
            raise RuntimeError("no WmiMonitorBrightnessMethods instance")
        for obj in objs:
            params = obj.Methods_.Item("WmiSetBrightness").InParameters.SpawnInstance_()
            params.Properties_.Item("Timeout").Value = 1
            params.Properties_.Item("Brightness").Value = clamp_percent(value)
            obj.ExecMethod_("WmiSetBrightness", params)

    def _com_failed(self) -> None:
        log.info("WMI brightness via COM failed; falling back to PowerShell", exc_info=True)
        self._use_com = False

    # ---- PowerShell --------------------------------------------------------

    @staticmethod
    def _powershell(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=POWERSHELL_TIMEOUT,
                              creationflags=CREATE_NO_WINDOW)

    # ---- public ------------------------------------------------------------

    def read(self) -> list[tuple[str, int]]:
        """[(InstanceName, 0-100)] of built-in panels; [] on desktops or on failure."""
        if self._use_com:
            try:
                return self._com_read()
            except Exception:
                self._com_failed()
        try:
            res = self._powershell(powershell_read_command())
        except Exception:
            log.debug("PowerShell WMI read failed", exc_info=True)
            return []
        return parse_wmi_brightness_output(res.stdout)

    def write(self, value: int) -> None:
        if self._use_com:
            try:
                self._com_write(value)
                return
            except Exception:
                self._com_failed()
        res = self._powershell(powershell_write_command(value))
        if res.returncode != 0:
            log.warning("WmiSetBrightness failed: %s", (res.stderr or "").strip()[:200])

    def init_thread(self) -> None:
        if self._use_com:
            try:
                self._services()
            except Exception:
                log.debug("COM init on display worker failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #


class WindowsDisplays:
    """DisplayBackend for Windows: DDC/CI external monitors + WMI built-in panel."""

    def __init__(self, *, ddc: bool = True, wmi: bool = True, use_com: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()  # guards handles (refresh vs. worker writes) and caches
        self._closed = False

        self._dxva: _Dxva | None = None
        if ddc:
            try:
                self._dxva = _Dxva()
            except Exception:
                log.warning("DDC/CI brightness unavailable", exc_info=True)
        self._groups: list[_PhysGroup] = []
        self._ddc: dict[str, DdcEntry] = {}
        self._ddc_refreshed: float | None = None

        self._wmi: _Wmi | None = None
        if wmi:
            try:
                self._wmi = _Wmi(use_com=use_com)
            except Exception:
                log.warning("WMI brightness unavailable", exc_info=True)
        self._builtin: DisplayInfo | None = None
        self._builtin_hw_ids: set[str] = set()
        self._builtin_checked: float | None = None

        self._last_set: dict[str, tuple[int, float]] = {}
        self._writer = Coalescer(self._write, self._interval_for, clock=clock,
                                 on_thread_start=self._init_worker)

    # ---- worker ------------------------------------------------------------

    def _init_worker(self) -> None:
        if self._wmi is not None:
            self._wmi.init_thread()

    @staticmethod
    def _interval_for(display_id: str) -> float:
        return WMI_WRITE_INTERVAL if display_id == BUILTIN_ID else DDC_WRITE_INTERVAL

    def _write(self, display_id: str, value: int) -> None:
        if display_id == BUILTIN_ID:
            if self._wmi is not None:
                self._wmi.write(value)
            return
        with self._lock:
            entry = self._ddc.get(display_id)
            if entry is None or self._dxva is None or entry.handle is None:
                log.debug("brightness write to unknown display %s dropped", display_id)
                return
            if not self._dxva.set_brightness(entry.handle, percent_to_raw(value, entry.lo,
                                                                           entry.hi)):
                log.warning("SetMonitorBrightness(%s) failed", display_id)

    # ---- refresh -----------------------------------------------------------

    def _stale(self, checked: float | None, ttl: float, now: float) -> bool:
        return checked is None or now - checked >= ttl

    def _refresh_ddc(self, now: float) -> None:
        if self._dxva is None:
            return
        with self._lock:
            old_groups = self._groups
            self._groups, self._ddc = [], {}
            for g in old_groups:
                try:
                    self._dxva.destroy(g)
                except Exception:
                    log.debug("destroying physical monitors failed", exc_info=True)
            self._ddc_refreshed = now
            try:
                groups = self._dxva.enumerate()
            except Exception:
                log.warning("monitor enumeration failed", exc_info=True)
                return
            self._groups = groups
            for g in groups:
                for e in g.entries:
                    e.value = reconcile(e.value, self._last_set.get(e.id),
                                        self._writer.is_pending(e.id), now)
                    self._ddc[e.id] = e

    def _refresh_builtin(self, now: float) -> None:
        if self._wmi is None:
            return
        self._builtin_checked = now
        try:
            panels = self._wmi.read()
        except Exception:
            log.debug("WMI brightness read failed", exc_info=True)
            panels = []
        with self._lock:
            if not panels:
                self._builtin = None
                self._builtin_hw_ids = set()
                return
            value = reconcile(panels[0][1], self._last_set.get(BUILTIN_ID),
                              self._writer.is_pending(BUILTIN_ID), now)
            self._builtin = DisplayInfo(id=BUILTIN_ID, name=BUILTIN_NAME, brightness=value)
            self._builtin_hw_ids = {h for h in (hardware_id(i) for i, _ in panels) if h}

    # ---- DisplayBackend ----------------------------------------------------

    def list_displays(self) -> list[DisplayInfo]:
        if self._closed:
            return []
        now = self._clock()
        try:
            if self._stale(self._ddc_refreshed, REFRESH_INTERVAL, now):
                self._refresh_ddc(now)
        except Exception:
            log.warning("DDC refresh failed", exc_info=True)
        try:
            ttl = REFRESH_INTERVAL if self._builtin is not None else BUILTIN_ABSENT_RECHECK
            if self._stale(self._builtin_checked, ttl, now):
                self._refresh_builtin(now)
        except Exception:
            log.warning("built-in panel refresh failed", exc_info=True)
        with self._lock:
            builtin = None
            if self._builtin is not None:
                builtin = DisplayInfo(self._builtin.id, self._builtin.name,
                                      self._builtin.brightness)
            return merge_displays(builtin, self._builtin_hw_ids, list(self._ddc.values()))

    def set_brightness(self, display_id: str, value: int) -> None:
        if self._closed:
            return
        try:
            v = clamp_percent(value)
            with self._lock:
                if display_id == BUILTIN_ID:
                    if self._builtin is None:
                        log.debug("no built-in panel; ignoring brightness %d", v)
                        return
                    self._builtin.brightness = v
                else:
                    entry = self._ddc.get(display_id)
                    if entry is None:
                        log.debug("unknown display %s; ignoring brightness %d", display_id, v)
                        return
                    entry.value = v
                self._last_set[display_id] = (v, self._clock())
            self._writer.submit(display_id, v)
        except Exception:
            log.warning("set_brightness(%s) failed", display_id, exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except Exception:
            log.debug("stopping display worker failed", exc_info=True)
        with self._lock:
            groups, self._groups, self._ddc = self._groups, [], {}
            for g in groups:
                try:
                    if self._dxva is not None:
                        self._dxva.destroy(g)
                except Exception:
                    log.debug("destroying physical monitors failed", exc_info=True)
