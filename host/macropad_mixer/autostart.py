"""Start the daemon automatically at login: `macropad-mixer autostart install|uninstall|status`.

- macOS: a LaunchAgent (~/Library/LaunchAgents) that launchd starts at login and restarts if it exits.
- Windows: a shortcut in the user's Startup folder running pythonw.exe (no console window).
- Linux: an XDG autostart entry (~/.config/autostart).

The installed command is the Python interpreter running this module, so it always points at the
environment (venv) the package is installed in. Extra daemon options can follow `install`, e.g.
`macropad-mixer autostart install --step 2`.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.github.macropad-mixer"


def default_log_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "macropad-mixer.log"
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "macropad-mixer" / "macropad-mixer.log"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "macropad-mixer.log"


def daemon_command(extra: list[str]) -> list[str]:
    python = Path(sys.executable)
    if sys.platform == "win32":  # pythonw.exe runs without a console window
        pythonw = python.with_name("pythonw.exe")
        python = pythonw if pythonw.exists() else python
    return [str(python), "-m", "macropad_mixer", "--log-file", str(default_log_file()), *extra]


# --- macOS ----------------------------------------------------------------------------------------

def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _install_macos(cmd: list[str]) -> str:
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": True,           # restart after a crash
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",  # low-latency knob handling
        # Only for startup crashes before logging is set up; normal logs go to --log-file.
        "StandardErrorPath": str(default_log_file().with_suffix(".stderr.log")),
    }
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # replace a previous install, if any
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    res = _launchctl("bootstrap", domain, str(path))
    if res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip() or res.returncode}")
    return f"Installed {path} and started it."


def _uninstall_macos() -> str:
    path = _plist_path()
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if path.exists():
        path.unlink()
        return f"Stopped and removed {path}."
    return "Not installed."


def _status_macos() -> str:
    if not _plist_path().exists():
        return "Not installed."
    res = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if res.returncode != 0:
        return f"Installed ({_plist_path()}) but not loaded; log in again or re-run install."
    state = next((ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("state =")), "")
    pid = next((ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("pid =")), "")
    return f"Installed and loaded ({state}{', ' + pid if pid else ''})."


# --- Windows --------------------------------------------------------------------------------------

def _shortcut_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "MacroPad Mixer.lnk"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _install_windows(cmd: list[str]) -> str:
    path = _shortcut_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    args = subprocess.list2cmdline(cmd[1:])
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(str(path)) + "); "
        "$s.TargetPath = " + _ps_quote(cmd[0]) + "; "
        "$s.Arguments = " + _ps_quote(args) + "; "
        "$s.WorkingDirectory = " + _ps_quote(str(Path.home())) + "; "
        "$s.Description = 'MacroPad per-app volume mixer'; "
        "$s.Save()"
    )
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"creating the shortcut failed: {res.stderr.strip() or res.returncode}")
    subprocess.Popen(cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))  # start now too
    return f"Installed {path} and started the daemon."


def _uninstall_windows() -> str:
    path = _shortcut_path()
    if path.exists():
        path.unlink()
        return f"Removed {path}. A daemon that is already running keeps running until you log out."
    return "Not installed."


def _status_windows() -> str:
    path = _shortcut_path()
    return f"Installed ({path})." if path.exists() else "Not installed."


# --- Linux ----------------------------------------------------------------------------------------

def _desktop_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "autostart" / "macropad-mixer.desktop"


def _install_linux(cmd: list[str]) -> str:
    path = _desktop_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join('"' + c.replace('"', '\\"') + '"' if " " in c else c for c in cmd)
    path.write_text(
        "[Desktop Entry]\nType=Application\nName=MacroPad Mixer\n"
        f"Exec={exec_line}\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n"
    )
    return f"Installed {path}. It starts at your next login."


def _uninstall_linux() -> str:
    path = _desktop_path()
    if path.exists():
        path.unlink()
        return f"Removed {path}."
    return "Not installed."


def _status_linux() -> str:
    return f"Installed ({_desktop_path()})." if _desktop_path().exists() else "Not installed."


# --- entry point ----------------------------------------------------------------------------------

def run(action: str, extra: list[str]) -> str:
    if sys.platform == "darwin":
        impl = {"install": lambda: _install_macos(daemon_command(extra)),
                "uninstall": _uninstall_macos, "status": _status_macos}
    elif sys.platform == "win32":
        impl = {"install": lambda: _install_windows(daemon_command(extra)),
                "uninstall": _uninstall_windows, "status": _status_windows}
    else:
        impl = {"install": lambda: _install_linux(daemon_command(extra)),
                "uninstall": _uninstall_linux, "status": _status_linux}
    msg = impl[action]()
    if action == "install":
        msg += f"\nLog file: {default_log_file()}"
    return msg
