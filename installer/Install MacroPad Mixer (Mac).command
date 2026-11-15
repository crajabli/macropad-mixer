#!/bin/bash
# Double-click to install MacroPad Mixer on this Mac.
HERE="$(cd "$(dirname "$0")" && pwd)"
for script in "$HERE/mixer/install_macos.sh" "$HERE/install_macos.sh"; do
  if [[ -f "$script" ]]; then
    exec /bin/bash "$script"
  fi
done
echo "install_macos.sh not found next to this file." >&2
exit 1
