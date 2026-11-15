#!/bin/bash
# MacroPad Mixer installer for macOS. Started by "Install MacroPad Mixer (Mac).command".
#
# 1. Installs uv (a small Python manager) if needed, which brings its own Python.
# 2. Copies the daemon to ~/Library/Application Support/macropad-mixer/app and installs it.
# 3. Installs FineTune (per-app volume engine) if it isn't installed.
# 4. Sets the daemon to start at every login, and starts it now.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/macropad-mixer/app"
FINETUNE_DMG_URL="https://github.com/ronitsingh10/FineTune/releases/latest/download/FineTune.dmg"

say() { printf '\n==> %s\n' "$1"; }
dialog() {  # dialog <message> [icon: note|caution|stop]; MACROPAD_MIXER_QUIET=1 skips (tests)
  [[ "${MACROPAD_MIXER_QUIET:-}" == 1 ]] && return 0
  osascript -e "display dialog \"$1\" with title \"MacroPad Mixer\" buttons {\"OK\"} default button 1 with icon ${2:-note}" >/dev/null 2>&1 || true
}
fail() {
  echo "ERROR: $1" >&2
  dialog "Installation failed: $1" stop
  exit 1
}
trap 'fail "step failed on line $LINENO (see the Terminal window for details)"' ERR

# --- find the daemon package: on the MacroPad drive it's mixer/host, in the repo ../host --------
HOST_SRC=""
for candidate in "$HERE/host" "$HERE/../host"; do
  if [[ -f "$candidate/pyproject.toml" && -d "$candidate/macropad_mixer" ]]; then
    HOST_SRC="$(cd "$candidate" && pwd)"
    break
  fi
done
[[ -n "$HOST_SRC" ]] || fail "could not find the macropad_mixer package next to the installer"

# --- macOS version: FineTune needs macOS 15 or later ------------------------------------------
MACOS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
(( MACOS_MAJOR >= 15 )) || fail "macOS 15 (Sequoia) or later is required; this Mac runs $(sw_vers -productVersion)."

# --- 1. uv --------------------------------------------------------------------------------------
say "Checking for uv"
UV="$(command -v uv || true)"
[[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"
if [[ -z "$UV" ]]; then
  say "Installing uv (Python manager) into ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$HOME/.local/bin/uv"
fi
[[ -x "$UV" ]] || fail "uv could not be installed"

# --- 2. daemon ----------------------------------------------------------------------------------
say "Stopping a running MacroPad Mixer (if any)"
launchctl bootout "gui/$(id -u)/com.github.macropad-mixer" 2>/dev/null || true
pkill -f "macropad_mixer" 2>/dev/null || true

say "Installing MacroPad Mixer into $APP_DIR"
mkdir -p "$APP_DIR"
# Run from the app dir: started from the MacroPad drive, its code.py would shadow Python's
# standard `code` module.
cd "$APP_DIR"
rm -rf "$APP_DIR/host"
mkdir -p "$APP_DIR/host"
cp "$HOST_SRC/pyproject.toml" "$APP_DIR/host/"
rsync -a --exclude '__pycache__' --exclude '*.egg-info' "$HOST_SRC/macropad_mixer" "$APP_DIR/host/"
rm -rf "$APP_DIR/.venv"
"$UV" venv --quiet --python 3.12 "$APP_DIR/.venv"
# --reinstall-package: the version number rarely changes, and uv would reuse a cached build.
"$UV" pip install --quiet --python "$APP_DIR/.venv/bin/python" --reinstall-package macropad-mixer "$APP_DIR/host"

# --- 3. FineTune --------------------------------------------------------------------------------
FINETUNE=""
for app in "/Applications/FineTune.app" "$HOME/Applications/FineTune.app"; do
  [[ -d "$app" ]] && FINETUNE="$app"
done
if [[ -z "$FINETUNE" ]]; then
  say "Downloading FineTune"
  TMP="$(mktemp -d)"
  curl -fL --progress-bar -o "$TMP/FineTune.dmg" "$FINETUNE_DMG_URL"
  hdiutil attach -nobrowse -quiet -mountpoint "$TMP/mnt" "$TMP/FineTune.dmg"
  SRC_APP="$(find "$TMP/mnt" -maxdepth 1 -name '*.app' | head -1)"
  [[ -n "$SRC_APP" ]] || { hdiutil detach -quiet "$TMP/mnt" || true; fail "FineTune.app not found in the download"; }
  if [[ -w /Applications ]]; then DEST=/Applications; else DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
  say "Installing FineTune into $DEST"
  ditto "$SRC_APP" "$DEST/FineTune.app"
  hdiutil detach -quiet "$TMP/mnt" || true
  rm -rf "$TMP"
  FINETUNE="$DEST/FineTune.app"
  FIRST_FINETUNE=1
else
  say "FineTune is already installed ($FINETUNE)"
  FIRST_FINETUNE=0
fi
open -g "$FINETUNE"

# --- 4. autostart -------------------------------------------------------------------------------
say "Setting MacroPad Mixer to start at login"
"$APP_DIR/.venv/bin/python" -m macropad_mixer autostart install

trap - ERR
MSG="MacroPad Mixer is installed and running. It starts automatically every time you log in, and the MacroPad works as soon as it is plugged in."
if [[ "$FIRST_FINETUNE" == 1 ]]; then
  MSG="$MSG\n\nOne last step: FineTune (the app that changes each app's volume) will ask for permission to access system audio. Click Allow (System Settings > Privacy & Security > Screen & System Audio Recording)."
fi
dialog "$MSG"
say "Done. You can close this window."
