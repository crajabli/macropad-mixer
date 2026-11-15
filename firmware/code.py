# MacroPad Mixer - per-app volume control firmware (CircuitPython 9/10)
#
# Keys pick an app sent by the host daemon, the knob changes that app's volume,
# pressing the knob toggles mute. Protocol: docs/protocol.md (JSON lines over
# the USB CDC data port enabled in boot.py).
import binascii
import json
import time

import displayio
import terminalio
import usb_cdc
from adafruit_display_text import label
from adafruit_macropad import MacroPad
from rainbowio import colorwheel

FW_VERSION = "0.4.0"
NUM_KEYS = 12
HOST_TIMEOUT = 6.0  # seconds without any message -> host considered gone
MAX_LINE = 4096
DELTA_INTERVAL = 0.05  # batch knob clicks, send at most every 50 ms
LONG_PRESS = 1.5  # seconds a key is held to pin/unpin its app
NOTE_TIME = 2.0  # seconds a host note stays on the status line
IDLE_PIN_DIM = 0.3  # extra dimming for pinned apps that aren't running
DIM = 0.12  # brightness factor for unselected keys
NEXT_PAGE_COLOR = 0x202020
ICON_MAX = 32  # largest icon side we accept (px)
NAME_X = 36  # text column to the right of an icon
NAME_CHARS = (128 - NAME_X) // 6  # terminalio glyphs are 6 px wide

macropad = MacroPad()
macropad.pixels.auto_write = False
macropad.pixels.brightness = 0.4
port = usb_cdc.data

# ---------------------------------------------------------------- display ---

display = macropad.display
WIDTH = display.width

screen = displayio.Group()
icon_slot = displayio.Group(x=0, y=0)  # holds the selected app's icon TileGrid, if any
title = label.Label(terminalio.FONT, text="", scale=2, x=2, y=10)
subtitle = label.Label(terminalio.FONT, text="", x=2, y=32)
status = label.Label(terminalio.FONT, text="", x=2, y=56)

BAR_W, BAR_H, BAR_Y = WIDTH - 4, 6, 42
bar_bitmap = displayio.Bitmap(BAR_W, BAR_H, 2)
bar_palette = displayio.Palette(2)
bar_palette[0] = 0x000000
bar_palette[1] = 0xFFFFFF
bar = displayio.TileGrid(bar_bitmap, pixel_shader=bar_palette, x=2, y=BAR_Y)
bar.hidden = True

for item in (icon_slot, title, subtitle, bar, status):
    screen.append(item)
display.root_group = screen


def draw_bar(vol):
    """Outlined bar filled to vol percent."""
    fill = (BAR_W - 2) * vol // 100
    for x in range(BAR_W):
        for y in range(BAR_H):
            edge = y in (0, BAR_H - 1) or x in (0, BAR_W - 1)
            bar_bitmap[x, y] = 1 if edge or (1 <= x <= fill) else 0


def set_title(text, icon=None):
    """Show the app name; with an icon, the icon goes left and the name is small beside it."""
    while len(icon_slot):
        icon_slot.pop()
    if icon is not None:
        icon_slot.append(displayio.TileGrid(icon, pixel_shader=bar_palette))
        title.scale, title.x, title.y = 1, NAME_X, 8
        title.text = text if len(text) <= NAME_CHARS else text[: NAME_CHARS - 1] + "."
        subtitle.x, subtitle.y = NAME_X, 24
        return
    # No icon: scale 2 fits 10 characters on the 128 px display; longer names use scale 1.
    title.scale = 2 if len(text) <= 10 else 1
    title.x, title.y = 2, (10 if title.scale == 2 else 6)
    title.text = text
    subtitle.x, subtitle.y = 2, 32


def decode_icon(w, h, data):
    """Packed 1-bit rows (MSB first, rows padded to bytes) -> displayio.Bitmap."""
    row = (w + 7) // 8
    if not (0 < w <= ICON_MAX and 0 < h <= ICON_MAX) or len(data) < row * h:
        return None
    bmp = displayio.Bitmap(w, h, 2)
    for y in range(h):
        base = y * row
        for x in range(w):
            if data[base + x // 8] & (0x80 >> (x % 8)):
                bmp[x, y] = 1
    return bmp


# ------------------------------------------------------------------ state ---

apps = []  # [{"id", "name"}] most recent first, as sent by the host
state = {}  # id -> {"vol": int|None, "mute": bool}
icons = {}  # id -> displayio.Bitmap
colors = {}  # id -> 0xRRGGBB brand colour from the host
selected = None  # app id
page = 0
connected = False
last_rx = 0.0
rx_buf = bytearray()
pending_clicks = 0
last_delta_sent = 0.0
dirty = True
key_down = {}  # key -> monotonic time pressed
long_fired = set()  # keys whose long press already triggered
note_text = ""
note_until = 0.0


def layout():
    """Return (apps_per_page, paged) for the current app count."""
    if len(apps) <= NUM_KEYS:
        return NUM_KEYS, False
    return NUM_KEYS - 1, True  # last key becomes "next page"


def page_count():
    per, _ = layout()
    return max(1, (len(apps) + per - 1) // per)


def app_for_key(key):
    per, paged = layout()
    if paged and key == NUM_KEYS - 1:
        return "next"
    idx = page * per + key
    return apps[idx] if idx < len(apps) else None


def app_color(app_id):
    if app_id in colors:
        return colors[app_id]
    h = 0
    for ch in app_id:
        h = (h * 31 + ord(ch)) & 0xFFFF
    return colorwheel(h & 0xFF)


def scale(color, factor):
    r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    return (int(r * factor), int(g * factor), int(b * factor))


def find_app(app_id):
    for app in apps:
        if app is not None and app["id"] == app_id:
            return app
    return None


# ----------------------------------------------------------------- serial ---


def send(msg):
    if port is None or not port.connected:
        return
    try:
        port.write((json.dumps(msg) + "\n").encode())
    except Exception as err:  # host closed the port mid-write
        print("send failed:", err)


def read_messages():
    """Yield decoded JSON objects from complete lines received so far."""
    global rx_buf
    n = port.in_waiting
    if n:
        rx_buf.extend(port.read(n))
    while True:
        nl = rx_buf.find(b"\n")
        if nl < 0:
            if len(rx_buf) > MAX_LINE:  # garbage without newline; drop it
                rx_buf = bytearray()
            return
        line = bytes(rx_buf[:nl]).strip()
        rx_buf = rx_buf[nl + 1 :]
        if not line or len(line) > MAX_LINE:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print("bad json:", line[:60])
            continue
        if isinstance(msg, dict):
            yield msg


def handle(msg):
    global apps, selected, page, dirty, note_text, note_until
    kind = msg.get("t")
    if kind == "ping":
        send({"t": "pong"})
    elif kind == "apps":
        apps = []  # None entries are empty keys (slots around pinned apps)
        for a in msg.get("apps", [])[:33]:
            if not (isinstance(a, dict) and "id" in a):
                apps.append(None)
                continue
            app_id = str(a["id"])
            apps.append({
                "id": app_id,
                "name": str(a.get("name") or app_id)[:20],
                "pin": bool(a.get("pin")),
                "run": a.get("run", True) is not False,
            })
            state[app_id] = {"vol": a.get("vol"), "mute": bool(a.get("mute"))}
        if selected is not None and find_app(selected) is None:
            selected = None
        for cache in (icons, colors):  # forget icons/colours of apps that went away
            for app_id in list(cache):
                if find_app(app_id) is None:
                    del cache[app_id]
        page = min(page, page_count() - 1)
        dirty = True
    elif kind == "icon":
        app_id = str(msg.get("id"))
        if "b" in msg:
            try:
                bmp = decode_icon(int(msg["w"]), int(msg["h"]), binascii.a2b_base64(msg["b"]))
            except (KeyError, ValueError, TypeError) as err:
                print("bad icon:", err)
                bmp = None
            if bmp is not None:
                icons[app_id] = bmp
        if isinstance(msg.get("c"), int):
            colors[app_id] = msg["c"] & 0xFFFFFF
        dirty = True  # key colours may have changed
    elif kind == "note":
        note_text = str(msg.get("text", ""))[:21]
        note_until = time.monotonic() + NOTE_TIME
        dirty = True
    elif kind == "vol":
        app_id = msg.get("id")
        if app_id is not None:
            state[app_id] = {"vol": msg.get("vol"), "mute": bool(msg.get("mute"))}
            dirty = True


# ------------------------------------------------------------------ input ---


def on_key(key):
    global selected, page, dirty, pending_clicks
    target = app_for_key(key)
    if target == "next":
        page = (page + 1) % page_count()
    elif target is not None:
        if target["id"] != selected:
            pending_clicks = 0  # clicks belong to the previously selected app
        selected = target["id"]
        send({"t": "sel", "id": selected})
    dirty = True


def check_long_press(now):
    """Holding a key pins its app to that key (or unpins it); the host decides and replies."""
    for key, since in key_down.items():
        if key in long_fired or now - since < LONG_PRESS:
            continue
        long_fired.add(key)
        target = app_for_key(key)
        if target == "next":
            continue
        per, _ = layout()
        send({"t": "pin", "k": page * per + key, "id": target["id"] if target else None})


def flush_delta(now):
    global pending_clicks, last_delta_sent
    if pending_clicks and selected and now - last_delta_sent >= DELTA_INTERVAL:
        send({"t": "delta", "id": selected, "d": pending_clicks})
        pending_clicks = 0
        last_delta_sent = now


# ---------------------------------------------------------------- drawing ---


def render():
    # LEDs
    for key in range(NUM_KEYS):
        target = app_for_key(key) if connected else None
        if target == "next":
            macropad.pixels[key] = NEXT_PAGE_COLOR
        elif target is None:
            macropad.pixels[key] = 0
        else:
            muted = state.get(target["id"], {}).get("mute")
            color = 0xFF0000 if muted else app_color(target["id"])
            level = 1.0 if target["id"] == selected else DIM
            if not target["run"]:  # pinned but not running: keep the key, very dim
                level *= IDLE_PIN_DIM
            macropad.pixels[key] = scale(color, level)
    macropad.pixels.show()

    # OLED
    if port is None:
        set_title("No data port")
        subtitle.text = "Hard-reset the board"
        status.text = "(boot.py enables it)"
        bar.hidden = True
        return
    if not connected:
        set_title("MacroMixer")
        subtitle.text = "Waiting for host..."
        status.text = "fw " + FW_VERSION
        bar.hidden = True
        return

    pages = page_count()
    page_info = "  p%d/%d" % (page + 1, pages) if pages > 1 else ""
    running = sum(1 for a in apps if a is not None and a["run"])
    app = find_app(selected) if selected else None
    pin_info = "pinned | " if app is not None and app["pin"] else ""
    status.text = "%s%d apps%s" % (pin_info, running, page_info)
    if note_until:
        status.text = note_text
    if app is None:
        set_title("Pick an app" if apps else "No audio apps")
        subtitle.text = "Press a lit key" if apps else "Play something..."
        bar.hidden = True
        return
    set_title(app["name"], icons.get(app["id"]))
    if not app["run"]:
        subtitle.text = "Not running"
        bar.hidden = True
        return
    st = state.get(app["id"], {})
    vol = st.get("vol")
    if st.get("mute"):
        subtitle.text = "MUTED" + ("  (%d%%)" % vol if vol is not None else "")
    else:
        subtitle.text = "Volume %d%%" % vol if vol is not None else "Volume ?"
    draw_bar(vol if vol is not None else 0)
    bar.hidden = False


# ------------------------------------------------------------------- main ---

if port is not None:
    port.timeout = 0
    port.write_timeout = 0.1
    send({"t": "hello", "fw": FW_VERSION})

last_encoder = macropad.encoder

while True:
    now = time.monotonic()

    if port is not None:
        for msg in read_messages():
            last_rx = now
            if not connected:
                connected = True
                dirty = True
                send({"t": "hello", "fw": FW_VERSION})
            handle(msg)
        if connected and now - last_rx > HOST_TIMEOUT:
            connected = False
            dirty = True

    event = macropad.keys.events.get()
    if event:
        if event.pressed:
            key_down[event.key_number] = now
            if connected:
                on_key(event.key_number)
        else:
            key_down.pop(event.key_number, None)
            long_fired.discard(event.key_number)
    if connected:
        check_long_press(now)
    if note_until and now > note_until:
        note_until = 0.0
        dirty = True

    position = macropad.encoder
    if position != last_encoder:
        if connected and selected:
            pending_clicks += position - last_encoder
        last_encoder = position
    flush_delta(now)

    macropad.encoder_switch_debounced.update()
    if macropad.encoder_switch_debounced.pressed and connected and selected:
        send({"t": "mute", "id": selected})

    if dirty:
        render()
        dirty = False
