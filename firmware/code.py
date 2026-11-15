# MacroPad Mixer - per-app volume control firmware (CircuitPython 9/10)
#
# Keys pick an app sent by the host daemon, the knob changes that app's volume,
# pressing the knob toggles mute. Protocol: docs/protocol.md (JSON lines over
# the USB CDC data port enabled in boot.py).
import json
import time

import displayio
import terminalio
import usb_cdc
from adafruit_display_text import label
from adafruit_macropad import MacroPad
from rainbowio import colorwheel

FW_VERSION = "0.1.0"
NUM_KEYS = 12
HOST_TIMEOUT = 6.0  # seconds without any message -> host considered gone
MAX_LINE = 1024
DELTA_INTERVAL = 0.05  # batch knob clicks, send at most every 50 ms
DIM = 0.12  # brightness factor for unselected keys
NEXT_PAGE_COLOR = 0x202020

macropad = MacroPad()
macropad.pixels.auto_write = False
macropad.pixels.brightness = 0.4
port = usb_cdc.data

# ---------------------------------------------------------------- display ---

display = macropad.display
WIDTH = display.width

screen = displayio.Group()
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

for item in (title, subtitle, bar, status):
    screen.append(item)
display.root_group = screen


def draw_bar(vol):
    """Outlined bar filled to vol percent."""
    fill = (BAR_W - 2) * vol // 100
    for x in range(BAR_W):
        for y in range(BAR_H):
            edge = y in (0, BAR_H - 1) or x in (0, BAR_W - 1)
            bar_bitmap[x, y] = 1 if edge or (1 <= x <= fill) else 0


def set_title(text):
    # Scale 2 fits 10 characters on the 128 px display; longer names use scale 1.
    title.scale = 2 if len(text) <= 10 else 1
    title.y = 10 if title.scale == 2 else 6
    title.text = text


# ------------------------------------------------------------------ state ---

apps = []  # [{"id", "name"}] most recent first, as sent by the host
state = {}  # id -> {"vol": int|None, "mute": bool}
selected = None  # app id
page = 0
connected = False
last_rx = 0.0
rx_buf = bytearray()
pending_clicks = 0
last_delta_sent = 0.0
dirty = True


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
    h = 0
    for ch in app_id:
        h = (h * 31 + ord(ch)) & 0xFFFF
    return colorwheel(h & 0xFF)


def scale(color, factor):
    r, g, b = (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF
    return (int(r * factor), int(g * factor), int(b * factor))


def find_app(app_id):
    for app in apps:
        if app["id"] == app_id:
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
    global apps, selected, page, dirty
    kind = msg.get("t")
    if kind == "ping":
        send({"t": "pong"})
    elif kind == "apps":
        apps = [
            {"id": str(a["id"]), "name": str(a.get("name") or a["id"])[:20]}
            for a in msg.get("apps", [])[:33]
            if isinstance(a, dict) and "id" in a
        ]
        for a in msg.get("apps", [])[:33]:
            if isinstance(a, dict) and "id" in a:
                state[str(a["id"])] = {"vol": a.get("vol"), "mute": bool(a.get("mute"))}
        if selected is not None and find_app(selected) is None:
            selected = None
        page = min(page, page_count() - 1)
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
            macropad.pixels[key] = scale(color, 1.0 if target["id"] == selected else DIM)
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
    status.text = "%d apps%s" % (len(apps), page_info)
    app = find_app(selected) if selected else None
    if app is None:
        set_title("Pick an app" if apps else "No audio apps")
        subtitle.text = "Press a lit key" if apps else "Play something..."
        bar.hidden = True
        return
    set_title(app["name"])
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
    if event and event.pressed and connected:
        on_key(event.key_number)

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
