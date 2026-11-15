"""JSON-lines framing for the device <-> host protocol (docs/protocol.md, v1)."""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

MAX_LINE = 4096  # bytes, including the trailing newline
MAX_APPS = 33
MAX_NAME = 20

# Device -> host message types we understand; anything else is ignored.
# ("ping" isn't in v1's device->host table, but we answer it with "pong" if it arrives.)
DEVICE_TYPES = {"hello", "sel", "delta", "mute", "pong", "ping", "pin"}


def encode(msg: dict[str, Any]) -> bytes:
    """Encode one message as a UTF-8 JSON line. Raises ValueError if it exceeds MAX_LINE."""
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError(f"message too long ({len(data)} > {MAX_LINE} bytes)")
    return data


def app_entry(app_id: str, name: str, vol: int | None, mute: bool | None) -> dict[str, Any]:
    return {"id": app_id, "name": name[:MAX_NAME], "vol": vol, "mute": bool(mute)}


def encode_apps(apps: list[dict[str, Any] | None]) -> bytes:
    """Encode an `apps` message, dropping trailing apps until it fits MAX_LINE.

    The device discards lines over MAX_LINE, so a shorter list beats no list.
    """
    apps = apps[:MAX_APPS]
    while True:
        try:
            return encode({"t": "apps", "apps": apps})
        except ValueError:
            if not apps:
                raise
            apps = apps[:-1]
            log.debug("apps message too long, trimming to %d apps", len(apps))


def vol_msg(app_id: str, vol: int | None, mute: bool | None) -> dict[str, Any]:
    return {"t": "vol", "id": app_id, "vol": vol, "mute": bool(mute)}


def decode(line: bytes | str) -> dict[str, Any] | None:
    """Parse one received line. Returns None for over-long, malformed, unknown or invalid messages."""
    if isinstance(line, str):
        line = line.encode("utf-8")
    line = line.strip()
    if not line or len(line) >= MAX_LINE:
        return None
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.debug("bad line: %r", line)
        return None
    if not isinstance(msg, dict) or msg.get("t") not in DEVICE_TYPES:
        return None
    t = msg["t"]
    if t in ("sel", "delta", "mute") and not isinstance(msg.get("id"), str):
        return None
    if t == "pin":
        k = msg.get("k")
        if not isinstance(k, int) or isinstance(k, bool) or k < 0:
            return None
        if msg.get("id") is not None and not isinstance(msg.get("id"), str):
            return None
    if t == "delta":
        d = msg.get("d")
        if not isinstance(d, int) or isinstance(d, bool) or d == 0:
            return None
    return msg


class LineBuffer:
    """Accumulates raw bytes and yields complete lines, discarding lines over MAX_LINE."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._overflow = False  # currently skipping the rest of an over-long line

    def feed(self, data: bytes) -> list[bytes]:
        lines: list[bytes] = []
        self._buf += data
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if self._overflow:
                self._overflow = False
            elif len(line) + 1 <= MAX_LINE:
                lines.append(line)
        if len(self._buf) >= MAX_LINE:
            self._buf.clear()
            self._overflow = True
        return lines
