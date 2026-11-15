"""macOS display brightness backend.

Built-in panel: the private DisplayServices framework (DisplayServicesGetBrightness /
DisplayServicesSetBrightness / DisplayServicesCanChangeBrightness, brightness 0.0-1.0) -- the same
calls the `brightness` CLI and MonitorControl use. Listed as id "builtin", name "Built-in".

External monitors: DDC/CI over the DCP's I2C on Apple Silicon, ported from m1ddc
(github.com/waydabber/m1ddc @ 7339cee: sources/i2c.m, sources/ioregistry.m, headers/i2c.h):
  * CoreDisplay_DisplayCreateInfoDictionary(display)["IODisplayLocation"] -> the display's
    IOMobileFramebuffer in the IORegistry;
  * walk the IOService plane: the first "DCPAVServiceProxy" with Location == "External" after that
    framebuffer is the display's AV service (IOAVServiceCreateWithService); chip address 0x37, or
    0xB7 behind an MCDP29xx (USB-C -> HDMI) converter;
  * VCP 0x10 (luminance) get/set packets written with IOAVServiceWriteI2C(svc, chip, 0x51, ...) and the
    reply read with IOAVServiceReadI2C(svc, chip, 0x51, buf, 12).
Intel Macs have no DCPAVServiceProxy/IOAVService DDC path, so there only the built-in panel is listed
(external DDC is skipped entirely).

DDC is slow and monitors choke on rapid writes, so all DDC I/O runs on one worker thread: the latest
requested value per display wins and writes to one display are at least DDC_MIN_WRITE_INTERVAL apart.
External brightness is re-read at most every DDC_REREAD_INTERVAL; in between list_displays() reports the
last value read or set.

Only the standard library + ctypes is used (no pyobjc). The pure DDC packet helpers at the top of this
module have no OS dependencies and are unit-tested on any platform.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .base import DisplayInfo

log = logging.getLogger(__name__)

# --- DDC/CI packets (pure; ported from m1ddc sources/i2c.m) -------------------------------------------

VCP_LUMINANCE = 0x10              # m1ddc headers/i2c.h LUMINANCE
DDC_INPUT_ADDRESS = 0x51          # m1ddc DEFAULT_INPUT_ADDRESS (host "source address" / I2C data offset)
DDC_DEST_ADDRESS = 0x6E           # display's 8-bit I2C write address (0x37 << 1), part of the checksum
DDC_REPLY_CHECKSUM_SEED = 0x50    # virtual host address used for reply checksums (MonitorControl Arm64DDC.swift)
DDC_CHIP_ADDRESS_DEFAULT = 0x37   # m1ddc headers/ioregistry.h
DDC_CHIP_ADDRESS_MCDP29XX = 0xB7  # m1ddc headers/ioregistry.h
DDC_WAIT = 0.010                  # m1ddc DDC_WAIT (10000 us): before every write and before a read
DDC_ITERATIONS = 2                # m1ddc DDC_ITERATIONS: each packet is written twice
DDC_MCDP_READ_WAIT = 0.050        # m1ddc DDC_MCDP_READ_WAIT
DDC_READ_LENGTH = 12              # m1ddc performDDCReadAtChipAddress reads 12 bytes
DDC_RETRY_READ_WAIT = 0.050       # MonitorControl's default read sleep; used when m1ddc's 10 ms read fails
DDC_RETRY_SLEEP = 0.020           # MonitorControl's default retry sleep
DDC_READ_ATTEMPTS = 3

DDC_MIN_WRITE_INTERVAL = 0.10     # per display
DDC_REREAD_INTERVAL = 10.0        # re-read an external's brightness at most this often
REDISCOVER_INTERVAL = 10.0        # retry matching online externals that had no DDC service yet
FIRST_READ_WAIT = 0.3             # list_displays waits this long for a newly found monitor's first read
MAX_NAME = 20


def _checksum(seed: int, data: bytes | bytearray) -> int:
    for b in data:
        seed ^= b
    return seed


def build_vcp_get_packet(vcp_code: int, input_addr: int = DDC_INPUT_ADDRESS) -> bytes:
    """Get VCP Feature request, i.e. the bytes written at I2C offset 0x51 (m1ddc prepareDDCRead):
    [0x82, 0x01, vcp, 0x6E ^ 0x51 ^ 0x82 ^ 0x01 ^ vcp]."""
    data = bytes([0x82, 0x01, vcp_code & 0xFF])
    return data + bytes([_checksum(DDC_DEST_ADDRESS ^ input_addr, data)])


def build_vcp_set_packet(vcp_code: int, value: int, input_addr: int = DDC_INPUT_ADDRESS) -> bytes:
    """Set VCP Feature request (m1ddc prepareDDCWrite):
    [0x84, 0x03, vcp, value_hi, value_lo, 0x6E ^ 0x51 ^ (all previous bytes)].

    m1ddc sends only up to the last non-zero byte of its buffer, which would drop a 0x00 checksum;
    we always send all 6 bytes (as MonitorControl does)."""
    value = max(0, min(0xFFFF, int(value)))
    data = bytes([0x84, 0x03, vcp_code & 0xFF, value >> 8, value & 0xFF])
    return data + bytes([_checksum(DDC_DEST_ADDRESS ^ input_addr, data)])


def parse_vcp_reply(reply: bytes | bytearray, vcp_code: int) -> tuple[int, int] | None:
    """Parse a Get VCP Feature reply -> (current, maximum), or None if it isn't a valid reply.

    Layout (m1ddc convertI2CtoDDC, DDC/CI spec): [src 0x6E, 0x88 length, 0x02 opcode, result,
    vcp, type, max_hi, max_lo, cur_hi, cur_lo, checksum]. m1ddc trusts the bytes blindly; like
    MonitorControl we also verify the checksum (0x50 ^ bytes[0..9]) and, additionally, the opcode,
    result code (0 = supported) and VCP code, so a garbage read never becomes a brightness."""
    if len(reply) < 11:
        return None
    if _checksum(DDC_REPLY_CHECKSUM_SEED, reply[:10]) != reply[10]:
        return None
    if reply[2] != 0x02 or reply[3] != 0x00 or reply[4] != (vcp_code & 0xFF):
        return None
    maximum = (reply[6] << 8) | reply[7]
    current = (reply[8] << 8) | reply[9]
    if maximum <= 0:
        return None
    return current, maximum


def raw_to_percent(current: int, maximum: int) -> int:
    if maximum <= 0:
        return 0
    return max(0, min(100, round(current * 100 / maximum)))


def percent_to_raw(percent: int, maximum: int) -> int:
    return max(0, min(maximum, round(max(0, min(100, percent)) * maximum / 100)))


# --- coalescing worker ----------------------------------------------------------------------------------


class _Worker:
    """One background thread doing slow brightness I/O.

    set(key, v): only the newest pending value per key is applied, and writes to the same key are at
    least `min_interval` apart. read(key): queued once; skipped while a write for that key is pending."""

    def __init__(self, do_write: Callable[[str, int], None], do_read: Callable[[str], None],
                 min_interval: float = DDC_MIN_WRITE_INTERVAL, clock: Callable[[], float] = time.monotonic):
        self._do_write = do_write
        self._do_read = do_read
        self._min_interval = min_interval
        self._clock = clock
        self._cond = threading.Condition()
        self._sets: dict[str, int] = {}
        self._reads: list[str] = []
        self._last_write: dict[str, float] = {}
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="display-brightness", daemon=True)
        self._thread.start()

    def set(self, key: str, value: int) -> None:
        with self._cond:
            self._sets[key] = value
            self._cond.notify()

    def read(self, key: str) -> None:
        with self._cond:
            if key not in self._reads:
                self._reads.append(key)
                self._cond.notify()

    def forget(self, key: str) -> None:
        with self._cond:
            self._sets.pop(key, None)
            if key in self._reads:
                self._reads.remove(key)
            self._last_write.pop(key, None)

    def close(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _next_job(self):
        now = self._clock()
        wait = None
        for key, value in self._sets.items():
            ready_at = self._last_write.get(key, float("-inf")) + self._min_interval
            if ready_at <= now:
                del self._sets[key]
                return ("set", key, value), None
            wait = ready_at - now if wait is None else min(wait, ready_at - now)
        while self._reads:
            key = self._reads.pop(0)
            if key not in self._sets:
                return ("read", key, None), None
        return None, wait

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    job, wait = self._next_job()
                    if job is not None:
                        break
                    self._cond.wait(wait)
            kind, key, value = job
            try:
                if kind == "set":
                    self._do_write(key, value)
                else:
                    self._do_read(key)
            except Exception:
                log.warning("display brightness %s failed for %s", kind, key, exc_info=True)
            finally:
                if kind == "set":
                    with self._cond:
                        self._last_write[key] = self._clock()


# --- ctypes bindings ------------------------------------------------------------------------------------

kCFStringEncodingUTF8 = 0x08000100
kIOMainPortDefault = 0
kIORegistryIterateRecursively = 1
kIOServicePlane = b"IOService"


def _is_apple_silicon() -> bool:
    """True on Apple Silicon (also when this Python runs under Rosetta)."""
    try:
        libc = ctypes.CDLL(None)
        val = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(val))
        if libc.sysctlbyname(b"hw.optional.arm64", ctypes.byref(val), ctypes.byref(size), None, 0) == 0:
            return val.value == 1
    except Exception:
        log.debug("sysctlbyname(hw.optional.arm64) failed", exc_info=True)
    return False


class _Libs:
    """Loaded frameworks. Any of ds / iokit / coredisplay may be None if unavailable."""

    def __init__(self) -> None:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGGetOnlineDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                              ctypes.POINTER(ctypes.c_uint32)]
        cg.CGGetOnlineDisplayList.restype = ctypes.c_int32
        cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
        cg.CGDisplayIsBuiltin.restype = ctypes.c_bool
        for fn in ("CGDisplayVendorNumber", "CGDisplayModelNumber", "CGDisplaySerialNumber"):
            getattr(cg, fn).argtypes = [ctypes.c_uint32]
            getattr(cg, fn).restype = ctypes.c_uint32
        self.cg = cg

        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        self.cf = cf
        self._string_type = cf.CFStringGetTypeID()
        self._dict_type = cf.CFDictionaryGetTypeID()

        self.ds = None
        try:
            ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
            ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
            ds.DisplayServicesGetBrightness.restype = ctypes.c_int
            ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
            ds.DisplayServicesSetBrightness.restype = ctypes.c_int
            ds.DisplayServicesCanChangeBrightness.argtypes = [ctypes.c_uint32]
            ds.DisplayServicesCanChangeBrightness.restype = ctypes.c_bool
            self.ds = ds
        except (OSError, AttributeError):
            log.warning("DisplayServices unavailable; built-in display brightness disabled", exc_info=True)

        self.iokit = None
        self.coredisplay = None
        if not _is_apple_silicon():
            log.info("not Apple Silicon: external monitor (DDC) brightness disabled")
            return
        try:
            io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            u32, p_u32 = ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
            io.IORegistryGetRootEntry.argtypes = [u32]
            io.IORegistryGetRootEntry.restype = u32
            io.IORegistryEntryCreateIterator.argtypes = [u32, ctypes.c_char_p, u32, p_u32]
            io.IORegistryEntryCreateIterator.restype = ctypes.c_int
            io.IOIteratorNext.argtypes = [u32]
            io.IOIteratorNext.restype = u32
            io.IOObjectConformsTo.argtypes = [u32, ctypes.c_char_p]
            io.IOObjectConformsTo.restype = ctypes.c_int
            io.IORegistryEntryGetName.argtypes = [u32, ctypes.c_char_p]
            io.IORegistryEntryGetName.restype = ctypes.c_int
            io.IORegistryEntryGetRegistryEntryID.argtypes = [u32, ctypes.POINTER(ctypes.c_uint64)]
            io.IORegistryEntryGetRegistryEntryID.restype = ctypes.c_int
            io.IORegistryEntrySearchCFProperty.argtypes = [u32, ctypes.c_char_p, ctypes.c_void_p,
                                                           ctypes.c_void_p, u32]
            io.IORegistryEntrySearchCFProperty.restype = ctypes.c_void_p
            io.IORegistryEntryCreateCFProperty.argtypes = [u32, ctypes.c_void_p, ctypes.c_void_p, u32]
            io.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
            io.IORegistryEntryGetParentEntry.argtypes = [u32, ctypes.c_char_p, p_u32]
            io.IORegistryEntryGetParentEntry.restype = ctypes.c_int
            io.IORegistryEntryCopyFromPath.argtypes = [u32, ctypes.c_void_p]
            io.IORegistryEntryCopyFromPath.restype = u32
            io.IOObjectRelease.argtypes = [u32]
            io.IOObjectRelease.restype = ctypes.c_int
            io.IOAVServiceCreateWithService.argtypes = [ctypes.c_void_p, u32]
            io.IOAVServiceCreateWithService.restype = ctypes.c_void_p
            for fn in (io.IOAVServiceReadI2C, io.IOAVServiceWriteI2C):
                fn.argtypes = [ctypes.c_void_p, u32, u32, ctypes.c_void_p, u32]
                fn.restype = ctypes.c_int32
            cd = ctypes.CDLL("/System/Library/Frameworks/CoreDisplay.framework/CoreDisplay")
            cd.CoreDisplay_DisplayCreateInfoDictionary.argtypes = [ctypes.c_uint32]
            cd.CoreDisplay_DisplayCreateInfoDictionary.restype = ctypes.c_void_p
            self.iokit, self.coredisplay = io, cd
        except (OSError, AttributeError):
            log.warning("IOKit DDC APIs unavailable; external monitor brightness disabled", exc_info=True)

    # CoreFoundation helpers

    def cfstr(self, s: str) -> int:
        return self.cf.CFStringCreateWithCString(None, s.encode(), kCFStringEncodingUTF8)

    def to_str(self, ref) -> str | None:
        if not ref or self.cf.CFGetTypeID(ref) != self._string_type:
            return None
        buf = ctypes.create_string_buffer(1024)
        if not self.cf.CFStringGetCString(ref, buf, len(buf), kCFStringEncodingUTF8):
            return None
        return buf.value.decode(errors="replace")

    def dict_get(self, d, key: str):
        """Borrowed (not retained) value of a CFDictionary, or None."""
        if not d or self.cf.CFGetTypeID(d) != self._dict_type:
            return None
        k = self.cfstr(key)
        try:
            return self.cf.CFDictionaryGetValue(d, k)
        finally:
            self.cf.CFRelease(k)

    def is_dict(self, ref) -> bool:
        return bool(ref) and self.cf.CFGetTypeID(ref) == self._dict_type

    def release(self, ref) -> None:
        if ref:
            self.cf.CFRelease(ref)

    # IOKit helpers

    def search_property(self, entry: int, key: str):
        """IORegistryEntrySearchCFProperty (recursive), +1 retained or None."""
        k = self.cfstr(key)
        try:
            return self.iokit.IORegistryEntrySearchCFProperty(entry, kIOServicePlane, k, None,
                                                              kIORegistryIterateRecursively)
        finally:
            self.cf.CFRelease(k)

    def search_string(self, entry: int, key: str) -> str | None:
        ref = self.search_property(entry, key)
        try:
            return self.to_str(ref)
        finally:
            self.release(ref)

    def entry_id(self, entry: int) -> int | None:
        out = ctypes.c_uint64()
        if self.iokit.IORegistryEntryGetRegistryEntryID(entry, ctypes.byref(out)) != 0:
            return None
        return out.value

    def entry_name(self, entry: int) -> str:
        buf = ctypes.create_string_buffer(128)  # io_name_t
        if self.iokit.IORegistryEntryGetName(entry, buf) != 0:
            return ""
        return buf.value.decode(errors="replace")


# --- display bookkeeping --------------------------------------------------------------------------------


@dataclass
class _External:
    id: str
    name: str
    cg_id: int
    service: int                       # IOAVServiceRef (+1 retained CFTypeRef)
    chip: int = DDC_CHIP_ADDRESS_DEFAULT
    max_value: int | None = None
    brightness: int | None = None
    last_read: float = float("-inf")
    last_set: float = float("-inf")
    warned: bool = False
    first_read: threading.Event = field(default_factory=threading.Event)


class MacDisplays:
    """DisplayBackend for macOS: built-in panel via DisplayServices, external monitors via DDC/CI
    (Apple Silicon only; Intel Macs list just the built-in panel)."""

    def __init__(self) -> None:
        self._libs = _Libs()
        self._lock = threading.RLock()     # guards _externals / _builtins / topology
        self._io_lock = threading.Lock()   # held around every IOAVService call and service release
        self._externals: dict[str, _External] = {}
        self._builtins: dict[str, int] = {}  # "builtin" -> CGDirectDisplayID
        self._topology: tuple[int, ...] | None = None
        self._unmatched = False
        self._last_discovery = float("-inf")
        self._closed = False
        self._worker = _Worker(self._ddc_write_job, self._ddc_read_job)

    # public API

    def list_displays(self) -> list[DisplayInfo]:
        if self._closed:
            return []
        try:
            self._refresh_topology()
        except Exception:
            log.warning("display discovery failed", exc_info=True)
        out: list[DisplayInfo] = []
        with self._lock:
            builtins = list(self._builtins.items())
            externals = list(self._externals.values())
        for did, cg_id in builtins:
            out.append(DisplayInfo(did, "Built-in" if did == "builtin" else "Built-in 2",
                                   self._builtin_get(cg_id)))
        now = time.monotonic()
        fresh = [e for e in externals if not e.first_read.is_set()]
        for ext in externals:
            if now - ext.last_read >= DDC_REREAD_INTERVAL and now - ext.last_set >= DDC_REREAD_INTERVAL:
                self._worker.read(ext.id)
        if fresh:  # give a newly connected monitor a moment for its first DDC read
            deadline = now + FIRST_READ_WAIT
            for ext in fresh:
                ext.first_read.wait(max(0.0, deadline - time.monotonic()))
        for ext in externals:
            out.append(DisplayInfo(ext.id, ext.name, ext.brightness))
        return out

    def set_brightness(self, display_id: str, value: int) -> None:
        if self._closed:
            return
        try:
            value = max(0, min(100, int(value)))
            with self._lock:
                cg_id = self._builtins.get(display_id)
                ext = self._externals.get(display_id)
            if cg_id is not None:
                self._builtin_set(cg_id, value)
            elif ext is not None:
                ext.brightness = value
                ext.last_set = time.monotonic()
                self._worker.set(display_id, value)
            else:
                log.debug("set_brightness: unknown display %r", display_id)
        except Exception:
            log.warning("set_brightness(%r, %r) failed", display_id, value, exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._worker.close()
        except Exception:
            log.debug("worker close failed", exc_info=True)
        with self._lock:
            externals = list(self._externals.values())
            self._externals.clear()
            self._builtins.clear()
        with self._io_lock:
            for ext in externals:
                self._libs.release(ext.service)

    # built-in panel (DisplayServices)

    def _builtin_get(self, cg_id: int) -> int | None:
        ds = self._libs.ds
        if ds is None:
            return None
        try:
            val = ctypes.c_float()
            err = ds.DisplayServicesGetBrightness(cg_id, ctypes.byref(val))
            if err != 0:
                log.debug("DisplayServicesGetBrightness(%d) -> %d", cg_id, err)
                return None
            return max(0, min(100, round(val.value * 100)))
        except Exception:
            log.debug("DisplayServicesGetBrightness failed", exc_info=True)
            return None

    def _builtin_set(self, cg_id: int, value: int) -> None:
        ds = self._libs.ds
        if ds is None:
            return
        err = ds.DisplayServicesSetBrightness(cg_id, ctypes.c_float(value / 100.0))
        if err != 0:
            log.warning("DisplayServicesSetBrightness(%d, %d%%) -> %d", cg_id, value, err)

    # discovery

    def _online_displays(self) -> list[int]:
        ids = (ctypes.c_uint32 * 16)()
        count = ctypes.c_uint32()
        err = self._libs.cg.CGGetOnlineDisplayList(len(ids), ids, ctypes.byref(count))
        if err != 0:
            log.debug("CGGetOnlineDisplayList -> %d", err)
            return []
        return list(ids[: count.value])

    def _refresh_topology(self) -> None:
        online = self._online_displays()
        topo = tuple(sorted(online))
        now = time.monotonic()
        with self._lock:
            if topo == self._topology and not (self._unmatched and now - self._last_discovery >= REDISCOVER_INTERVAL):
                return
            self._topology = topo
            self._last_discovery = now
        self._discover(online)

    def _discover(self, online: list[int]) -> None:
        libs = self._libs
        builtins: dict[str, int] = {}
        external_cg: list[int] = []
        for cg_id in online:
            try:
                if libs.cg.CGDisplayIsBuiltin(cg_id):
                    if libs.ds is not None and libs.ds.DisplayServicesCanChangeBrightness(cg_id):
                        builtins["builtin" if "builtin" not in builtins else "builtin-2"] = cg_id
                    else:
                        log.debug("built-in display %d: brightness not controllable", cg_id)
                else:
                    external_cg.append(cg_id)
            except Exception:
                log.debug("inspecting display %d failed", cg_id, exc_info=True)

        with self._lock:
            old = dict(self._externals)
        keep: dict[str, _External] = {}
        kept_cg = set()
        for ext in old.values():
            if ext.cg_id in external_cg:
                keep[ext.id] = ext
                kept_cg.add(ext.cg_id)
        new: dict[str, _External] = {}
        unmatched = False
        todo = [c for c in external_cg if c not in kept_cg]
        if todo and libs.iokit is not None:
            try:
                found = self._discover_externals(todo, used_ids=set(keep))
                new.update(found)
                unmatched = len(found) < len(todo)
            except Exception:
                log.warning("external display discovery failed", exc_info=True)
                unmatched = True

        removed = [e for k, e in old.items() if k not in keep]
        with self._lock:
            self._builtins = builtins
            self._externals = {**keep, **new}
            self._unmatched = unmatched
        for ext in removed:
            self._worker.forget(ext.id)
        if removed:
            with self._io_lock:
                for ext in removed:
                    libs.release(ext.service)
        for ext in new.values():
            log.info("external display %r (%s): DDC via chip 0x%02x", ext.name, ext.id, ext.chip)
            self._worker.read(ext.id)

    def _discover_externals(self, cg_ids: list[int], used_ids: set[str]) -> dict[str, _External]:
        """Match external CGDisplays to their DCPAVServiceProxy (m1ddc getOnlineDisplayInfos +
        getDisplayDDCTransport) and create IOAVServices for them."""
        libs = self._libs
        io = libs.iokit
        by_fb: dict[int, tuple[int, str]] = {}  # framebuffer registry id -> (cg id, product name)
        for n, cg_id in enumerate(cg_ids, start=1):
            info = self._framebuffer_for(cg_id)
            if info is None:
                log.debug("display %d: no IORegistry framebuffer (virtual/AirPlay/Sidecar?)", cg_id)
                continue
            fb_id, name = info
            by_fb[fb_id] = (cg_id, name or f"Display {len(used_ids) + n}")
        if not by_fb:
            return {}

        services = self._match_av_proxies(set(by_fb), location="External")
        found: dict[str, _External] = {}
        for fb_id, (service, chip) in services.items():
            cg_id, name = by_fb[fb_id]
            try:
                av = io.IOAVServiceCreateWithService(None, service)
            finally:
                io.IOObjectRelease(service)
            if not av:
                log.debug("display %d: IOAVServiceCreateWithService failed", cg_id)
                continue
            did = self._stable_id(cg_id, used_ids | set(found))
            found[did] = _External(id=did, name=name[:MAX_NAME], cg_id=cg_id, service=av, chip=chip)
        return found

    def _stable_id(self, cg_id: int, taken: set[str]) -> str:
        cg = self._libs.cg
        base = "ddc:%04x-%04x-%08x" % (cg.CGDisplayVendorNumber(cg_id), cg.CGDisplayModelNumber(cg_id),
                                       cg.CGDisplaySerialNumber(cg_id))
        did, n = base, 2
        while did in taken:  # identical monitors without serial numbers
            did = f"{base}#{n}"
            n += 1
        return did

    def _framebuffer_for(self, cg_id: int) -> tuple[int, str | None] | None:
        """(registry entry id of the display's IOMobileFramebuffer, product name) via
        CoreDisplay_DisplayCreateInfoDictionary["IODisplayLocation"] (m1ddc getOnlineDisplayInfos)."""
        libs = self._libs
        info = libs.coredisplay.CoreDisplay_DisplayCreateInfoDictionary(cg_id)
        if not info:
            return None
        adapter = 0
        try:
            location = libs.dict_get(info, "IODisplayLocation")
            if not location or not libs.dict_get(info, "kCGDisplayUUID"):
                return None
            adapter = libs.iokit.IORegistryEntryCopyFromPath(kIOMainPortDefault, location)
            if not adapter:
                return None
            fb_id = libs.entry_id(adapter)
            if fb_id is None:
                return None
            return fb_id, self._product_name(adapter)
        finally:
            if adapter:
                libs.iokit.IOObjectRelease(adapter)
            libs.release(info)

    def _product_name(self, adapter: int) -> str | None:
        libs = self._libs
        attrs = libs.search_property(adapter, "DisplayAttributes")
        try:
            product = libs.dict_get(attrs, "ProductAttributes")
            name = libs.to_str(libs.dict_get(product, "ProductName")) if libs.is_dict(product) else None
            return name.strip() or None if name else None
        finally:
            libs.release(attrs)

    def _match_av_proxies(self, fb_ids: set[int], location: str = "External") -> dict[int, tuple[int, int]]:
        """Walk the IOService plane like m1ddc getDisplayDDCTransport: remember the last
        IOMobileFramebuffer seen; the first DCPAVServiceProxy with the given Location after a wanted
        framebuffer belongs to it. Returns fb id -> (retained io_service_t, I2C chip address)."""
        libs = self._libs
        io = libs.iokit
        it = ctypes.c_uint32()
        root = io.IORegistryGetRootEntry(kIOMainPortDefault)
        try:
            if io.IORegistryEntryCreateIterator(root, kIOServicePlane, kIORegistryIterateRecursively,
                                                ctypes.byref(it)) != 0:
                return {}
        finally:
            io.IOObjectRelease(root)
        result: dict[int, tuple[int, int]] = {}
        current_fb: int | None = None
        try:
            while True:
                service = io.IOIteratorNext(it.value)
                if not service:
                    break
                keep = False
                try:
                    if io.IOObjectConformsTo(service, b"IOMobileFramebuffer"):
                        current_fb = libs.entry_id(service)
                        continue
                    if current_fb not in fb_ids or current_fb in result:
                        continue
                    if libs.entry_name(service) != "DCPAVServiceProxy":
                        continue
                    if libs.search_string(service, "Location") != location:
                        continue
                    chip = DDC_CHIP_ADDRESS_MCDP29XX if self._is_mcdp29xx(service) else DDC_CHIP_ADDRESS_DEFAULT
                    result[current_fb] = (service, chip)
                    keep = True
                    if len(result) == len(fb_ids):
                        break
                finally:
                    if not keep:
                        io.IOObjectRelease(service)
        finally:
            io.IOObjectRelease(it.value)
        return result

    def _is_mcdp29xx(self, proxy: int) -> bool:
        """m1ddc isMCDP29XXProxy: parent's EPICProviderClass == "AppleDCPMCDP29XX"."""
        libs = self._libs
        parent = ctypes.c_uint32()
        if libs.iokit.IORegistryEntryGetParentEntry(proxy, kIOServicePlane, ctypes.byref(parent)) != 0:
            return False
        key = libs.cfstr("EPICProviderClass")
        try:
            ref = libs.iokit.IORegistryEntryCreateCFProperty(parent.value, key, None, 0)
            try:
                return libs.to_str(ref) == "AppleDCPMCDP29XX"
            finally:
                libs.release(ref)
        finally:
            libs.release(key)
            libs.iokit.IOObjectRelease(parent.value)

    # DDC I/O (worker thread only)

    def _i2c_write(self, ext: _External, packet: bytes) -> bool:
        """m1ddc performDDCWriteAtChipAddress: DDC_ITERATIONS x (sleep DDC_WAIT, write)."""
        buf = ctypes.create_string_buffer(packet, len(packet))
        for _ in range(DDC_ITERATIONS):
            time.sleep(DDC_WAIT)
            err = self._libs.iokit.IOAVServiceWriteI2C(ext.service, ext.chip, DDC_INPUT_ADDRESS, buf, len(packet))
            if err != 0:
                log.debug("IOAVServiceWriteI2C(%s) -> 0x%x", ext.id, err & 0xFFFFFFFF)
                return False
        return True

    def _ddc_read(self, ext: _External) -> tuple[int, int] | None:
        """m1ddc readingOperation: write the Get VCP request, wait, read 12 bytes at offset 0x51.
        Retries with MonitorControl's longer read delay if the reply is missing or invalid."""
        packet = build_vcp_get_packet(VCP_LUMINANCE)
        read_wait = DDC_MCDP_READ_WAIT if ext.chip == DDC_CHIP_ADDRESS_MCDP29XX else DDC_WAIT
        for attempt in range(DDC_READ_ATTEMPTS):
            if attempt:
                time.sleep(DDC_RETRY_SLEEP)
                read_wait = max(read_wait, DDC_RETRY_READ_WAIT)
            with self._io_lock:
                if self._closed or ext.id not in self._externals:
                    return None
                if not self._i2c_write(ext, packet):
                    continue
                time.sleep(read_wait)
                reply = ctypes.create_string_buffer(DDC_READ_LENGTH)
                err = self._libs.iokit.IOAVServiceReadI2C(ext.service, ext.chip, DDC_INPUT_ADDRESS,
                                                          reply, DDC_READ_LENGTH)
            if err != 0:
                log.debug("IOAVServiceReadI2C(%s) -> 0x%x", ext.id, err & 0xFFFFFFFF)
                continue
            parsed = parse_vcp_reply(reply.raw, VCP_LUMINANCE)
            if parsed is not None:
                return parsed
            log.debug("invalid DDC reply from %s: %s", ext.id, reply.raw.hex())
        return None

    def _ddc_read_job(self, display_id: str) -> None:
        with self._lock:
            ext = self._externals.get(display_id)
        if ext is None:
            return
        started = time.monotonic()
        try:
            parsed = self._ddc_read(ext)
        finally:
            ext.last_read = time.monotonic()
        if parsed is None:
            if not ext.warned:
                log.warning("could not read brightness of %r over DDC", ext.name)
                ext.warned = True
        else:
            current, maximum = parsed
            ext.max_value = maximum
            if ext.last_set < started:  # don't clobber a value set while we were reading
                ext.brightness = raw_to_percent(current, maximum)
        ext.first_read.set()

    def _ddc_write_job(self, display_id: str, value: int) -> None:
        with self._lock:
            ext = self._externals.get(display_id)
        if ext is None:
            return
        if ext.max_value is None:  # need the monitor's scale first
            parsed = self._ddc_read(ext)
            ext.max_value = parsed[1] if parsed else 100
            ext.first_read.set()
        packet = build_vcp_set_packet(VCP_LUMINANCE, percent_to_raw(value, ext.max_value))
        with self._io_lock:
            if self._closed or display_id not in self._externals:
                return
            ok = self._i2c_write(ext, packet)
        if not ok and not ext.warned:
            log.warning("could not set brightness of %r over DDC", ext.name)
            ext.warned = True
