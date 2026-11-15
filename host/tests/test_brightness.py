from macropad_mixer import protocol
from macropad_mixer.backends.fake import FakeBackend
from macropad_mixer.displays.base import DisplayInfo
from macropad_mixer.mixer import Mixer

from test_mixer import Clock, FakeLink


class FakeDisplays:
    def __init__(self):
        self.displays = [DisplayInfo("builtin", "Built-in", 40), DisplayInfo("ddc:1", "DELL U2720Q", None)]
        self.set: list[tuple[str, int]] = []

    def list_displays(self):
        return list(self.displays)

    def set_brightness(self, display_id, value):
        self.set.append((display_id, value))

    def close(self):
        pass


def make():
    displays = FakeDisplays()
    link, clock = FakeLink(), Clock()
    mixer = Mixer(FakeBackend([]), link, clock=clock, displays=displays, bright_step=5)
    mixer.tick()
    return mixer, displays, link, clock


def test_displays_are_sent_with_known_brightness():
    _, _, link, _ = make()
    msg = link.take("bright")[-1]
    assert msg["displays"] == [{"id": "builtin", "name": "Built-in", "val": 40},
                               {"id": "ddc:1", "name": "DELL U2720Q", "val": None}]


def test_knob_changes_one_display_and_replies():
    mixer, displays, link, _ = make()
    link.take()
    link.push(t="bdelta", id="builtin", d=3)
    mixer.tick()
    assert displays.set == [("builtin", 55)]
    assert link.take("bright")[-1]["displays"][0]["val"] == 55


def test_all_displays_move_together_and_clamp():
    mixer, displays, link, _ = make()
    link.push(t="bdelta", id="*", d=-20)
    mixer.tick()
    assert displays.set == [("builtin", 0), ("ddc:1", 0)]  # unknown external starts from 50


def test_external_brightness_change_is_forwarded():
    mixer, displays, link, clock = make()
    link.take()
    displays.displays[0] = DisplayInfo("builtin", "Built-in", 80)  # user pressed F2
    clock.t += 2.1
    mixer.tick()
    assert link.take("bright")[-1]["displays"][0]["val"] == 80


def test_unknown_display_is_ignored_and_bad_messages_rejected():
    mixer, displays, link, _ = make()
    link.push(t="bdelta", id="nope", d=1)
    mixer.tick()
    assert displays.set == []
    assert protocol.decode(b'{"t":"bdelta","id":"*","d":0}') is None
    assert protocol.decode(b'{"t":"bdelta","d":1}') is None
