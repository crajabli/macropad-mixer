"""In-memory backend with a few fake apps, for running without real audio."""

from __future__ import annotations

import logging
import time

from .base import AppInfo

log = logging.getLogger(__name__)


class FakeBackend:
    def __init__(self, apps: list[AppInfo] | None = None, report_volume: bool = True) -> None:
        """report_volume=False mimics backends that can't read volume (they return None)."""
        now = time.time()
        if apps is None:
            apps = [
                AppInfo("com.spotify.client", "Spotify", now - 3600),
                AppInfo("com.google.Chrome", "Google Chrome", now - 600),
                AppInfo("us.zoom.xos", "zoom.us", now - 60),
            ]
        self.apps: dict[str, AppInfo] = {a.id: a for a in apps}
        self.report_volume = report_volume
        self.volumes: dict[str, int] = {}
        self.mutes: dict[str, bool] = {}
        self.closed = False

    def add(self, app: AppInfo) -> None:
        self.apps[app.id] = app

    def remove(self, app_id: str) -> None:
        self.apps.pop(app_id, None)

    def list_apps(self) -> list[AppInfo]:
        out = []
        for a in self.apps.values():
            vol = self.volumes.get(a.id, 100) if self.report_volume else None
            mute = self.mutes.get(a.id, False) if self.report_volume else None
            out.append(AppInfo(a.id, a.name, a.started, vol, mute))
        return out

    def set_volume(self, app_id: str, vol: int) -> None:
        log.info("fake: %s volume -> %d", app_id, vol)
        self.volumes[app_id] = vol

    def set_mute(self, app_id: str, mute: bool) -> None:
        log.info("fake: %s mute -> %s", app_id, mute)
        self.mutes[app_id] = mute

    def close(self) -> None:
        self.closed = True
