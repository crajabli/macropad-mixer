"""Pure-Python tests for the Windows backend's grouping/naming logic (no pycaw needed)."""

import sys

from macropad_mixer.backends import windows as w
from macropad_mixer.backends.base import AppInfo


def rec(
    exe,
    pid,
    vol=1.0,
    muted=False,
    started=100.0,
    state=w.STATE_ACTIVE,
    dn="",
    handle=None,
):
    return w.SessionRecord(
        app_id=w.app_id_for_exe(exe),
        exe_name=exe,
        pid=pid,
        display_name=dn,
        volume=vol,
        muted=muted,
        started=started,
        state=state,
        handle=handle,
    )


def test_module_imports_without_pycaw():
    assert "pycaw" not in sys.modules
    assert "comtypes" not in sys.modules
    assert hasattr(w, "WindowsBackend")


def test_app_id_is_lowercased_basename():
    assert w.app_id_for_exe("Spotify.exe") == "spotify.exe"
    assert (
        w.app_id_for_exe(r"C:\Program Files\Google\Chrome\chrome.EXE") == "chrome.exe"
    )


def test_display_name_filtering():
    assert w.is_usable_display_name("Discord")
    assert not w.is_usable_display_name("")
    assert not w.is_usable_display_name("   ")
    assert not w.is_usable_display_name(None)
    assert not w.is_usable_display_name("@%SystemRoot%\\System32\\AudioSrv.Dll,-202")


def test_fallback_name():
    assert w.fallback_name("spotify.exe") == "Spotify"
    assert w.fallback_name("MSEDGE.EXE") == "Msedge"
    assert w.fallback_name(r"C:\x\vlc.exe") == "Vlc"


def test_choose_display_name_order():
    assert (
        w.choose_display_name(["", "Discord"], "Discord App", "discord.exe")
        == "Discord"
    )
    assert (
        w.choose_display_name(
            ["@%SystemRoot%\\x.dll,-1"], "Google Chrome", "chrome.exe"
        )
        == "Google Chrome"
    )
    assert w.choose_display_name([None], None, "spotify.exe") == "Spotify"
    assert w.choose_display_name([], "  ", "spotify.exe") == "Spotify"


def test_grouping_merges_sessions_and_drops_expired():
    groups = w.group_sessions(
        [
            rec("chrome.exe", 10),
            rec("Spotify.exe", 20),
            rec("chrome.exe", 11),
            rec("CHROME.EXE", 12, state=w.STATE_INACTIVE),
            rec("chrome.exe", 13, state=w.STATE_EXPIRED),
            rec("zoom.exe", 30, state=w.STATE_EXPIRED),
        ]
    )
    assert list(groups) == ["chrome.exe", "spotify.exe"]
    assert [s.pid for s in groups["chrome.exe"].sessions] == [10, 11, 12]


def test_summary_max_volume_all_muted_earliest_start():
    g = w.group_sessions(
        [
            rec("chrome.exe", 1, vol=0.25, muted=True, started=300.0),
            rec("chrome.exe", 2, vol=0.704, muted=False, started=200.0),
            rec("chrome.exe", 3, vol=None, muted=None, started=None),
        ]
    )["chrome.exe"]
    info = w.summarize_group(g, lambda grp: "Google Chrome")
    assert info == AppInfo(
        id="chrome.exe", name="Google Chrome", started=200.0, vol=70, mute=False
    )

    g2 = w.group_sessions(
        [rec("a.exe", 1, muted=True), rec("a.exe", 2, muted=True, vol=0.0)]
    )["a.exe"]
    info2 = w.summarize_group(g2)
    assert info2.mute is True and info2.vol == 100 and info2.name == "A"


def test_summary_unknown_volume_is_none():
    g = w.group_sessions([rec("x.exe", 1, vol=None, muted=None, started=None)])["x.exe"]
    info = w.summarize_group(g)
    assert info.vol is None and info.mute is None and info.started == 0.0


def test_handles_and_apply_to_every_session():
    class FakeVol:
        def __init__(self):
            self.level = None
            self.mute = None

        def SetMasterVolume(self, level, ctx):
            self.level = level

        def SetMute(self, m, ctx):
            self.mute = m

    class Boom:
        def SetMasterVolume(self, level, ctx):
            raise OSError("session gone")

    a, b, boom = FakeVol(), FakeVol(), Boom()
    backend = w.WindowsBackend()
    backend._tls.com_ready = True  # skip real COM init
    groups = w.group_sessions(
        [
            rec("chrome.exe", 1, handle=a),
            rec("chrome.exe", 2, handle=boom),
            rec("chrome.exe", 3, handle=b),
        ]
    )
    backend._tls.groups = groups
    backend._enumerate = lambda: groups  # re-enumeration returns the same fakes
    backend.set_volume("chrome.exe", 42)
    assert a.level == b.level == 0.42
    backend.set_volume("chrome.exe", 150)
    assert a.level == 1.0
    backend.set_mute("chrome.exe", True)
    assert a.mute == b.mute == 1
    backend.set_volume("nope.exe", 10)  # unknown id: logged, no exception


def test_image_from_black_white_recovers_alpha_and_colour():
    from macropad_mixer.backends.windows import image_from_black_white

    # 2x1 px, BGRA rows: pixel 0 opaque red, pixel 1 fully transparent.
    black = bytes([0, 0, 200, 0, 0, 0, 0, 0])
    white = bytes([0, 0, 200, 0, 255, 255, 255, 0])
    img = image_from_black_white(black + bytes(8), white + bytes(8), 2)  # pad to 2x2
    assert img.getpixel((0, 0)) == (200, 0, 0, 255)
    assert img.getpixel((1, 0))[3] == 0


def test_image_from_black_white_half_transparent():
    from macropad_mixer.backends.windows import image_from_black_white

    # 50% blue over black = (0,0,100); over white = (127,127,227) in BGR terms.
    black = bytes([100, 0, 0, 0])
    white = bytes([227, 127, 127, 0])
    img = image_from_black_white(black, white, 1)
    r, g, b, a = img.getpixel((0, 0))
    assert 125 <= a <= 130 and b > 190 and r < 10 and g < 10
