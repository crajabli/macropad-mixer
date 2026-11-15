from macropad_mixer import protocol
from macropad_mixer.backends.base import AppInfo
from macropad_mixer.backends.fake import FakeBackend
from macropad_mixer.mixer import Mixer
from macropad_mixer.pins import Pins

from test_mixer import Clock, FakeLink


def apps():  # most recent first after sorting: c, b, a
    return [AppInfo("a", "Alpha", 1.0), AppInfo("b", "Beta", 2.0), AppInfo("c", "Gamma", 3.0)]


def make(pins=None):
    backend = FakeBackend(apps(), report_volume=False)
    link, clock = FakeLink(), Clock()
    mixer = Mixer(backend, link, clock=clock, pins=pins or Pins())
    mixer.tick()
    link.take()
    return mixer, backend, link, clock


def ids(entries):
    return [e and e["id"] for e in entries]


def test_long_press_pins_app_to_key_and_others_fill_around_it():
    mixer, _, link, _ = make()
    link.push(t="pin", k=4, id="a")
    mixer.tick()
    sent = link.take()
    assert {"t": "note", "text": "Pinned to key 5"} in sent
    layout = [m for m in sent if m["t"] == "apps"][-1]["apps"]
    assert ids(layout) == ["c", "b", None, None, "a"]
    assert layout[4]["pin"] is True and "run" not in layout[4]


def test_pinned_app_that_quits_keeps_a_dim_slot_with_its_name():
    mixer, backend, link, clock = make()
    link.push(t="pin", k=0, id="a")
    mixer.tick()
    backend.remove("a")
    mixer.backend.app_alive = lambda _id: False
    clock.t += 3
    link.take()
    mixer.tick()
    layout = link.take("apps")[0]["apps"]
    assert layout[0] == {"id": "a", "name": "Alpha", "vol": 100, "mute": False, "pin": True, "run": False}
    assert ids(layout) == ["a", "c", "b"]


def test_long_press_again_unpins():
    mixer, _, link, _ = make()
    link.push(t="pin", k=4, id="a")
    mixer.tick()
    link.push(t="pin", k=4, id="a")
    mixer.tick()
    sent = link.take()
    assert {"t": "note", "text": "Unpinned key 5"} in sent
    assert ids([m for m in sent if m["t"] == "apps"][-1]["apps"]) == ["c", "b", "a"]


def test_pinning_moves_an_app_and_replaces_the_previous_pin():
    pins = Pins()
    pins.toggle(0, "a", "Alpha")
    pins.toggle(2, "a", "Alpha")          # moves
    assert pins.slots == {2: {"id": "a", "name": "Alpha"}}
    pins.toggle(2, "b", "Beta")           # replaces
    assert pins.slots == {2: {"id": "b", "name": "Beta"}}
    assert pins.toggle(12, "a", "Alpha") == "Pin on page 1 only"
    assert pins.toggle(5, None, None) == "No app to pin"


def test_pins_persist(tmp_path):
    path = tmp_path / "sub" / "pins.json"
    Pins(path).toggle(3, "com.spotify.client", "Spotify")
    assert Pins(path).slots == {3: {"id": "com.spotify.client", "name": "Spotify"}}
    path.write_text("not json")
    assert Pins(path).slots == {}         # unreadable file is ignored, not fatal


def test_decode_pin_messages():
    assert protocol.decode(b'{"t":"pin","k":3,"id":"a"}') == {"t": "pin", "k": 3, "id": "a"}
    assert protocol.decode(b'{"t":"pin","k":3,"id":null}') is not None
    assert protocol.decode(b'{"t":"pin","k":-1,"id":"a"}') is None
    assert protocol.decode(b'{"t":"pin","k":true,"id":"a"}') is None
    assert protocol.decode(b'{"t":"pin","k":1,"id":5}') is None
