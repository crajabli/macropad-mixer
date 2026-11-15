# MacroPad Mixer — device ↔ host protocol (v1)

## Transport

- USB CDC **data** serial port of the MacroPad (enabled in `firmware/boot.py` with
  `usb_cdc.enable(console=True, data=True)`). The console port stays free for the REPL / debugging.
- Host finds the port with `adafruit_board_toolkit.circuitpython_serial.data_comports()`
  (interface name starts with `CircuitPython CDC2`). Baud rate is ignored by USB CDC; use 115200.
- Framing: **one UTF-8 JSON object per line**, terminated by `\n`. Max line length 4096 bytes (33 apps fit);
  longer lines are discarded by the receiver. Unknown `t` values and unknown fields are ignored
  (forward compatibility).
- Every message has a type field `t`.

## Concepts

- **App id** (`id`): stable string chosen by the host backend. macOS: bundle id
  (`com.spotify.client`). Windows: lower-cased exe name (`spotify.exe`). Linux: application name.
  The device treats it as opaque.
- **Volume** (`vol`): integer 0–100 (percent of that app's level, not the master).
- **Steps**: the knob reports raw detent clicks; the **host** converts clicks to volume
  (default 5 % per click) and is the single source of truth for volume. The device may show an
  optimistic value but must replace it with whatever the host sends back.
- **Order**: the host sends apps most-recently-started first. Index in the list = slot.
  The device maps slots to keys; with more than 12 apps it pages (see firmware section).

## Host → device

| `t` | Fields | Meaning |
|-----|--------|---------|
| `apps` | `apps`: list of `{id, name, vol, mute}` (max 33) | Full replacement of the app list. Sent on connect (after `hello`), whenever the set or order of apps changes, and at least every 30 s. |
| `bright` | `displays`: list of `{id, name, val}` | Displays whose brightness can be set (built-in panel, DDC/CI monitors); `val` 0-100 or `null` if unknown. Sent with every `apps`, when the set of displays or a value changes (also changes made outside the MacroPad), and after each `bdelta`. Empty list = no brightness control. |
| `note` | `text` (max 21 chars) | Short message the device shows on its status line for 2 s (e.g. "Pinned to key 3"). |
| `vol` | `id`, `vol`, `mute` | Current state of one app (reply to `delta`/`mute`, or external change). |
| `icon` | `id`, optional `w`, `h`, `b`, optional `c` | `c`: the app's brand colour for its key LED as an integer 0xRRGGBB. `w`/`h`/`b`: app icon as 1-bit pixels: `b` is base64 of packed rows (MSB = leftmost pixel, each row padded to whole bytes, 1 = lit). Currently 32×32 = 128 bytes. Sent once per app per connection (again after every `hello`), after the `apps` message that lists the app. Max side 32 px. Optional: apps without an icon get none. |
| `ping` | — | Heartbeat, every 2 s. |

`name` is a human label, max 20 chars (host truncates). `vol` may be `null` if unknown.

## Device → host

| `t` | Fields | Meaning |
|-----|--------|---------|
| `hello` | `fw` (firmware version string) | Sent on boot, and in reply to the first `ping` after the device considered itself disconnected. Host must reply with `apps`. |
| `sel` | `id` | A key was pressed and this app is now selected (informational). |
| `delta` | `id`, `d` (int, ± clicks, non-zero) | Knob turned while `id` selected. Device may batch clicks (send at most every 50 ms). |
| `mute` | `id` | Knob pressed: toggle mute of `id`. |
| `bdelta` | `id` (display id, or `"*"` for all), `d` (±clicks, non-zero) | Knob turned while key 12 (brightness) is selected. The host applies `d` × brightness step (default 5 %) to the display(s) and replies with `bright`. |
| `pin` | `k` (slot index), `id` (app on that key, or `null` if empty) | Pin request. Held key with an unpinned app: pin it there. Held key with a pinned app enters move mode on the device; the next key press sends `pin` with that key's slot and the moving app's id. The host: `id` already pinned on `k` → unpin; `id` pinned elsewhere → move it (an app pinned on `k` swaps into the old key); otherwise pin it on `k`. It saves the pins and replies with a `note` and a new `apps`. Only slots 0–10 (keys 1–11) can hold pins. |
| `pong` | — | Reply to `ping`. |

## Key 12: screen brightness

Key 12 is permanently the brightness key; it can't hold apps or pins. Pressing it selects
brightness (sun icon on the OLED); pressing it again cycles All displays → each display. The
knob sends `bdelta` for the shown target. Apps use keys 1-11; with more than 11 slots the device
pages with 10 apps per page and key 11 as "next page".

## Key layout and pins

The host decides which app sits on which key. Each `apps` entry is one slot, in key order;
an entry may be `null` (an empty key). Entries may carry:

- `pin: true` — the user pinned this app to this key (long-press on the MacroPad).
- `run: false` — pinned but not running: the key stays reserved, dimly lit, and the OLED
  shows "Not running".

Pinned apps take their keys; all other apps fill the free slots most-recent first. Pins are
saved by the host (`pins.json` in the user's app-config directory) so they survive restarts.
With more than 11 slots the device pages (keys 1–10 per page, key 11 = next page), so a pin on
key 11 moves to page 2 while paging is needed.

## Connection state

- Device considers the host **disconnected** if nothing was received for 6 s; it then shows
  "Waiting for host…" and turns key LEDs off, keeping the last list in memory.
- Host considers the device gone when the serial port errors/disappears; it reconnects by
  re-scanning ports every 2 s.

## Example session

```
D→H {"t":"hello","fw":"0.1.0"}
H→D {"t":"apps","apps":[{"id":"com.spotify.client","name":"Spotify","vol":60,"mute":false},{"id":"com.google.Chrome","name":"Chrome","vol":100,"mute":false}]}
D→H {"t":"sel","id":"com.spotify.client"}
D→H {"t":"delta","id":"com.spotify.client","d":-2}
H→D {"t":"vol","id":"com.spotify.client","vol":50,"mute":false}
D→H {"t":"mute","id":"com.spotify.client"}
H→D {"t":"vol","id":"com.spotify.client","vol":50,"mute":true}
H→D {"t":"ping"}
D→H {"t":"pong"}
```

## Host backend interface (Python)

Every OS backend implements this (in `host/macropad_mixer/backends/base.py`):

```python
@dataclass
class AppInfo:
    id: str                 # see "App id" above
    name: str               # friendly name
    started: float          # epoch seconds the app (process) started; used for recent-first order
    vol: int | None = None  # 0-100 if the backend can read it, else None
    mute: bool | None = None

class AudioBackend(Protocol):
    def list_apps(self) -> list[AppInfo]: ...          # apps that currently have audio
    def set_volume(self, app_id: str, vol: int) -> None: ...   # 0-100
    def set_mute(self, app_id: str, mute: bool) -> None: ...
    def close(self) -> None: ...
    # optional:
    icon_inset: float                               # fraction to crop off each side first
    def icon_image(self, app_id: str) -> "PIL.Image.Image | None": ...
    def app_alive(self, app_id: str) -> bool: ...  # is any process of this app still running?
```

### Keeping paused apps on their keys

`list_apps()` reports apps that are playing (or recently played) audio. When an app drops out of
that list, for example because it was paused, the daemon keeps it (same slot, last known name and
start time) as long as `backend.app_alive(app_id)` is true, i.e. until the app quits.
`app_alive` must be cheap (it is called every poll for each kept app), must not raise, and should
answer True for any process belonging to the app id (main process or helper), using the PIDs the
backend saw in `list_apps()`. Backends without `app_alive` fall back to keeping an app for
10 minutes after it was last listed.

If a backend cannot read volume/mute (macOS + FineTune), it returns `None` and the daemon
keeps its own record (initially 100 / unmuted).
