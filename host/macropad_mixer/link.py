"""Serial link to the MacroPad's USB CDC data port, with auto-detection and reconnect."""

from __future__ import annotations

import logging
import re
import time

import serial
from serial.tools import list_ports

from . import protocol

log = logging.getLogger(__name__)

ADAFRUIT_VID = 0x239A
MACROPAD_PID = 0x8108
RECONNECT_INTERVAL = 2.0


def _port_number(device: str) -> int:
    m = re.search(r"(\d+)$", device)
    return int(m.group(1)) if m else -1


def find_data_port() -> str | None:
    """Return the device path of the MacroPad's CDC data port, or None."""
    try:
        from adafruit_board_toolkit import circuitpython_serial

        ports = circuitpython_serial.data_comports()
        if ports:
            return ports[0].device
    except Exception as e:  # toolkit missing or failed; fall back to VID/PID
        log.debug("adafruit_board_toolkit lookup failed: %s", e)
    # Fallback: the data port enumerates after the console port, so take the highest number.
    ports = [p for p in list_ports.comports() if p.vid == ADAFRUIT_VID and p.pid == MACROPAD_PID]
    if not ports:
        return None
    return max(ports, key=lambda p: _port_number(p.device)).device


class SerialLink:
    """Non-blocking line-oriented serial link that reconnects every 2 s when lost.

    `generation` increments on every successful (re)connect so callers can detect it.
    """

    def __init__(self, port: str | None = None, clock=time.monotonic) -> None:
        self._fixed_port = port
        self._clock = clock
        self._ser: serial.Serial | None = None
        self._buf = protocol.LineBuffer()
        self._next_attempt = 0.0
        self.generation = 0

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def _try_open(self) -> None:
        now = self._clock()
        if now < self._next_attempt:
            return
        self._next_attempt = now + RECONNECT_INTERVAL
        port = self._fixed_port or find_data_port()
        if not port:
            log.debug("MacroPad data port not found")
            return
        try:
            self._ser = serial.Serial(port, 115200, timeout=0, write_timeout=1)
        except (serial.SerialException, OSError) as e:
            log.debug("cannot open %s: %s", port, e)
            return
        self._buf = protocol.LineBuffer()
        self.generation += 1
        log.info("connected to %s", port)

    def _drop(self, err: Exception) -> None:
        log.warning("serial link lost: %s", err)
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        self._next_attempt = self._clock() + RECONNECT_INTERVAL

    def read_lines(self) -> list[bytes]:
        """Return complete lines received since the last call (connects if needed)."""
        if self._ser is None:
            self._try_open()
            if self._ser is None:
                return []
        try:
            n = self._ser.in_waiting
            data = self._ser.read(n) if n else b""
        except (serial.SerialException, OSError) as e:
            self._drop(e)
            return []
        return self._buf.feed(data) if data else []

    def send(self, data: bytes) -> bool:
        if self._ser is None:
            return False
        try:
            self._ser.write(data)
            return True
        except (serial.SerialException, OSError) as e:
            self._drop(e)
            return False

    def close(self) -> None:
        if self._ser:
            self._ser.close()
            self._ser = None
