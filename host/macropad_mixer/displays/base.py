"""Display brightness backends (key 12 on the MacroPad).

Each OS backend lists the connected displays and sets their brightness. Built-in panels and
external monitors (via DDC/CI) are both "displays"; a display whose brightness can't be
controlled is simply not listed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol


@dataclass
class DisplayInfo:
    id: str                       # stable while the display stays connected, e.g. "builtin", "ddc:1234"
    name: str                     # human name for the OLED (max ~20 chars), e.g. "Built-in", "DELL U2720Q"
    brightness: int | None = None  # 0-100, None if it can't be read right now


class DisplayBackend(Protocol):
    def list_displays(self) -> list[DisplayInfo]: ...
    # 0-100. May return before the change lands (DDC writes are slow), but must coalesce rapid
    # calls (knob spinning): only the latest value per display matters.
    def set_brightness(self, display_id: str, value: int) -> None: ...
    def close(self) -> None: ...


class NoDisplays:
    """Fallback when the platform has no display backend."""

    def list_displays(self) -> list[DisplayInfo]:
        return []

    def set_brightness(self, display_id: str, value: int) -> None:
        pass

    def close(self) -> None:
        pass


def load_display_backend() -> DisplayBackend:
    """Pick the display backend for this OS; never raises (falls back to NoDisplays)."""
    import logging

    log = logging.getLogger(__name__)
    try:
        if sys.platform == "darwin":
            from .macos import MacDisplays
            return MacDisplays()
        if sys.platform == "win32":
            from .windows import WindowsDisplays
            return WindowsDisplays()
    except Exception:
        log.warning("display brightness control unavailable", exc_info=True)
    return NoDisplays()
