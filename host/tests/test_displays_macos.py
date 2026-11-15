import sys
import threading
import time

import pytest

from macropad_mixer.displays import macos as dm

# --- DDC/CI packets (pure; platform independent) -------------------------------------------------------


def test_get_vcp_packet_matches_spec():
    # DDC/CI spec "Get VCP Feature" for brightness: 6E 51 82 01 10 AC (we send the bytes after 6E 51).
    assert dm.build_vcp_get_packet(0x10) == bytes([0x82, 0x01, 0x10, 0xAC])
    # m1ddc prepareDDCRead: data[3] = 0x6E ^ 0x51 ^ 0x82 ^ 0x01 ^ attr
    assert dm.build_vcp_get_packet(0x12)[3] == 0x6E ^ 0x51 ^ 0x82 ^ 0x01 ^ 0x12


def test_set_vcp_packet():
    # Set brightness to 50: 84 03 10 00 32, checksum 0x6E^0x51^0x84^0x03^0x10^0x00^0x32 = 0x9A
    assert dm.build_vcp_set_packet(0x10, 50) == bytes([0x84, 0x03, 0x10, 0x00, 0x32, 0x9A])
    # 16-bit value split hi/lo (m1ddc prepareDDCWrite)
    pkt = dm.build_vcp_set_packet(0x10, 0x0123)
    assert pkt[3:5] == bytes([0x01, 0x23])
    assert pkt[5] == 0x6E ^ 0x51 ^ 0x84 ^ 0x03 ^ 0x10 ^ 0x01 ^ 0x23
    # a 0x00 checksum is still sent (m1ddc would trim it)
    assert len(dm.build_vcp_set_packet(0x10, 0)) == 6


def _reply(cur, mx, vcp=0x10, result=0x00):
    body = bytes([0x6E, 0x88, 0x02, result, vcp, 0x00, mx >> 8, mx & 0xFF, cur >> 8, cur & 0xFF])
    chk = 0x50
    for b in body:
        chk ^= b
    return body + bytes([chk, 0x00])  # 12 bytes like m1ddc's read


def test_parse_reply_known_bytes():
    # cur=50, max=100: checksum 0x50^6E^88^02^00^10^00^00^64^00^32 = 0xF2
    raw = bytes([0x6E, 0x88, 0x02, 0x00, 0x10, 0x00, 0x00, 0x64, 0x00, 0x32, 0xF2, 0x00])
    assert dm.parse_vcp_reply(raw, 0x10) == (50, 100)


def test_parse_reply_rejects_garbage():
    assert dm.parse_vcp_reply(_reply(30, 255), 0x10) == (30, 255)
    assert dm.parse_vcp_reply(bytes(12), 0x10) is None                 # nothing answered
    assert dm.parse_vcp_reply(_reply(30, 100)[:10], 0x10) is None      # short
    bad = bytearray(_reply(30, 100))
    bad[10] ^= 0xFF
    assert dm.parse_vcp_reply(bytes(bad), 0x10) is None                # checksum
    assert dm.parse_vcp_reply(_reply(30, 100, vcp=0x12), 0x10) is None  # other VCP code
    assert dm.parse_vcp_reply(_reply(30, 100, result=0x01), 0x10) is None  # unsupported
    assert dm.parse_vcp_reply(_reply(0, 0), 0x10) is None               # max 0


def test_scaling():
    assert dm.raw_to_percent(50, 100) == 50
    assert dm.raw_to_percent(255, 255) == 100
    assert dm.raw_to_percent(128, 255) == 50
    assert dm.percent_to_raw(50, 255) == 128
    assert dm.percent_to_raw(100, 255) == 255
    assert dm.percent_to_raw(150, 100) == 100
    assert dm.percent_to_raw(-5, 100) == 0


# --- coalescing worker ---------------------------------------------------------------------------------


def test_worker_coalesces_and_rate_limits():
    writes = []
    w = dm._Worker(lambda k, v: writes.append((time.monotonic(), k, v)), lambda k: None, min_interval=0.1)
    try:
        for v in range(0, 101):
            w.set("a", v)
            time.sleep(0.003)  # ~0.3 s of knob spinning
        w.set("b", 7)
        time.sleep(0.4)
    finally:
        w.close()
    a = [(t, v) for t, k, v in writes if k == "a"]
    assert a[-1][1] == 100                       # latest value wins
    assert 2 <= len(a) <= 6                      # coalesced, not 101 writes
    gaps = [t2 - t1 for (t1, _), (t2, _) in zip(a, a[1:])]
    assert all(g >= 0.09 for g in gaps)          # >= ~100 ms between writes to one display
    assert ("b", 7) in [(k, v) for _, k, v in writes]


def test_worker_reads_and_survives_errors():
    reads = []
    done = threading.Event()

    def read(k):
        reads.append(k)
        if k == "boom":
            raise RuntimeError("ddc failed")
        done.set()

    w = dm._Worker(lambda k, v: None, read)
    try:
        w.read("boom")
        w.read("ok")
        w.read("ok")
        assert done.wait(1)
    finally:
        w.close()
    assert reads[:2] == ["boom", "ok"]  # an error in one job does not stop the worker
    assert not w._thread.is_alive()


# --- real backend (this Mac) -----------------------------------------------------------------------------

mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")


@mac
def test_list_displays_and_noop_set():
    d = dm.MacDisplays()
    try:
        displays = d.list_displays()
        assert all(0 <= (x.brightness or 0) <= 100 for x in displays)
        assert len({x.id for x in displays}) == len(displays)
        builtin = next((x for x in displays if x.id == "builtin"), None)
        if builtin is not None and builtin.brightness is not None:
            assert builtin.name == "Built-in"
            d.set_brightness("builtin", builtin.brightness)  # same value: no visible change
            assert next(x for x in d.list_displays() if x.id == "builtin").brightness == builtin.brightness
        d.set_brightness("no-such-display", 50)  # ignored, never raises
        t = time.perf_counter()
        d.list_displays()
        assert time.perf_counter() - t < 0.5     # cheap enough to poll every 2 s
    finally:
        d.close()
    assert d.list_displays() == []
    assert not d._worker._thread.is_alive()


class _FakeMonitor:
    """Stands in for IOAVServiceWriteI2C/ReadI2C: answers Get VCP 0x10 and records Set VCP."""

    def __init__(self, cur=40, mx=255):
        self.cur, self.mx, self.sets, self.last = cur, mx, [], b""

    def IOAVServiceWriteI2C(self, svc, chip, offset, buf, n):
        assert (chip, offset) == (0x37, 0x51)
        self.last = bytes(buf.raw[:n])
        if self.last[:3] == bytes([0x84, 0x03, 0x10]):
            assert self.last == dm.build_vcp_set_packet(0x10, (self.last[3] << 8) | self.last[4])
            self.cur = (self.last[3] << 8) | self.last[4]
            self.sets.append(self.cur)
        return 0

    def IOAVServiceReadI2C(self, svc, chip, offset, buf, n):
        assert self.last == dm.build_vcp_get_packet(0x10)
        reply = _reply(self.cur, self.mx)
        ctypes_memmove(buf, reply, n)
        return 0


def ctypes_memmove(buf, data, n):
    import ctypes
    ctypes.memmove(buf, data, min(n, len(data)))


@mac
def test_ddc_path_with_fake_monitor(monkeypatch):
    monkeypatch.setattr(dm, "DDC_WAIT", 0.0)
    d = dm.MacDisplays()
    try:
        d._refresh_topology()
        fake = _FakeMonitor()
        d._libs.iokit = fake
        d._libs.release = lambda ref: None
        ext = dm._External(id="ddc:test", name="Fake", cg_id=12345, service=1)
        d._externals[ext.id] = ext
        d._ddc_read_job(ext.id)
        assert (ext.max_value, ext.brightness) == (255, 16)   # 40/255
        for v in range(0, 81, 2):
            d.set_brightness("ddc:test", v)
        listed = {x.id: x for x in d.list_displays()}
        assert listed["ddc:test"].brightness == 80             # last set value, without re-reading
        time.sleep(0.4)
        assert fake.sets[-1] == dm.percent_to_raw(80, 255)
        assert len(fake.sets) <= 5                             # coalesced
    finally:
        d.close()
