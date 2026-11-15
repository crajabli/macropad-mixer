"""Tests for the Windows display-brightness backend's pure logic and caching/coalescing.

Nothing here touches Windows APIs: dxva2/WMI are replaced with fakes.
"""

import ctypes
import threading
import time

import pytest

from macropad_mixer.displays import windows as w
from macropad_mixer.displays.base import DisplayInfo


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_scale_roundtrip():
    assert w.scale_to_percent(0, 0, 100) == 0
    assert w.scale_to_percent(100, 0, 100) == 100
    assert w.scale_to_percent(50, 0, 100) == 50
    assert w.scale_to_percent(10, 10, 60) == 0
    assert w.scale_to_percent(35, 10, 60) == 50
    assert w.scale_to_percent(60, 10, 60) == 100
    assert w.scale_to_percent(200, 0, 100) == 100  # out of range clamps
    assert w.scale_to_percent(42, 5, 5) == 42      # degenerate range
    for pct in range(0, 101):
        raw = w.percent_to_raw(pct, 0, 255)
        assert 0 <= raw <= 255
        assert abs(w.scale_to_percent(raw, 0, 255) - pct) <= 1
    assert w.percent_to_raw(50, 10, 60) == 35
    assert w.percent_to_raw(150, 0, 100) == 100
    assert w.percent_to_raw(-5, 0, 100) == 0


def test_clamp_percent():
    assert w.clamp_percent(-3) == 0
    assert w.clamp_percent(101) == 100
    assert w.clamp_percent(49.6) == 50
    assert w.clamp_percent("7") == 7


def test_ids_and_numbers():
    assert w.short_device_name("\\\\.\\DISPLAY2") == "DISPLAY2"
    assert w.ddc_display_id("\\\\.\\DISPLAY2", 0) == "ddc:DISPLAY2:0"
    assert w.ddc_display_id("", 1) == "ddc:?:1"
    assert w.display_number("\\\\.\\DISPLAY12") == 12
    assert w.display_number("") is None


def test_choose_monitor_name():
    assert w.choose_monitor_name("DELL U2720Q", "Generic PnP Monitor", 2) == "DELL U2720Q"
    assert w.choose_monitor_name("Generic PnP Monitor", "LG ULTRAFINE", 2) == "LG ULTRAFINE"
    assert w.choose_monitor_name("Generic PnP Monitor", "Generic PnP Monitor", 2) == "Display 2"
    assert w.choose_monitor_name("", None, 3) == "Display 3"
    assert len(w.choose_monitor_name("A" * 50, None, 1)) == w.MAX_NAME_LEN


def test_hardware_id():
    assert w.hardware_id("DISPLAY\\BOE0867\\4&2f1d3f05&0&UID8388688_0") == "BOE0867"
    assert w.hardware_id("MONITOR\\boe0867\\{4d36e96e-e325-11ce-bfc1-08002be10318}\\0001") == "BOE0867"
    assert w.hardware_id("PCI\\VEN_8086") is None
    assert w.hardware_id("") is None
    assert w.hardware_id(None) is None


def test_parse_wmi_output():
    out = "DISPLAY\\BOE0867\\4&2f&0&UID1_0|70\r\n\r\nDISPLAY\\X\\1|101\njunk|abc\n55\n"
    assert w.parse_wmi_brightness_output(out) == [
        ("DISPLAY\\BOE0867\\4&2f&0&UID1_0", 70),
        ("DISPLAY\\X\\1", 100),
        ("", 55),
    ]
    assert w.parse_wmi_brightness_output("") == []


def test_powershell_commands():
    read = w.powershell_read_command()
    assert read[:2] == ["powershell", "-NoProfile"]
    assert "WmiMonitorBrightness" in read[-1] and "root/WMI" in read[-1]
    write = w.powershell_write_command(140)
    assert "WmiSetBrightness" in write[-1]
    assert "Brightness=[byte]100" in write[-1]
    assert "Timeout=1" in write[-1]
    assert "Brightness=[byte]0" in w.powershell_write_command(-4)[-1]


def test_reconcile():
    assert w.reconcile(40, None, False, 100.0) == 40
    assert w.reconcile(40, (80, 99.0), False, 100.0) == 80     # within grace
    assert w.reconcile(40, (80, 10.0), False, 100.0) == 40     # grace expired
    assert w.reconcile(40, (80, 10.0), True, 100.0) == 80      # still pending
    assert w.reconcile(None, None, False, 0.0) is None


def test_merge_prefers_builtin_over_ddc_of_same_panel():
    builtin = DisplayInfo("builtin", "Built-in", 60)
    ddc = [w.DdcEntry("ddc:DISPLAY1:0", "Panel", hw_id="BOE0867", value=10),
           w.DdcEntry("ddc:DISPLAY2:0", "DELL", hw_id="DEL41A8", value=30)]
    out = w.merge_displays(builtin, {"BOE0867"}, ddc)
    assert [d.id for d in out] == ["builtin", "ddc:DISPLAY2:0"]
    assert out[1] == DisplayInfo("ddc:DISPLAY2:0", "DELL", 30)
    # without a built-in panel nothing is dropped
    assert len(w.merge_displays(None, set(), ddc)) == 2


def test_physical_monitor_layout():
    PM = w._physical_monitor_struct()
    # HANDLE first, description right after (packed), 128 WCHARs.
    assert PM.hPhysicalMonitor.offset == 0
    assert PM.szPhysicalMonitorDescription.offset == ctypes.sizeof(ctypes.c_void_p)
    wchar = ctypes.sizeof(ctypes.c_wchar)  # 2 on Windows (4 on macOS)
    assert ctypes.sizeof(PM) == ctypes.sizeof(ctypes.c_void_p) + 128 * wchar


# --------------------------------------------------------------------------- #
# Coalescer
# --------------------------------------------------------------------------- #


def test_take_ready_respects_interval_and_latest_wins():
    c = w.Coalescer(lambda k, v: None, interval_for=lambda k: 0.1)
    c._pending.update({"a": 10})
    c._pending["a"] = 20  # latest wins
    ready, wait = c.take_ready(5.0)
    assert ready == [("a", 20)] and wait is None
    c._last_write["a"] = 5.0
    c._pending["a"] = 30
    ready, wait = c.take_ready(5.04)
    assert ready == [] and wait == pytest.approx(0.06)
    ready, wait = c.take_ready(5.1)
    assert ready == [("a", 30)]


def test_take_ready_is_per_key():
    c = w.Coalescer(lambda k, v: None, interval_for=lambda k: 0.1)
    c._last_write["a"] = 1.0
    c._pending.update({"a": 1, "b": 2})
    ready, wait = c.take_ready(1.05)
    assert ready == [("b", 2)] and wait == pytest.approx(0.05)


def test_coalescer_thread_coalesces_bursts():
    writes = []
    first_started = threading.Event()
    release = threading.Event()

    def write(k, v):
        writes.append((k, v, time.monotonic()))
        first_started.set()
        release.wait(2)

    c = w.Coalescer(write, interval_for=lambda k: 0.05)
    try:
        c.submit("a", 1)
        assert first_started.wait(2)
        for v in range(2, 50):  # knob spin while the first write is in flight
            c.submit("a", v)
        release.set()
        deadline = time.monotonic() + 2
        while len(writes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.15)
        assert [v for _, v, _ in writes] == [1, 49]
        assert writes[1][2] - writes[0][2] >= 0.05
        assert not c.is_pending("a")
    finally:
        c.close()


def test_coalescer_survives_write_errors_and_close_stops():
    calls = []

    def write(k, v):
        calls.append(v)
        raise OSError("boom")

    c = w.Coalescer(write, interval_for=lambda k: 0.0)
    c.submit("a", 1)
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    c.submit("a", 2)
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [1, 2]
    c.close()
    assert not c._thread.is_alive()
    c.submit("a", 3)  # ignored after close
    time.sleep(0.05)
    assert calls == [1, 2]


# --------------------------------------------------------------------------- #
# WindowsDisplays with fake dxva2 / WMI
# --------------------------------------------------------------------------- #


class Clock:
    """Real monotonic time plus a manual offset (the worker thread still needs real waits)."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset

    def advance(self, s):
        self.offset += s


class FakeDxva:
    def __init__(self, monitors):
        self.monitors = monitors  # list of (device, [(name, hw_id, lo, cur, hi)])
        self.enumerations = 0
        self.destroyed = []
        self.sets = []
        self.fail_set = False
        self._next = 1000

    def enumerate(self):
        self.enumerations += 1
        groups = []
        for device, mons in self.monitors:
            g = w._PhysGroup(device=device, count=len(mons), array=object())
            for i, (name, hw, lo, cur, hi) in enumerate(mons):
                self._next += 1
                g.entries.append(w.DdcEntry(id=w.ddc_display_id(device, i), name=name,
                                            handle=self._next, hw_id=hw, lo=lo, hi=hi,
                                            value=w.scale_to_percent(cur, lo, hi)))
            groups.append(g)
        return groups

    def set_brightness(self, handle, raw):
        if self.fail_set:
            raise OSError("DDC write failed")
        self.sets.append((handle, raw))
        return True

    def destroy(self, group):
        self.destroyed.append(group)
        group.array = None


class FakeWmi:
    def __init__(self, panels):
        self.panels = panels
        self.reads = 0
        self.writes = []

    def read(self):
        self.reads += 1
        return list(self.panels)

    def write(self, value):
        self.writes.append(value)

    def init_thread(self):
        pass


def make(monitors=(), panels=(), clock=None):
    clock = clock or Clock()
    d = w.WindowsDisplays(ddc=False, wmi=False, clock=clock)
    d._dxva = FakeDxva(list(monitors))
    d._wmi = FakeWmi(list(panels))
    return d, clock


def wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


DELL = ("\\\\.\\DISPLAY2", [("DELL U2720Q", "DEL41A8", 0, 50, 100)])
LG = ("\\\\.\\DISPLAY3", [("LG", "GSM5B7F", 0, 128, 255)])


def test_lists_builtin_and_ddc():
    d, _ = make([DELL, LG], [("DISPLAY\\BOE0867\\4&1_0", 70)])
    try:
        out = d.list_displays()
        assert out == [
            DisplayInfo("builtin", "Built-in", 70),
            DisplayInfo("ddc:DISPLAY2:0", "DELL U2720Q", 50),
            DisplayInfo("ddc:DISPLAY3:0", "LG", 50),
        ]
    finally:
        d.close()


def test_desktop_has_no_builtin_and_rechecks_rarely():
    d, clock = make([DELL], [])
    try:
        assert [x.id for x in d.list_displays()] == ["ddc:DISPLAY2:0"]
        clock.advance(w.REFRESH_INTERVAL + 1)
        d.list_displays()
        assert d._wmi.reads == 1
        clock.advance(w.BUILTIN_ABSENT_RECHECK)
        d.list_displays()
        assert d._wmi.reads == 2
    finally:
        d.close()


def test_builtin_panel_answering_ddc_is_deduplicated():
    panel = ("\\\\.\\DISPLAY1", [("Panel", "BOE0867", 0, 30, 100)])
    d, _ = make([panel, DELL], [("DISPLAY\\BOE0867\\4&1_0", 70)])
    try:
        assert [x.id for x in d.list_displays()] == ["builtin", "ddc:DISPLAY2:0"]
    finally:
        d.close()


def test_caches_and_refreshes_every_10s_destroying_old_handles():
    d, clock = make([DELL])
    try:
        d.list_displays()
        d.list_displays()
        clock.advance(5)
        d.list_displays()
        assert d._dxva.enumerations == 1
        first_groups = list(d._groups)
        clock.advance(w.REFRESH_INTERVAL)
        d.list_displays()
        assert d._dxva.enumerations == 2
        assert d._dxva.destroyed == first_groups
    finally:
        d.close()


def test_set_brightness_updates_cache_and_writes_scaled_value():
    d, clock = make([LG], [("DISPLAY\\BOE0867\\4&1_0", 70)])
    try:
        d.list_displays()
        d.set_brightness("ddc:DISPLAY3:0", 100)
        d.set_brightness("builtin", 25)
        out = {x.id: x.brightness for x in d.list_displays()}
        assert out == {"builtin": 25, "ddc:DISPLAY3:0": 100}
        handle = d._ddc["ddc:DISPLAY3:0"].handle
        assert wait_for(lambda: d._dxva.sets == [(handle, 255)])
        assert wait_for(lambda: d._wmi.writes == [25])
    finally:
        d.close()


def test_knob_spin_is_coalesced():
    d, _ = make([DELL])
    try:
        d.list_displays()
        for v in range(0, 101, 2):
            d.set_brightness("ddc:DISPLAY2:0", v)
        assert wait_for(lambda: d._dxva.sets and d._dxva.sets[-1][1] == 100)
        time.sleep(0.2)
        assert len(d._dxva.sets) <= 3  # first value, maybe one in between, then the latest
        assert d._dxva.sets[-1][1] == 100
    finally:
        d.close()


def test_refresh_keeps_recently_set_value():
    d, clock = make([DELL])
    try:
        d.list_displays()
        d.set_brightness("ddc:DISPLAY2:0", 90)
        clock.advance(w.REFRESH_INTERVAL)  # re-read happens, monitor still reports 50
        d._last_set["ddc:DISPLAY2:0"] = (90, clock() - 1)  # set 1 s ago (within grace)
        assert d.list_displays()[0].brightness == 90
        clock.advance(w.REFRESH_INTERVAL)
        assert d.list_displays()[0].brightness == 50  # grace over: trust the monitor
    finally:
        d.close()


def test_unknown_display_and_failures_never_raise():
    d, _ = make([DELL])
    try:
        d.set_brightness("ddc:NOPE:0", 50)
        d.set_brightness("builtin", 50)  # no built-in panel
        d.list_displays()
        d._dxva.fail_set = True
        d.set_brightness("ddc:DISPLAY2:0", 10)
        time.sleep(0.1)

        def boom():
            raise OSError("enumeration exploded")

        d._dxva.enumerate = boom
        d._ddc_refreshed = None
        assert d.list_displays() == []
        d._wmi.read = boom
        d._builtin_checked = None
        assert d.list_displays() == []
    finally:
        d.close()


def test_close_stops_worker_and_destroys_handles():
    d, _ = make([DELL, LG])
    d.list_displays()
    d.set_brightness("ddc:DISPLAY2:0", 10)
    groups = list(d._groups)
    d.close()
    assert d._dxva.destroyed == groups
    assert d._writer._thread is None or not d._writer._thread.is_alive()
    assert d.list_displays() == []
    d.set_brightness("ddc:DISPLAY2:0", 20)  # no-op, no raise
    d.close()  # idempotent


def test_constructs_on_non_windows_without_raising():
    d = w.WindowsDisplays()  # WinDLL missing -> no DDC; comtypes missing -> PowerShell
    try:
        assert d.list_displays() == []
        d.set_brightness("builtin", 50)
    finally:
        d.close()
