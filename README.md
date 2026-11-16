# MacroPad Mixer

A hardware per-app volume mixer for the **Adafruit MacroPad RP2040**, like the Windows
Volume Mixer on your desk, for macOS and Windows (Linux as a bonus).

- Keys 1-11 are automatically assigned apps that are playing audio, most recently started
  first. An unpinned app leaves its key after 60 s of silence (`--forget-after`); pinned apps
  stay. With more than 11 apps, key 11 flips pages.
- Key 12 is always screen brightness (built-in display and external monitors with DDC/CI):
  press it, then turn the knob. Press it again to switch between all displays and each one.
- Keys light up in each app's brand colour (taken from its icon).
- Press a key: the app's logo, name and volume appear on the OLED and the key lights up.
- Turn the knob: that app's volume changes in 5 % steps. Only that app, not the system volume.
- Press the knob: mute or unmute that app.
- Hold a key for 1.5 s: pin the app to that key, so it's always there (even when it isn't running,
  shown dimly).
- Hold a pinned key: it blinks; press another key to move the pin there (swapping with another
  pinned app), press the same key to unpin, or press the knob to cancel.

## How it works

```
MacroPad (CircuitPython)  <-- USB data serial, JSON lines -->  host daemon (Python)
  keys / knob / OLED                                            ├─ macOS:   FineTune URL scheme (Core Audio taps)
                                                                ├─ Windows: pycaw (WASAPI audio sessions)
                                                                └─ Linux:   pulsectl (PulseAudio / PipeWire)
```

- `firmware/` runs on the MacroPad. `boot.py` enables a second USB serial port for data so
  the normal console stays free for debugging.
- `host/` is the daemon that runs on the computer. It lists audio apps, sends them to the
  MacroPad, and applies volume changes. The device ↔ host protocol is in
  [`docs/protocol.md`](docs/protocol.md).

## Install (no commands needed)

The MacroPad carries its own installer. Its **CIRCUITPY** drive is hidden in normal use (so it
can be unplugged any time without ejecting), so **hold key 1, the top-left key right under the
screen, while plugging it in** to show the drive. Then open **CIRCUITPY** and double-click:

- **Mac:** `Install MacroPad Mixer (Mac).command`
- **Windows:** `Install MacroPad Mixer (Windows).cmd`

The installer (online, a minute or two) sets up a private Python and the mixer daemon, installs
[FineTune](https://github.com/ronitsingh10/FineTune) on Mac, and makes the daemon start at every
login. From then on the MacroPad just works whenever it's plugged in; on Mac the daemon also
starts FineTune when needed. The only click left is FineTune's one-time "allow system audio"
prompt on Mac.

No operating system runs software automatically when a USB device is plugged in (AutoRun was
removed for security), so this one double-click per computer is the minimum.

## Setup (developers / building a MacroPad)

### 1. MacroPad firmware

1. Install CircuitPython 9 or 10 on the MacroPad
   ([guide](https://learn.adafruit.com/adafruit-macropad-rp2040/circuitpython)).
2. Install `circup` (`pip install circup`) and run:

   ```sh
   tools/deploy_firmware.sh                 # macOS (uses /Volumes/CIRCUITPY)
   tools/deploy_firmware.sh /path/to/CIRCUITPY   # other OSes
   ```

   On Windows without bash, copy `firmware/boot.py` and `firmware/code.py` to the
   `CIRCUITPY` drive and run `circup install adafruit_macropad adafruit_display_text`.
3. Press the MacroPad's **RESET** button once so `boot.py` takes effect: it enables the data
   port and hides the CIRCUITPY drive. From then on, hold **key 1** while plugging in (or
   pressing RESET) whenever you need the drive, e.g. to deploy or edit the firmware.
   `tools/deploy_firmware.sh` also copies the installers and the daemon onto the drive.
   The OLED shows "Waiting for host...".

### 2. Host daemon

Requires Python 3.10+.

```sh
pip install -e "host"            # macOS
pip install -e "host[windows]"   # Windows
pip install -e "host[linux]"     # Linux
macropad-mixer -v                # try it in the terminal first (Ctrl-C to stop)
```

### 3. Start it automatically at login

```sh
macropad-mixer autostart install     # also starts it right away
macropad-mixer autostart status
macropad-mixer autostart uninstall
```

- macOS: a LaunchAgent that also restarts the daemon if it crashes.
  Log: `~/Library/Logs/macropad-mixer.log`.
- Windows: a "MacroPad Mixer" shortcut in your Startup folder (runs without a console window).
  Log: `%LOCALAPPDATA%\macropad-mixer\macropad-mixer.log`.
- Linux: an entry in `~/.config/autostart`.

Options after `install` are passed to the daemon, e.g. `macropad-mixer autostart install --step 2`.
Stop the autostarted daemon before running one in a terminal: only one program can use the serial port.

**macOS** has no built-in per-app volume API, so the daemon drives
[FineTune](https://github.com/ronitsingh10/FineTune) (free, open source, macOS 15+):
`brew install --cask finetune`, start it once and grant it "Screen & System Audio Recording".

## Status

Early prototype. See [`docs/protocol.md`](docs/protocol.md) for the protocol and the
roadmap on the design page.
