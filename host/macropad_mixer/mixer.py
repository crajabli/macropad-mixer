"""Core daemon loop: polls the audio backend and talks to the MacroPad."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Protocol

from . import protocol
from .backends.base import AppInfo, AudioBackend
from .icons import IconCache, icon_msg
from .displays.base import DisplayBackend, DisplayInfo, NoDisplays
from .pins import PINNABLE_KEYS, Pins

ALL_DISPLAYS = "*"

log = logging.getLogger(__name__)

PING_INTERVAL = 2.0
APPS_REFRESH = 30.0
FALLBACK_KEEP = 600.0  # backends without app_alive(): keep an app this long after it was listed
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
        pins: Pins | None = None,
        icon_store: Path | None = None,
        displays: DisplayBackend | None = None,
        bright_step: int = 5,
        forget_after: float = 60.0,
    ) -> None:
        self.forget_after = forget_after  # unpinned apps leave this long after their last sound
        self.backend = backend
        self.displays = displays if displays is not None else NoDisplays()
        self.bright_step = bright_step
        self.display_list: list[DisplayInfo] = []
        self.brightness: dict[str, int] = {}       # display id -> last known/set value 0-100
        self._bright_signature: tuple | None = None
        self.pins = pins if pins is not None else Pins()  # in-memory unless a file is given
        self.link = link
        self.step = step
        self.poll = poll
        self.clock = clock
        self.state: dict[str, AppState] = {}  # kept even when apps disappear
        self.apps: list[AppInfo] = []
        # Apps stay on their keys after they stop playing (e.g. paused) until they quit.
        self._seen: dict[str, AppInfo] = {}        # last known info per app id
        self._last_listed: dict[str, float] = {}   # clock() when the backend last listed it
        self._last_played: dict[str, float] = {}   # clock() when it was last making sound
        self._signature: tuple | None = None
        self._last_poll = float("-inf")
        self._last_ping = float("-inf")
        self._last_apps_sent = float("-inf")
        self._generation = getattr(link, "generation", 0)
        # Icons are optional: only backends with icon_image(app_id) -> PIL image provide them.
        loader = getattr(backend, "icon_image", None)
        self.icons = (IconCache(loader, getattr(backend, "icon_inset", 0.0),
                                getattr(backend, "color_image", None),
                                getattr(backend, "icon_keep_outline", False),
                                store_dir=icon_store) if loader else None)
        self._icons_sent: set[str] = set()  # per connection; the device forgets on reboot

    # --- helpers -----------------------------------------------------------

    def _state(self, app_id: str) -> AppState:
        return self.state.setdefault(app_id, AppState())

    def _send(self, msg: dict) -> None:
        self.link.send(protocol.encode(msg))

    def _entry(self, app_id: str, name: str) -> dict:
        s = self._state(app_id)
        return protocol.app_entry(app_id, name, s.vol, s.mute)

    def layout(self) -> list[dict | None]:
        """Key slots for the device: pinned apps on their keys (running or not), the other apps
        filling the free slots most-recent first. None = empty key."""
        running = {a.id: a for a in self.apps}
        slots: list[dict | None] = [None] * PINNABLE_KEYS
        for key, pin in self.pins.slots.items():
            app = running.get(pin["id"])
            entry = self._entry(pin["id"], app.name if app else pin["name"])
            entry["pin"] = True
            if app is None:
                entry["run"] = False
            slots[key] = entry
        pinned = {p["id"] for p in self.pins.slots.values()}
        rest = iter(a for a in self.apps if a.id not in pinned)
        for i, slot in enumerate(slots):
            if slot is None:
                app = next(rest, None)
                if app is None:
                    break
                slots[i] = self._entry(app.id, app.name)
        slots.extend(self._entry(a.id, a.name) for a in rest)
        while slots and slots[-1] is None:
            slots.pop()
        return slots[: protocol.MAX_APPS]

    def _layout_signature(self) -> tuple:
        return tuple((e["id"], e["name"], e.get("pin"), e.get("run", True)) if e else None
                     for e in self.layout())

    def send_apps(self) -> None:
        self.link.send(protocol.encode_apps(self.layout()))
        self._last_apps_sent = self.clock()
        self.send_brightness()

    def send_brightness(self) -> None:
        self._send({"t": "bright", "displays": [
            {"id": d.id, "name": d.name[: protocol.MAX_NAME], "val": self.brightness.get(d.id)}
            for d in self.display_list]})

    def refresh_displays(self) -> None:
        try:
            displays = self.displays.list_displays()
        except Exception:
            log.exception("display backend failed")
            return
        self.display_list = displays
        for d in displays:
            if d.brightness is not None:
                self.brightness[d.id] = max(0, min(100, int(d.brightness)))
        signature = tuple((d.id, d.name, self.brightness.get(d.id)) for d in displays)
        if signature != self._bright_signature:
            if [s[:2] for s in signature] != [s[:2] for s in self._bright_signature or ()]:
                log.info("displays: %s", ", ".join(f"{d.name} ({d.id})" for d in displays) or "(none)")
            self._bright_signature = signature
            self.send_brightness()

    def _change_brightness(self, display_id: str, clicks: int) -> None:
        targets = [d.id for d in self.display_list] if display_id == ALL_DISPLAYS else [display_id]
        for did in targets:
            if did not in {d.id for d in self.display_list}:
                continue
            value = max(0, min(100, self.brightness.get(did, 50) + clicks * self.bright_step))
            self.brightness[did] = value
            self._apply(lambda did=did, value=value: self.displays.set_brightness(did, value))
        self._bright_signature = tuple((d.id, d.name, self.brightness.get(d.id)) for d in self.display_list)
        self.send_brightness()

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
        apps = self._with_paused(apps)
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
        for a in apps:
            self.pins.rename(a.id, a.name)
        signature = self._layout_signature()
        if signature != self._signature:
            self._signature = signature
            log.info("apps: %s", ", ".join(a.id for a in apps) or "(none)")
            self.send_apps()
        else:
            for app_id in changed_vol:  # external change (e.g. user moved a slider)
                self._send_vol(app_id)

    def _with_paused(self, apps: list[AppInfo]) -> list[AppInfo]:
        """Decide which apps keep a key.

        - Unpinned apps stay until they've been silent for `forget_after` seconds.
        - Pinned apps stay while they're running (their key exists anyway; this keeps them
          showing as running rather than "Not running" while paused).
        An app that is no longer listed counts as running while backend.app_alive() says so
        (backends without it: FALLBACK_KEEP seconds after it was last listed).
        """
        now = self.clock()
        listed = {a.id: a for a in apps}
        for a in apps:
            self._seen[a.id] = a
            self._last_listed[a.id] = now
            if a.playing is not False:
                self._last_played[a.id] = now
        pinned = {p["id"] for p in self.pins.slots.values()}
        alive_fn = getattr(self.backend, "app_alive", None)
        keep: list[AppInfo] = []
        for app_id, info in list(self._seen.items()):
            if app_id in listed:
                running = True
            elif alive_fn is not None:
                try:
                    running = bool(alive_fn(app_id))
                except Exception:
                    log.debug("app_alive(%s) failed", app_id, exc_info=True)
                    running = False
            else:
                running = now - self._last_listed[app_id] < FALLBACK_KEEP
            recent = now - self._last_played.get(app_id, float("-inf")) < self.forget_after
            if running and (recent or app_id in pinned):
                # vol/mute None for apps not in this listing: keep the daemon's own record
                keep.append(listed.get(app_id) or AppInfo(info.id, info.name, info.started))
            elif not running:
                log.debug("app %s is gone", app_id)
                self._seen.pop(app_id, None)
                self._last_listed.pop(app_id, None)
                self._last_played.pop(app_id, None)
        return keep

    # --- device messages ---------------------------------------------------

    def handle(self, msg: dict) -> None:
        t = msg["t"]
        if t == "hello":
            log.info("device hello (fw %s)", msg.get("fw"))
            self._icons_sent.clear()
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
        elif t == "pin":
            self._toggle_pin(msg["k"], msg.get("id"))
        elif t == "bdelta":
            self._change_brightness(msg["id"], msg["d"])

    def _toggle_pin(self, key: int, app_id: str | None) -> None:
        name = next((a.name for a in self.apps if a.id == app_id), None)
        if app_id is not None and name is None:
            k = self.pins.key_of(app_id)
            name = self.pins.slots[k]["name"] if k is not None else app_id
        note = self.pins.toggle(key, app_id, name)
        log.info("%s (%s)", note, app_id)
        self._send({"t": "note", "text": note})
        self._signature = self._layout_signature()
        self.send_apps()

    def send_icons(self) -> None:
        """Send icons that finished loading for every app on a key (pinned apps that aren't
        running included) that the device hasn't received yet."""
        if self.icons is None:
            return
        on_keys = [a.id for a in self.apps] + [p["id"] for p in self.pins.slots.values()]
        for app_id in dict.fromkeys(on_keys):
            if app_id in self._icons_sent:
                continue
            entry = self.icons.get(app_id)
            if entry is not None:
                bits, color = entry
                if bits or color is not None:
                    self._icons_sent.add(app_id)
                    self._send(icon_msg(app_id, bits, color))

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
            self._icons_sent.clear()
        now = self.clock()
        if now - self._last_poll >= self.poll:
            self._last_poll = now
            self.refresh_apps()
            self.refresh_displays()
        if now - self._last_apps_sent >= APPS_REFRESH:
            self.send_apps()
        self.send_icons()
        if now - self._last_ping >= PING_INTERVAL:
            self._last_ping = now
            self._send({"t": "ping"})

    def run(self) -> None:
        while True:
            self.tick()
            time.sleep(TICK)
