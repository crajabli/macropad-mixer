import importlib
import os
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def linux(monkeypatch):
    """Import the Linux backend with a fake pulsectl (not installed off Linux)."""
    fake = types.ModuleType("pulsectl")

    class PulseError(Exception):
        pass

    class Pulse:
        def __init__(self, name):
            self.inputs = []

        def sink_input_list(self):
            return self.inputs

        def close(self):
            pass

    fake.PulseError = PulseError
    fake.Pulse = Pulse
    monkeypatch.setitem(sys.modules, "pulsectl", fake)
    monkeypatch.delitem(sys.modules, "macropad_mixer.backends.linux", raising=False)
    return importlib.import_module("macropad_mixer.backends.linux")


def make_proc(root, pid, comm=None, exe=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    if comm is not None:
        (d / "comm").write_text(comm + "\n")
    if exe is not None:
        os.symlink(exe, d / "exe")
    return d


def sink_input(name, pid=None, binary=None):
    props = {"application.name": name}
    if pid is not None:
        props["application.process.id"] = pid
    if binary is not None:
        props["application.process.binary"] = binary
    return SimpleNamespace(proplist=props, volume=SimpleNamespace(value_flat=0.5), mute=0)


def test_proc_alive_checks(linux, tmp_path):
    make_proc(tmp_path, 100, comm="firefox")
    make_proc(tmp_path, 101, comm="bash")
    make_proc(tmp_path, 102, comm="a-very-long-pro")  # comm truncated to 15 chars
    make_proc(tmp_path, 103, exe="/usr/lib/spotify/spotify (deleted)")  # no comm, exe only
    root = str(tmp_path)

    assert linux._proc_alive(100, "firefox", root)
    assert linux._proc_alive(100, None, root)  # no binary recorded: existence is enough
    assert not linux._proc_alive(101, "firefox", root)  # PID reused by another program
    assert linux._proc_alive(102, "a-very-long-program-name", root)
    assert linux._proc_alive(103, "spotify", root)
    assert not linux._proc_alive(999, "firefox", root)  # gone
    assert not linux._proc_alive(100, "firefox", str(tmp_path / "missing"))


def test_app_alive_after_app_drops_out(linux, tmp_path):
    be = linux.LinuxBackend()
    be._proc_root = str(tmp_path)
    pulse = be._conn()
    pulse.inputs = [
        sink_input("Firefox", "100", "firefox"),
        sink_input("Firefox", "105", "firefox"),
        sink_input("Spotify", "bogus", "spotify"),  # invalid PID ignored
        sink_input("mpv"),  # no PID at all
    ]
    make_proc(tmp_path, 105, comm="firefox")
    assert {a.id for a in be.list_apps()} == {"Firefox", "Spotify", "mpv"}

    pulse.inputs = []  # paused: Firefox destroys its sink inputs
    be.list_apps()
    assert be.app_alive("Firefox")  # 105 still running
    assert not be.app_alive("Spotify")
    assert not be.app_alive("mpv")
    assert not be.app_alive("never-seen")

    # Listed again with a new PID: the set is replaced, so old 105 no longer counts.
    pulse.inputs = [sink_input("Firefox", "200", "firefox")]
    be.list_apps()
    pulse.inputs = []
    be.list_apps()
    assert not be.app_alive("Firefox")
    make_proc(tmp_path, 200, comm="firefox")
    assert be.app_alive("Firefox")


def test_app_alive_never_raises(linux):
    be = linux.LinuxBackend()
    be._procs["x"] = None  # corrupt state
    assert be.app_alive("x") is False
