"""Linux backend: PulseAudio / PipeWire-pulse sink inputs grouped by application.name (pulsectl)."""

from __future__ import annotations

import logging
import os
import time

import pulsectl

from .base import AppInfo

log = logging.getLogger(__name__)

_COMM_LEN = 15  # /proc/<pid>/comm holds at most TASK_COMM_LEN - 1 characters


def _proc_alive(pid: int, binary: str | None, proc_root: str = "/proc") -> bool:
    """True if `pid` is running and (when `binary` is known) is still that program.

    PIDs get reused, so the name is checked against /proc/<pid>/comm (truncated to 15 chars) or
    the basename of /proc/<pid>/exe. Never raises.
    """
    try:
        base = os.path.join(proc_root, str(pid))
        if not os.path.isdir(base):
            return False
        if not binary:
            return True
        binary = os.path.basename(binary)
        try:
            with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as f:
                if f.read().strip() == binary[:_COMM_LEN]:
                    return True
        except OSError:
            pass
        try:
            exe = os.readlink(os.path.join(base, "exe"))
        except OSError:
            return False
        exe = exe.removesuffix(" (deleted)")  # binary replaced by an upgrade while running
        return os.path.basename(exe) == binary
    except Exception:  # noqa: BLE001 - app_alive must never raise
        return False


class LinuxBackend:
    def __init__(self) -> None:
        self._pulse: pulsectl.Pulse | None = None
        self._first_seen: dict[str, float] = {}
        # app id -> {(pid, binary)} from the last listing that included the app. Entries are kept
        # after the app drops out of a listing so app_alive() can answer for it: many Linux apps
        # (Firefox, Chrome, Spotify) destroy their sink input when paused, but are still running.
        self._procs: dict[str, set[tuple[int, str | None]]] = {}
        self._proc_root = "/proc"

    def _conn(self) -> pulsectl.Pulse:
        if self._pulse is None:
            self._pulse = pulsectl.Pulse("macropad-mixer")
        return self._pulse

    def _inputs(self, app_id: str | None = None) -> list:
        try:
            inputs = self._conn().sink_input_list()
        except pulsectl.PulseError:
            self.close()  # reconnect next time
            inputs = self._conn().sink_input_list()
        return [si for si in inputs
                if "application.name" in si.proplist
                and (app_id is None or si.proplist["application.name"] == app_id)]

    @staticmethod
    def _started(si) -> float | None:
        pid = si.proplist.get("application.process.id")
        try:
            return os.stat(f"/proc/{int(pid)}").st_mtime if pid else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _proc_entry(si) -> tuple[int, str | None] | None:
        try:
            pid = int(si.proplist.get("application.process.id"))
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        return pid, si.proplist.get("application.process.binary") or None

    def list_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        procs: dict[str, set[tuple[int, str | None]]] = {}
        for si in self._inputs():
            name = si.proplist["application.name"]
            entry = self._proc_entry(si)
            procs.setdefault(name, set())
            if entry is not None:
                procs[name].add(entry)
            started = self._started(si) or self._first_seen.setdefault(name, time.time())
            vol = round(si.volume.value_flat * 100)
            if name in apps:  # several streams: earliest start, loudest volume, muted only if all are
                a = apps[name]
                a.started = min(a.started, started)
                a.vol = max(a.vol or 0, vol)
                a.mute = a.mute and bool(si.mute)
            else:
                apps[name] = AppInfo(name, name, started, vol, bool(si.mute))
        self._procs.update(procs)  # replace listed apps' sets, keep the rest for app_alive
        return list(apps.values())

    def app_alive(self, app_id: str) -> bool:
        """True while any process seen for `app_id` in list_apps() still runs. /proc only, no
        PulseAudio calls; never raises."""
        try:
            return any(_proc_alive(pid, binary, self._proc_root)
                       for pid, binary in self._procs.get(app_id, ()))
        except Exception:  # noqa: BLE001
            return False

    def set_volume(self, app_id: str, vol: int) -> None:
        for si in self._inputs(app_id):
            self._conn().volume_set_all_chans(si, max(0, min(100, vol)) / 100)

    def set_mute(self, app_id: str, mute: bool) -> None:
        for si in self._inputs(app_id):
            self._conn().mute(si, mute)

    def close(self) -> None:
        if self._pulse is not None:
            self._pulse.close()
            self._pulse = None
