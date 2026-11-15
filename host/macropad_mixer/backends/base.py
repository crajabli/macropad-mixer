"""Backend interface shared by all OS audio backends (docs/protocol.md)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol


@dataclass
class AppInfo:
    id: str                 # macOS: bundle id; Windows: lower-cased exe; Linux: application name
    name: str               # friendly name
    started: float          # epoch seconds the app (process) started; used for recent-first order
    vol: int | None = None  # 0-100 if the backend can read it, else None
    mute: bool | None = None


class AudioBackend(Protocol):
    def list_apps(self) -> list[AppInfo]: ...          # apps that currently have audio
    def set_volume(self, app_id: str, vol: int) -> None: ...   # 0-100
    def set_mute(self, app_id: str, mute: bool) -> None: ...
    def close(self) -> None: ...
    # Optional (see icons.py): `icon_image(app_id) -> PIL.Image | None`, `icon_inset: float`, and
    # `color_image(app_id) -> PIL.Image | None` when the brand colour needs a different image.


BACKENDS = ("auto", "macos", "windows", "linux", "fake")


def load_backend(name: str = "auto") -> AudioBackend:
    """Instantiate a backend by name; "auto" picks one from sys.platform. Imports are lazy."""
    if name == "auto":
        if sys.platform == "darwin":
            name = "macos"
        elif sys.platform == "win32":
            name = "windows"
        elif sys.platform.startswith("linux"):
            name = "linux"
        else:
            raise RuntimeError(f"no audio backend for platform {sys.platform!r}")
    if name == "macos":
        from .macos import MacBackend
        return MacBackend()
    if name == "windows":
        from .windows import WindowsBackend
        return WindowsBackend()
    if name == "linux":
        from .linux import LinuxBackend
        return LinuxBackend()
    if name == "fake":
        from .fake import FakeBackend
        return FakeBackend()
    raise ValueError(f"unknown backend {name!r}; choose from {', '.join(BACKENDS)}")
