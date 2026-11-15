import base64
import time

from PIL import Image, ImageDraw

from macropad_mixer.backends.base import AppInfo
from macropad_mixer.backends.fake import FakeBackend
from macropad_mixer.icons import ICON_SIZE, to_bits
from macropad_mixer.mixer import Mixer

from test_mixer import Clock, FakeLink


def sample_icon() -> Image.Image:
    img = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 120, 120), radius=24, fill=(120, 120, 120, 255))  # macOS-style plate
    d.ellipse((34, 34, 94, 94), fill=(255, 255, 255, 255))                       # the "logo"
    return img


def pixels(bits: bytes):
    row = (ICON_SIZE + 7) // 8
    return {(x, y) for y in range(ICON_SIZE) for x in range(ICON_SIZE)
            if bits[y * row + x // 8] & (0x80 >> (x % 8))}


def test_to_bits_draws_logo_outline_and_keeps_border_empty():
    bits = to_bits(sample_icon(), inset=0.07)
    assert len(bits) == ICON_SIZE * ICON_SIZE // 8
    on = pixels(bits)
    assert on, "logo outline should produce some pixels"
    assert not any(x in (0, ICON_SIZE - 1) or y in (0, ICON_SIZE - 1) for x, y in on)
    assert (ICON_SIZE // 2, ICON_SIZE // 2) not in on  # outline only, not a filled disc


def test_blank_icon_is_empty():
    assert not any(to_bits(Image.new("RGBA", (64, 64), (0, 0, 0, 0))))


class IconBackend(FakeBackend):
    icon_inset = 0.07

    def icon_image(self, app_id):
        return sample_icon() if app_id == "a" else None


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_mixer_sends_each_icon_once_per_connection():
    backend = IconBackend([AppInfo("a", "A", 2.0), AppInfo("b", "B", 1.0)], report_volume=False)
    link = FakeLink()
    mixer = Mixer(backend, link, clock=Clock())
    mixer.tick()
    wait_for(lambda: mixer.icons.ready("a") and mixer.icons.ready("b"))
    mixer.tick()
    icons = link.take("icon")
    assert [m["id"] for m in icons] == ["a"]  # "b" has no icon, so nothing is sent for it
    assert len(base64.b64decode(icons[0]["b"])) == ICON_SIZE * ICON_SIZE // 8
    mixer.tick()
    assert link.take("icon") == []            # not repeated
    link.push(t="hello", fw="x")              # device rebooted: send again
    mixer.tick()
    assert [m["id"] for m in link.take("icon")] == ["a"]
    mixer.icons.close()


def test_backends_without_icons_still_work():
    mixer = Mixer(FakeBackend([AppInfo("a", "A", 1.0)]), FakeLink(), clock=Clock())
    assert mixer.icons is None
    mixer.tick()


def test_dominant_color_picks_brand_colour_and_ignores_grey():
    from macropad_mixer.icons import dominant_color

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 63, 63), fill=(240, 240, 240, 255))  # white plate: not saturated
    d.ellipse((8, 8, 56, 56), fill=(30, 215, 96, 255))     # Spotify green
    c = dominant_color(img)
    r, g, b = c >> 16, (c >> 8) & 0xFF, c & 0xFF
    assert g == 255 and r < 80 and b < 160                  # bright green
    assert dominant_color(Image.new("RGBA", (16, 16), (128, 128, 128, 255))) is None


def test_color_loader_is_used_for_colour():
    from macropad_mixer.icons import IconCache

    green = Image.new("RGBA", (16, 16), (0, 200, 0, 255))
    cache = IconCache(lambda _id: sample_icon(), 0.07, color_loader=lambda _id: green)
    cache.get("a")
    wait_for(lambda: cache.ready("a"))
    bits, color = cache.get("a")
    assert bits and color == 0x00FF00
    cache.close()


def test_pinned_app_that_is_not_running_still_gets_its_icon_and_colour():
    # Regression: after a daemon restart, pinned apps that hadn't played yet lost logo + colour.
    from macropad_mixer.pins import Pins

    pins = Pins()
    pins.toggle(0, "a", "Alpha")
    backend = IconBackend([], report_volume=False)     # nothing playing
    link = FakeLink()
    mixer = Mixer(backend, link, clock=Clock(), pins=pins)
    mixer.tick()
    wait_for(lambda: mixer.icons.ready("a"))
    mixer.tick()
    assert [m["id"] for m in link.take("icon")] == ["a"]
    mixer.icons.close()


def test_icons_survive_a_restart_via_the_store(tmp_path):
    from macropad_mixer.icons import IconCache

    first = IconCache(lambda _id: sample_icon(), 0.07, store_dir=tmp_path)
    first.get("a")
    wait_for(lambda: first.ready("a"))
    saved = first.get("a")
    first.close()

    # Restarted daemon: the app can't be located now, but the saved look is used.
    second = IconCache(lambda _id: None, 0.07, store_dir=tmp_path)
    second.get("a")
    wait_for(lambda: second.ready("a"))
    assert second.get("a") == saved
    second.close()


def test_empty_result_is_retried_later():
    from macropad_mixer.icons import RETRY_EMPTY, IconCache

    clock = Clock()
    found = {"ok": False}
    cache = IconCache(lambda _id: sample_icon() if found["ok"] else None, 0.07, clock=clock)
    cache.get("a")
    wait_for(lambda: cache.ready("a"))
    assert cache.get("a") == (None, None)
    found["ok"] = True                         # e.g. the app got installed / located
    assert cache.get("a") == (None, None)      # not retried yet
    clock.t += RETRY_EMPTY + 1
    assert cache.get("a") is None              # retry scheduled
    wait_for(lambda: cache.ready("a"))
    assert cache.get("a")[0]
    cache.close()
