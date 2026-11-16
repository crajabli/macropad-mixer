import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")


def test_finetune_urls():
    from macropad_mixer.backends.macos import mute_url, volume_url

    assert volume_url("com.spotify.client", 55) == "finetune://set-volumes?app=com.spotify.client&volume=55"
    assert mute_url("com.spotify.client", True) == "finetune://set-mute?app=com.spotify.client&muted=true"
    assert mute_url("a b&c", False) == "finetune://set-mute?app=a%20b%26c&muted=false"


def test_url_sender_coalesces(monkeypatch):
    from macropad_mixer.backends import macos

    opened = []
    monkeypatch.setattr(macos.subprocess, "run", lambda cmd, **kw: opened.append(cmd[-1]))
    sender = macos._UrlSender()
    for v in range(0, 101, 5):
        sender.submit("vol", "app", macos.volume_url("app", v))
    time.sleep(0.3)
    sender.close()
    assert 1 <= len(opened) <= 3
    assert opened[-1].endswith("volume=100")


def test_recent_output_window(monkeypatch):
    from macropad_mixer.backends import macos

    playing = {"v": 1}
    now = {"t": 1000.0}
    monkeypatch.setattr(macos, "_process_objects", lambda: [7])
    monkeypatch.setattr(macos, "_resolve_app", lambda pid: ("/Applications/X.app", pid))
    monkeypatch.setattr(macos, "_bundle_info", lambda path: ("com.example.x", "X"))
    monkeypatch.setattr(macos, "_start_time", lambda pid: 5.0)
    monkeypatch.setattr(macos, "finetune_installed", lambda: True)

    def fake_get(obj, sel, value):
        value.value = 4242 if sel == macos.kAudioProcessPropertyPID else playing["v"]
        return True

    monkeypatch.setattr(macos, "_get", fake_get)
    monkeypatch.setattr(macos.time, "monotonic", lambda: now["t"])
    b = macos.MacBackend()
    try:
        assert [a.id for a in b.list_apps()] == ["com.example.x"]
        playing["v"] = 0
        now["t"] += 59
        assert [a.id for a in b.list_apps()] == ["com.example.x"]  # paused briefly: still listed
        now["t"] += 2
        assert b.list_apps() == []
    finally:
        b.close()


def test_finetune_itself_is_hidden():
    # FineTune re-plays the audio of apps it adjusts, so Core Audio reports it as playing.
    from macropad_mixer.backends import macos

    assert macos._is_system(macos.FINETUNE_BUNDLE_ID)
    assert not macos._is_system("com.spotify.client")


def test_app_alive_follows_the_process():
    import subprocess

    from macropad_mixer.backends import macos

    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        backend = macos.MacBackend.__new__(macos.MacBackend)  # skip FineTune checks
        backend._pids = {"x": {proc.pid: macos._pid_path(proc.pid)}}
        assert backend.app_alive("x")
        assert not backend.app_alive("unknown")
        backend._pids["y"] = {proc.pid: "/some/other/binary"}  # PID reused by another program
        assert not backend.app_alive("y")
    finally:
        proc.kill()
        proc.wait()
    assert not backend.app_alive("x")


def test_finetune_is_started_when_not_running(monkeypatch):
    from macropad_mixer.backends import macos

    launched = []
    monkeypatch.setattr(macos, "finetune_running", lambda: False)
    monkeypatch.setattr(macos, "launch_finetune", lambda: launched.append(1))
    backend = macos.MacBackend.__new__(macos.MacBackend)
    backend._finetune_ok = True
    backend._finetune_running_checked = float("-inf")
    backend._ensure_finetune_running()
    backend._ensure_finetune_running()  # throttled: no second launch within 30 s
    assert launched == [1]
    backend._finetune_ok = False        # not installed: never try
    backend._finetune_running_checked = float("-inf")
    backend._ensure_finetune_running()
    assert launched == [1]


def test_app_alive_finds_apps_never_seen_playing(monkeypatch):
    from macropad_mixer.backends import macos

    monkeypatch.setattr(macos, "running_apps", lambda: {"com.google.Chrome": {42: "/x"}})
    backend = macos.MacBackend.__new__(macos.MacBackend)
    backend._pids = {}
    assert backend.app_alive("com.google.Chrome")
    assert backend._pids["com.google.Chrome"] == {42: "/x"}   # tracked from now on
    assert not backend.app_alive("com.example.closed")
