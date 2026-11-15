#!/usr/bin/env bash
# Copy the firmware to the MacroPad's CIRCUITPY drive and install its libraries.
# Usage: tools/deploy_firmware.sh [path-to-CIRCUITPY]   (default: /Volumes/CIRCUITPY on macOS)
# On Windows/Linux pass the drive path, e.g. tools/deploy_firmware.sh /media/$USER/CIRCUITPY
set -euo pipefail

DRIVE="${1:-/Volumes/CIRCUITPY}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "$DRIVE/boot_out.txt" ]]; then
  echo "CIRCUITPY drive not found at $DRIVE" >&2
  exit 1
fi

boot_changed=1
cmp -s "$HERE/firmware/boot.py" "$DRIVE/boot.py" && boot_changed=0

cp "$HERE/firmware/boot.py" "$HERE/firmware/code.py" "$DRIVE/"

if command -v circup >/dev/null 2>&1; then
  circup --path "$DRIVE" install adafruit_macropad adafruit_display_text
else
  echo "circup not on PATH - skipping library install (pip install circup)" >&2
fi

# macOS litters the drive with ._ metadata files; remove them.
command -v dot_clean >/dev/null 2>&1 && dot_clean -m "$DRIVE" || true
sync

echo "Firmware copied to $DRIVE."
if [[ $boot_changed == 1 ]]; then
  echo "boot.py changed: press the MacroPad's RESET button (or unplug/replug) to enable the data port."
fi
