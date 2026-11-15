"""Apps pinned to fixed keys, set by long-pressing a key on the MacroPad and saved to disk."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

PINNABLE_KEYS = 12  # only the first page's keys can hold pins


def default_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "macropad-mixer" / "pins.json"


class Pins:
    """key index (0-11) -> {"id": app id, "name": last known name}."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.slots: dict[int, dict[str, str]] = {}
        if path is not None:
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable pins file %s: %s", self.path, e)
            return
        for key, entry in (data.get("pins") or {}).items():
            try:
                k = int(key)
            except ValueError:
                continue
            if 0 <= k < PINNABLE_KEYS and isinstance(entry, dict) and isinstance(entry.get("id"), str):
                self.slots[k] = {"id": entry["id"], "name": str(entry.get("name") or entry["id"])}

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pins": {str(k): v for k, v in sorted(self.slots.items())}},
                                      indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save pins to %s: %s", self.path, e)

    def key_of(self, app_id: str) -> int | None:
        return next((k for k, v in self.slots.items() if v["id"] == app_id), None)

    def toggle(self, key: int, app_id: str | None, name: str | None) -> str:
        """Pin `app_id` to `key` (sent by the MacroPad after a long-press or a move).

        - `app_id` already pinned to `key`: unpin it.
        - `app_id` pinned elsewhere: move it; a different app pinned on `key` swaps into the
          old key.
        - otherwise: pin it to `key`, replacing any app pinned there.

        Returns a short message for the OLED.
        """
        if not 0 <= key < PINNABLE_KEYS:
            return "Pin on page 1 only"
        current = self.slots.get(key)
        if current is not None and (app_id is None or current["id"] == app_id):
            del self.slots[key]
            self.save()
            return f"Unpinned key {key + 1}"
        if app_id is None:
            return "No app to pin"
        old = self.key_of(app_id)
        entry = {"id": app_id, "name": name or app_id}
        if old is None:
            self.slots[key] = entry
            msg = f"Pinned to key {key + 1}"
        else:
            del self.slots[old]
            if current is not None:  # swap the other pinned app into the freed key
                self.slots[old] = current
                msg = f"Swapped keys {old + 1}+{key + 1}"
            else:
                msg = f"Moved to key {key + 1}"
            self.slots[key] = entry
        self.save()
        return msg

    def rename(self, app_id: str, name: str) -> None:
        """Remember the latest name so a pinned app that isn't running still shows it."""
        k = self.key_of(app_id)
        if k is not None and self.slots[k]["name"] != name:
            self.slots[k]["name"] = name
            self.save()
