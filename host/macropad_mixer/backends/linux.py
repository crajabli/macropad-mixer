"""Linux backend: PulseAudio / PipeWire-pulse sink inputs grouped by application.name (pulsectl)."""

from __future__ import annotations

import logging
import os
import time

import pulsectl

from .base import AppInfo

log = logging.getLogger(__name__)


class LinuxBackend:
    def __init__(self) -> None:
        self._pulse: pulsectl.Pulse | None = None
        self._first_seen: dict[str, float] = {}

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

    def list_apps(self) -> list[AppInfo]:
        apps: dict[str, AppInfo] = {}
        for si in self._inputs():
            name = si.proplist["application.name"]
            started = self._started(si) or self._first_seen.setdefault(name, time.time())
            vol = round(si.volume.value_flat * 100)
            if name in apps:  # several streams: earliest start, loudest volume, muted only if all are
                a = apps[name]
                a.started = min(a.started, started)
                a.vol = max(a.vol or 0, vol)
                a.mute = a.mute and bool(si.mute)
            else:
                apps[name] = AppInfo(name, name, started, vol, bool(si.mute))
        return list(apps.values())

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
