"""Core daemon loop: polls the audio backend and talks to the MacroPad."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from . import protocol
from .backends.base import AppInfo, AudioBackend

log = logging.getLogger(__name__)

PING_INTERVAL = 2.0
APPS_REFRESH = 30.0
TICK = 0.02


class Link(Protocol):
    generation: int  # increments on every (re)connect

    def read_lines(self) -> list[bytes]: ...
    def send(self, data: bytes) -> bool: ...


@dataclass
class AppState:
    vol: int = 100
    mute: bool = False


class Mixer:
    def __init__(
        self,
        backend: AudioBackend,
        link: Link,
        step: int = 5,
        poll: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.link = link
        self.step = step
        self.poll = poll
        self.clock = clock
        self.state: dict[str, AppState] = {}  # kept even when apps disappear
        self.apps: list[AppInfo] = []
        self._signature: tuple[tuple[str, str], ...] | None = None
        self._last_poll = float("-inf")
        self._last_ping = float("-inf")
        self._last_apps_sent = float("-inf")
        self._generation = getattr(link, "generation", 0)

    # --- helpers -----------------------------------------------------------

    def _state(self, app_id: str) -> AppState:
        return self.state.setdefault(app_id, AppState())

    def _send(self, msg: dict) -> None:
        self.link.send(protocol.encode(msg))

    def send_apps(self) -> None:
        entries = []
        for a in self.apps:
            s = self._state(a.id)
            entries.append(protocol.app_entry(a.id, a.name, s.vol, s.mute))
        self.link.send(protocol.encode_apps(entries))
        self._last_apps_sent = self.clock()

    def _send_vol(self, app_id: str) -> None:
        s = self._state(app_id)
        self._send(protocol.vol_msg(app_id, s.vol, s.mute))

    # --- backend polling ---------------------------------------------------

    def refresh_apps(self) -> None:
        try:
            apps = self.backend.list_apps()
        except Exception:
            log.exception("backend.list_apps failed")
            return
        apps = sorted(apps, key=lambda a: (-a.started, a.name.lower()))[: protocol.MAX_APPS]
        changed_vol: list[str] = []
        for a in apps:
            s = self._state(a.id)
            new_vol = s.vol if a.vol is None else max(0, min(100, int(a.vol)))
            new_mute = s.mute if a.mute is None else bool(a.mute)
            if (new_vol, new_mute) != (s.vol, s.mute):
                s.vol, s.mute = new_vol, new_mute
                changed_vol.append(a.id)
        self.apps = apps
        signature = tuple((a.id, a.name[: protocol.MAX_NAME]) for a in apps)
        if signature != self._signature:
            self._signature = signature
            log.info("apps: %s", ", ".join(a.id for a in apps) or "(none)")
            self.send_apps()
        else:
            for app_id in changed_vol:  # external change (e.g. user moved a slider)
                self._send_vol(app_id)

    # --- device messages ---------------------------------------------------

    def handle(self, msg: dict) -> None:
        t = msg["t"]
        if t == "hello":
            log.info("device hello (fw %s)", msg.get("fw"))
            self.send_apps()
        elif t == "ping":  # not sent by v1 firmware, but answer it anyway
            self._send({"t": "pong"})
        elif t == "delta":
            s = self._state(msg["id"])
            s.vol = max(0, min(100, s.vol + msg["d"] * self.step))
            self._apply(lambda: self.backend.set_volume(msg["id"], s.vol))
            self._send_vol(msg["id"])
        elif t == "mute":
            s = self._state(msg["id"])
            s.mute = not s.mute
            self._apply(lambda: self.backend.set_mute(msg["id"], s.mute))
            self._send_vol(msg["id"])
        elif t == "sel":
            log.debug("selected %s", msg["id"])

    def _apply(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("backend call failed")

    # --- main loop ---------------------------------------------------------

    def tick(self) -> None:
        for line in self.link.read_lines():
            msg = protocol.decode(line)
            if msg is not None:
                self.handle(msg)
        gen = getattr(self.link, "generation", 0)
        if gen != self._generation:  # reconnected: ping right away to elicit hello
            self._generation = gen
            self._last_ping = float("-inf")
        now = self.clock()
        if now - self._last_poll >= self.poll:
            self._last_poll = now
            self.refresh_apps()
        if now - self._last_apps_sent >= APPS_REFRESH:
            self.send_apps()
        if now - self._last_ping >= PING_INTERVAL:
            self._last_ping = now
            self._send({"t": "ping"})

    def run(self) -> None:
        while True:
            self.tick()
            time.sleep(TICK)
