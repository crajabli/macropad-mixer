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
| `vol` | `id`, `vol`, `mute` | Current state of one app (reply to `delta`/`mute`, or external change). |
| `ping` | — | Heartbeat, every 2 s. |

`name` is a human label, max 20 chars (host truncates). `vol` may be `null` if unknown.

## Device → host

| `t` | Fields | Meaning |
|-----|--------|---------|
| `hello` | `fw` (firmware version string) | Sent on boot, and in reply to the first `ping` after the device considered itself disconnected. Host must reply with `apps`. |
| `sel` | `id` | A key was pressed and this app is now selected (informational). |
| `delta` | `id`, `d` (int, ± clicks, non-zero) | Knob turned while `id` selected. Device may batch clicks (send at most every 50 ms). |
| `mute` | `id` | Knob pressed: toggle mute of `id`. |
| `pong` | — | Reply to `ping`. |

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
```

If a backend cannot read volume/mute (macOS + FineTune), it returns `None` and the daemon
keeps its own record (initially 100 / unmuted).
