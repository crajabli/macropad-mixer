#!/usr/bin/env bash
# Copy the firmware to the MacroPad's CIRCUITPY drive and install its libraries.
# Usage: tools/deploy_firmware.sh [path-to-CIRCUITPY]   (default: /Volumes/CIRCUITPY on macOS)
# On Windows/Linux pass the drive path, e.g. tools/deploy_firmware.sh /media/$USER/CIRCUITPY
set -euo pipefail

DRIVE="${1:-/Volumes/CIRCUITPY}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "$DRIVE/boot_out.txt" ]]; then
  echo "CIRCUITPY drive not found at $DRIVE." >&2
  echo "The drive is hidden in normal use: hold KEY 1 (top-left, under the screen) while" >&2
  echo "plugging in the MacroPad or pressing RESET to show it." >&2
  exit 1
fi

boot_changed=1
cmp -s "$HERE/firmware/boot.py" "$DRIVE/boot.py" && boot_changed=0

cp "$HERE/firmware/boot.py" "$DRIVE/"
# Write code.py under a temporary name first: CircuitPython reloads on every write, and a
# reload on a half-written code.py crashes until the next reload.
cp "$HERE/firmware/code.py" "$DRIVE/code.tmp" && sync && mv "$DRIVE/code.tmp" "$DRIVE/code.py"

if command -v circup >/dev/null 2>&1; then
  circup --path "$DRIVE" install adafruit_macropad adafruit_display_text
else
  echo "circup not on PATH - skipping library install (pip install circup)" >&2
fi

# Put the installer on the drive so any computer can install the host side with a double-click.
mkdir -p "$DRIVE/mixer/host"
cp "$HERE/installer/HOW TO INSTALL.txt" "$HERE/installer/Install MacroPad Mixer (Mac).command" "$DRIVE/"
[[ -f "$HERE/installer/Install MacroPad Mixer (Windows).cmd" ]] && \
  cp "$HERE/installer/Install MacroPad Mixer (Windows).cmd" "$DRIVE/"
cp "$HERE/installer/install_macos.sh" "$DRIVE/mixer/"
[[ -f "$HERE/installer/install.ps1" ]] && cp "$HERE/installer/install.ps1" "$DRIVE/mixer/"
cp "$HERE/host/pyproject.toml" "$DRIVE/mixer/host/"
rm -rf "$DRIVE/mixer/host/macropad_mixer"
rsync -a --exclude '__pycache__' --exclude '*.egg-info' "$HERE/host/macropad_mixer" "$DRIVE/mixer/host/"

# macOS litters the drive with ._ metadata files; remove them.
command -v dot_clean >/dev/null 2>&1 && dot_clean -m "$DRIVE" || true
sync

echo "Firmware copied to $DRIVE."
if [[ $boot_changed == 1 ]]; then
  echo "boot.py changed: eject CIRCUITPY, then press the MacroPad's RESET button for it to take effect."
fi
