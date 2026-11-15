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
