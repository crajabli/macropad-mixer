import json

import pytest

from macropad_mixer.backends.base import AppInfo, load_backend
from macropad_mixer.backends.fake import FakeBackend
from macropad_mixer.mixer import Mixer


class FakeLink:
    def __init__(self):
        self.generation = 1
        self.incoming: list[bytes] = []
        self.sent: list[dict] = []

    def read_lines(self):
        lines, self.incoming = self.incoming, []
        return lines

    def send(self, data: bytes) -> bool:
        assert data.endswith(b"\n") and len(data) <= 4096
        self.sent.append(json.loads(data))
        return True

    def push(self, **msg):
        self.incoming.append(json.dumps(msg).encode())

    def take(self, t=None):
        out = [m for m in self.sent if t is None or m["t"] == t]
        self.sent = []
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def apps3():
    return [AppInfo("old", "Old", 100.0), AppInfo("new", "New", 300.0), AppInfo("mid", "Mid", 200.0)]


@pytest.fixture
def env():
    backend = FakeBackend(apps3(), report_volume=False)
    link, clock = FakeLink(), Clock()
    mixer = Mixer(backend, link, step=5, poll=2.0, clock=clock)
    return mixer, backend, link, clock


def test_first_tick_sends_apps_recent_first_and_ping(env):
    mixer, _, link, _ = env
    mixer.tick()
    sent = link.take()
    apps = [m for m in sent if m["t"] == "apps"]
    assert len(apps) == 1
    assert [a["id"] for a in apps[0]["apps"]] == ["new", "mid", "old"]
    assert apps[0]["apps"][0] == {"id": "new", "name": "New", "vol": 100, "mute": False}
    assert {"t": "ping"} in sent


def test_ping_every_2s_and_apps_refresh_every_30s(env):
    mixer, _, link, clock = env
    mixer.tick()
    link.take()
    clock.t += 1.0
    mixer.tick()
    assert link.take() == []
    clock.t += 1.0
    mixer.tick()
    assert link.take() == [{"t": "ping"}]  # unchanged apps -> no resend yet
    for _ in range(14):
        clock.t += 2.0
        mixer.tick()
    assert len(link.take("apps")) == 1  # periodic resend at 30 s


def test_hello_gets_apps_and_ping_gets_pong(env):
    mixer, _, link, _ = env
    mixer.tick()
    link.take()
    link.push(t="hello", fw="0.1.0")
    link.push(t="ping")
    link.push(t="unknown_type", foo=1)
    mixer.tick()
    sent = link.take()
    assert [m["t"] for m in sent] == ["apps", "pong"]


def test_delta_clamps_and_replies_vol(env):
    mixer, backend, link, _ = env
    mixer.tick()
    link.take()
    link.push(t="delta", id="new", d=-2)
    mixer.tick()
    assert link.take() == [{"t": "vol", "id": "new", "vol": 90, "mute": False}]
    assert backend.volumes["new"] == 90
    link.push(t="delta", id="new", d=5)
    link.push(t="delta", id="new", d=-100)
    mixer.tick()
    assert [m["vol"] for m in link.take("vol")] == [100, 0]
    assert backend.volumes["new"] == 0


def test_mute_toggles(env):
    mixer, backend, link, _ = env
    mixer.tick()
    link.take()
    link.push(t="mute", id="mid")
    mixer.tick()
    assert link.take() == [{"t": "vol", "id": "mid", "vol": 100, "mute": True}]
    assert backend.mutes["mid"] is True
    link.push(t="mute", id="mid")
    mixer.tick()
    assert link.take()[0]["mute"] is False
    assert backend.mutes["mid"] is False


def test_apps_resent_on_set_or_name_change_only(env):
    mixer, backend, link, clock = env
    mixer.tick()
    link.take()
    backend.add(AppInfo("newest", "Newest", 400.0))
    clock.t += 2.0
    mixer.tick()
    assert [a["id"] for a in link.take("apps")[0]["apps"]] == ["newest", "new", "mid", "old"]
    backend.apps["old"].name = "Renamed"
    clock.t += 2.0
    mixer.tick()
    assert link.take("apps")[0]["apps"][-1]["name"] == "Renamed"
    clock.t += 2.0
    mixer.tick()
    assert link.take("apps") == []


def test_record_kept_when_app_disappears_and_returns(env):
    mixer, backend, link, clock = env
    mixer.tick()
    link.push(t="delta", id="old", d=-4)
    link.push(t="mute", id="old")
    mixer.tick()
    backend.remove("old")
    clock.t += 2.0
    mixer.tick()
    backend.add(AppInfo("old", "Old", 100.0))
    clock.t += 2.0
    link.take()
    mixer.tick()
    entry = [a for a in link.take("apps")[0]["apps"] if a["id"] == "old"][0]
    assert entry["vol"] == 80 and entry["mute"] is True


def test_backend_reported_volume_is_used_and_external_changes_sent():
    backend = FakeBackend(apps3(), report_volume=True)
    backend.volumes["new"] = 40
    link, clock = FakeLink(), Clock()
    mixer = Mixer(backend, link, clock=clock)
    mixer.tick()
    assert link.take("apps")[0]["apps"][0]["vol"] == 40
    backend.volumes["new"] = 70  # changed outside the daemon
    clock.t += 2.0
    mixer.tick()
    assert link.take("vol") == [{"t": "vol", "id": "new", "vol": 70, "mute": False}]
    link.push(t="delta", id="new", d=1)
    mixer.tick()
    assert link.take("vol")[0]["vol"] == 75


def test_cap_33_apps_and_name_truncation():
    apps = [AppInfo(f"a{i}", f"Application number {i:03d}", float(i)) for i in range(50)]
    link, clock = FakeLink(), Clock()
    mixer = Mixer(FakeBackend(apps, report_volume=False), link, clock=clock)
    mixer.tick()
    assert len(mixer.apps) == 33 and mixer.apps[0].id == "a49"
    sent = link.take("apps")[0]["apps"]
    assert sent[0]["id"] == "a49"
    assert all(len(a["name"]) <= 20 for a in sent)


def test_reconnect_triggers_immediate_ping(env):
    mixer, _, link, clock = env
    mixer.tick()
    link.take()
    clock.t += 0.5
    link.generation += 1
    mixer.tick()
    assert link.take() == [{"t": "ping"}]


def test_backend_errors_do_not_crash(env):
    mixer, backend, link, clock = env

    def boom(*a):
        raise RuntimeError("x")

    backend.set_volume = boom
    backend.list_apps = boom
    link.push(t="delta", id="new", d=1)
    mixer.tick()
    assert link.take("vol") == [{"t": "vol", "id": "new", "vol": 100, "mute": False}]


def test_load_backend():
    assert isinstance(load_backend("fake"), FakeBackend)
    with pytest.raises(ValueError):
        load_backend("nope")
