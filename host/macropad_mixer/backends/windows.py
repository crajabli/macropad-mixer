"""Windows backend: per-app volume via WASAPI audio sessions (pycaw + comtypes).

Design notes
------------
* Sessions are enumerated on **every active render device** (not just the default
  one, which is all ``pycaw.AudioUtilities.GetAllSessions`` covers), the same way
  deej does: ``IMMDeviceEnumerator.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)``
  -> ``IMMDevice.Activate(IAudioSessionManager2)`` -> ``GetSessionEnumerator()``.
* Sessions are grouped by app id = lower-cased exe name (``spotify.exe``). One app
  may own many sessions (Chrome, Discord, the same app on two devices);
  ``set_volume`` / ``set_mute`` apply to every session in the group. ``list_apps``
  reports the group's **max** volume and ``mute = all sessions muted``.
* Expired sessions are skipped; Inactive ones are kept so paused apps stay on keys.
* The system-sounds session (PID 0 / ``IsSystemSoundsSession``) is skipped by
  default. Pass ``include_system_sounds=True`` to expose it as id ``"system"``,
  name ``"System sounds"``.
* Friendly name (EarTrumpet's order): session DisplayName if set and not an
  ``@%SystemRoot%...`` indirect resource string; else the exe's version-info
  FileDescription (needs pywin32; optional); else the exe name without ``.exe``,
  title-cased. Cached per exe path.
* COM: every public method first initialises COM on the calling thread (tracked in
  a ``threading.local``). COM interface pointers are apartment-bound, so the
  session cache is also per thread: a worker thread that calls ``set_volume``
  enumerates its own sessions once and reuses them afterwards.
* Simple polling: ``list_apps`` re-enumerates every call; no notification callbacks.

Everything Windows-specific (pycaw, comtypes, win32api) is imported lazily, so this
module -- and the pure helpers at the top, which the tests exercise -- import fine on
macOS/Linux.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .base import AppInfo

log = logging.getLogger(__name__)

SYSTEM_SOUNDS_ID = "system"
SYSTEM_SOUNDS_NAME = "System sounds"

# AudioSessionState values (pycaw.constants.AudioSessionState); duplicated here so
# the pure helpers don't need pycaw.
STATE_INACTIVE = 0
STATE_ACTIVE = 1
STATE_EXPIRED = 2


# --------------------------------------------------------------------------- #
# Pure helpers (no Windows imports; unit-tested on any OS)
# --------------------------------------------------------------------------- #


def app_id_for_exe(exe_name: str) -> str:
    """App id for an exe: its lower-cased basename (``C:\\x\\Spotify.exe`` -> ``spotify.exe``)."""
    base = exe_name.replace("/", "\\").rsplit("\\", 1)[-1]
    return base.strip().lower()


def is_usable_display_name(display_name: str | None) -> bool:
    """True if a session DisplayName can be shown as-is.

    Empty/whitespace names and indirect resource strings such as
    ``@%SystemRoot%\\System32\\AudioSrv.Dll,-202`` are rejected.
    """
    if not display_name or not display_name.strip():
        return False
    return not display_name.strip().startswith("@")


def fallback_name(exe_name: str) -> str:
    """``spotify.exe`` -> ``Spotify``; ``C:\\x\\msedge.EXE`` -> ``Msedge``."""
    base = exe_name.replace("/", "\\").rsplit("\\", 1)[-1].strip()
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.title() if base else exe_name


def choose_display_name(
    session_display_names: Iterable[str | None],
    file_description: str | None,
    exe_name: str,
) -> str:
    """EarTrumpet-style fallback: session DisplayName -> FileDescription -> exe name."""
    for dn in session_display_names:
        if is_usable_display_name(dn):
            return dn.strip()  # type: ignore[union-attr]
    if file_description and file_description.strip():
        return file_description.strip()
    return fallback_name(exe_name)


@dataclass
class SessionRecord:
    """Everything we read from one WASAPI session (plus its process)."""

    app_id: str
    exe_name: str
    pid: int
    exe_path: str | None = None
    display_name: str | None = None
    volume: float | None = None  # 0.0 - 1.0 scalar
    muted: bool | None = None
    started: float | None = None  # process create_time (epoch seconds)
    state: int = STATE_ACTIVE
    handle: Any = None  # ISimpleAudioVolume (opaque to the pure helpers)


@dataclass
class AppGroup:
    app_id: str
    sessions: list[SessionRecord] = field(default_factory=list)

    @property
    def handles(self) -> list[Any]:
        return [s.handle for s in self.sessions if s.handle is not None]


def group_sessions(records: Iterable[SessionRecord]) -> dict[str, AppGroup]:
    """Group session records by app id, dropping expired sessions. Order = first seen."""
    groups: dict[str, AppGroup] = {}
    for rec in records:
        if rec.state == STATE_EXPIRED:
            continue
        groups.setdefault(rec.app_id, AppGroup(rec.app_id)).sessions.append(rec)
    return groups


def volume_to_percent(scalar: float) -> int:
    return max(0, min(100, round(scalar * 100)))


def summarize_group(
    group: AppGroup,
    name_for: Callable[[AppGroup], str] | None = None,
) -> AppInfo:
    """Collapse a group into one AppInfo: max volume, mute only if every session is muted,
    earliest process start time."""
    vols = [s.volume for s in group.sessions if s.volume is not None]
    mutes = [s.muted for s in group.sessions if s.muted is not None]
    starts = [s.started for s in group.sessions if s.started is not None]
    if name_for is not None:
        name = name_for(group)
    else:
        first = group.sessions[0] if group.sessions else None
        name = choose_display_name(
            (s.display_name for s in group.sessions),
            None,
            first.exe_name if first else group.app_id,
        )
    return AppInfo(
        id=group.app_id,
        name=name,
        started=min(starts) if starts else 0.0,
        vol=volume_to_percent(max(vols)) if vols else None,
        mute=all(mutes) if mutes else None,
    )


def image_from_black_white(black: bytes, white: bytes, size: int):
    """Recover an RGBA image from the same icon drawn on black and on white (BGRA rows).

    Where the icon is opaque both renders agree; where it is transparent they differ by the
    background, so alpha = 255 - (white - black), and the colour is the black render
    un-premultiplied. This sidesteps GDI's unreliable alpha channel.
    """
    from PIL import Image

    out = bytearray(size * size * 4)
    for i in range(0, size * size * 4, 4):
        bb, bg, br = black[i], black[i + 1], black[i + 2]
        diff = (white[i] - bb) + (white[i + 1] - bg) + (white[i + 2] - br)
        a = max(0, min(255, 255 - round(diff / 3)))
        if a:
            out[i] = min(255, br * 255 // a)
            out[i + 1] = min(255, bg * 255 // a)
            out[i + 2] = min(255, bb * 255 // a)
            out[i + 3] = a
    return Image.frombytes("RGBA", (size, size), bytes(out))


# --------------------------------------------------------------------------- #
# Windows-only helpers (lazy imports)
# --------------------------------------------------------------------------- #


def _render_exe_icon(exe_path: str, size: int = 128):
    """The exe's main icon as a PIL RGBA image via shell32/GDI (ctypes, no pywin32), or None."""
    import ctypes
    from ctypes import wintypes

    shell32, user32, gdi32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    shell32.SHDefExtractIconW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.UINT,
                                          ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
                                          wintypes.UINT]
    shell32.SHDefExtractIconW.restype = ctypes.c_long  # HRESULT
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int,
                                  ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user32.DrawIconEx.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    hicon = wintypes.HICON()
    # nIconSize: LOWORD = large icon size, HIWORD = small icon size (unused).
    if shell32.SHDefExtractIconW(exe_path, 0, 0, ctypes.byref(hicon), None, size) != 0 or not hicon:
        return None

    def render(fill: int) -> bytes | None:
        hdc = gdi32.CreateCompatibleDC(None)
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = size, -size  # negative height: top-down rows
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0  # BI_RGB
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbm or not bits:
            gdi32.DeleteDC(hdc)
            return None
        old = gdi32.SelectObject(hdc, hbm)
        try:
            ctypes.memset(bits, fill, size * size * 4)
            if not user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 3):  # DI_NORMAL
                return None
            gdi32.GdiFlush()
            return ctypes.string_at(bits, size * size * 4)
        finally:
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(hbm)
            gdi32.DeleteDC(hdc)

    try:
        black, white = render(0x00), render(0xFF)
    finally:
        user32.DestroyIcon(hicon)
    if black is None or white is None:
        return None
    return image_from_black_white(black, white, size)


def _file_description(exe_path: str) -> str | None:
    """FileDescription from the exe's version resource, or None (pywin32 optional)."""
    try:
        import win32api  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        translations = win32api.GetFileVersionInfo(
            exe_path, "\\VarFileInfo\\Translation"
        )
    except Exception:  # noqa: BLE001 - no version resource / access denied
        translations = None
    candidates: list[tuple[int, int]] = list(translations or [])
    # Common fallbacks: US English + Unicode, US English + Western.
    candidates += [(0x0409, 0x04B0), (0x0409, 0x04E4)]
    for lang, codepage in candidates:
        try:
            desc = win32api.GetFileVersionInfo(
                exe_path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
            )
        except Exception as e:  # noqa: BLE001 - this lang/codepage not present
            log.debug(
                "no FileDescription %04x/%04x in %s: %r", lang, codepage, exe_path, e
            )
            continue
        if desc and str(desc).strip():
            return str(desc).strip()
    return None


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #


class WindowsBackend:
    """AudioBackend implementation for Windows (WASAPI sessions via pycaw)."""

    icon_inset = 0.0          # Windows icons have no plate around them...
    icon_keep_outline = True  # ...so the silhouette's edge is part of the logo

    def __init__(self, include_system_sounds: bool = False) -> None:
        self.include_system_sounds = include_system_sounds
        self._tls = threading.local()
        # app id -> exe path, shared across threads (icons load on a worker thread, while the
        # session cache in self._tls is per thread).
        self._exe_paths: dict[str, str] = {}
        self._name_cache: dict[str, str] = {}  # exe path (or exe name) -> friendly name
        self._name_lock = threading.Lock()

    # ---- COM / per-thread state -------------------------------------------

    def _ensure_com(self) -> None:
        if getattr(self._tls, "com_ready", False):
            return
        import comtypes  # type: ignore[import-not-found]

        try:
            comtypes.CoInitialize()
        except OSError as e:
            # RPC_E_CHANGED_MODE: thread already initialised in another apartment
            # mode (e.g. MTA). COM is usable, so carry on.
            log.debug("CoInitialize on %s: %s", threading.current_thread().name, e)
        self._tls.com_ready = True
        self._tls.groups = {}
        self._tls.enumerator = None

    def _groups(self) -> dict[str, AppGroup]:
        return self._tls.groups

    def _device_enumerator(self) -> Any:
        if self._tls.enumerator is None:
            from pycaw.utils import AudioUtilities  # type: ignore[import-not-found]

            self._tls.enumerator = AudioUtilities.GetDeviceEnumerator()
        return self._tls.enumerator

    # ---- enumeration -------------------------------------------------------

    def _iter_session_controls(self) -> Iterable[Any]:
        """Yield IAudioSessionControl2 for every session on every active render device."""
        import comtypes  # type: ignore[import-not-found]
        from pycaw.api.audiopolicy import (  # type: ignore[import-not-found]
            IAudioSessionControl2,
            IAudioSessionManager2,
        )
        from pycaw.constants import DEVICE_STATE, EDataFlow  # type: ignore[import-not-found]

        try:
            collection = self._device_enumerator().EnumAudioEndpoints(
                EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value
            )
        except Exception:
            # Enumerator may have gone stale; drop it so the next call recreates it.
            self._tls.enumerator = None
            raise
        if collection is None:
            return
        for d in range(collection.GetCount()):
            try:
                dev = collection.Item(d)
                if dev is None:
                    continue
                mgr = dev.Activate(
                    IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None
                ).QueryInterface(IAudioSessionManager2)
                sess_enum = mgr.GetSessionEnumerator()
                count = sess_enum.GetCount()
            except Exception:
                log.warning("skipping audio device #%d", d, exc_info=True)
                continue
            for i in range(count):
                try:
                    ctl = sess_enum.GetSession(i)
                    if ctl is None:
                        continue
                    yield ctl.QueryInterface(IAudioSessionControl2)
                except Exception:
                    log.debug("skipping session %d on device #%d", i, d, exc_info=True)

    @staticmethod
    def _is_system_session(ctl2: Any, pid: int) -> bool:
        if pid == 0:
            return True
        try:
            # comtypes returns the raw HRESULT for methods without [out] params:
            # S_OK (0) = system sounds session, S_FALSE (1) = not.
            return ctl2.IsSystemSoundsSession() == 0
        except Exception:  # noqa: BLE001
            return False

    def _read_session(self, ctl2: Any) -> SessionRecord | None:
        import psutil
        from pycaw.api.audioclient import ISimpleAudioVolume  # type: ignore[import-not-found]

        state = ctl2.GetState()
        if state == STATE_EXPIRED:
            return None
        pid = int(ctl2.GetProcessId())
        is_system = self._is_system_session(ctl2, pid)
        if is_system and not self.include_system_sounds:
            return None

        vol_iface = ctl2.QueryInterface(ISimpleAudioVolume)
        volume: float | None
        muted: bool | None
        try:
            volume = float(vol_iface.GetMasterVolume())
            muted = bool(vol_iface.GetMute())
        except Exception:  # noqa: BLE001
            volume = muted = None
        try:
            display_name = ctl2.GetDisplayName()
        except Exception:  # noqa: BLE001
            display_name = None

        if is_system:
            return SessionRecord(
                app_id=SYSTEM_SOUNDS_ID,
                exe_name=SYSTEM_SOUNDS_NAME,
                pid=pid,
                display_name=SYSTEM_SOUNDS_NAME,
                volume=volume,
                muted=muted,
                started=None,
                state=state,
                handle=vol_iface,
            )

        proc = psutil.Process(pid)  # NoSuchProcess -> caller logs + skips
        exe_name = proc.name()
        exe_path: str | None
        try:
            exe_path = proc.exe()
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            exe_path = None
        try:
            started: float | None = proc.create_time()
        except (psutil.Error, OSError):
            started = None
        return SessionRecord(
            app_id=app_id_for_exe(exe_name),
            exe_name=exe_name,
            pid=pid,
            exe_path=exe_path,
            display_name=display_name,
            volume=volume,
            muted=muted,
            started=started,
            state=state,
            handle=vol_iface,
        )

    def _enumerate(self) -> dict[str, AppGroup]:
        records: list[SessionRecord] = []
        for ctl2 in self._iter_session_controls():
            try:
                rec = self._read_session(ctl2)
            except Exception as e:  # noqa: BLE001 - access denied, process exited, ...
                log.debug("skipping audio session: %r", e)
                continue
            if rec is not None:
                records.append(rec)
        groups = group_sessions(records)
        self._tls.groups = groups
        return groups

    def _name_for(self, group: AppGroup) -> str:
        if group.app_id == SYSTEM_SOUNDS_ID:
            return SYSTEM_SOUNDS_NAME
        for s in group.sessions:
            if is_usable_display_name(s.display_name):
                return s.display_name.strip()  # type: ignore[union-attr]
        first = group.sessions[0]
        key = (
            next((s.exe_path for s in group.sessions if s.exe_path), None)
            or first.exe_name
        )
        with self._name_lock:
            cached = self._name_cache.get(key)
        if cached is not None:
            return cached
        desc = None
        if key != first.exe_name:  # we have a real path
            try:
                desc = _file_description(key)
            except Exception:  # noqa: BLE001
                desc = None
        name = choose_display_name((), desc, first.exe_name)
        with self._name_lock:
            self._name_cache[key] = name
        return name

    # ---- AudioBackend API --------------------------------------------------

    def list_apps(self) -> list[AppInfo]:
        self._ensure_com()
        apps: list[AppInfo] = []
        for group in self._enumerate().values():
            try:
                apps.append(summarize_group(group, self._name_for))
            except Exception:
                log.warning("skipping app %s", group.app_id, exc_info=True)
                continue
            path = next((s.exe_path for s in group.sessions if s.exe_path), None)
            if path:
                with self._name_lock:
                    self._exe_paths[group.app_id] = path
        return apps

    def icon_image(self, app_id: str):
        with self._name_lock:
            path = self._exe_paths.get(app_id)
        return _render_exe_icon(path) if path else None

    def _apply(self, app_id: str, op: Callable[[Any], None], what: str) -> None:
        self._ensure_com()
        group = self._groups().get(app_id)
        if group is None:
            group = self._enumerate().get(app_id)
        if group is None:
            log.info("%s: no audio sessions for %r", what, app_id)
            return
        failures = 0
        for handle in group.handles:
            try:
                op(handle)
            except Exception as e:  # noqa: BLE001 - session expired / process gone
                failures += 1
                log.debug("%s on %r session failed: %r", what, app_id, e)
        if failures:
            # Some cached sessions are stale; refresh the cache and retry once.
            fresh = self._enumerate().get(app_id)
            if fresh is not None:
                for handle in fresh.handles:
                    try:
                        op(handle)
                    except Exception as e:  # noqa: BLE001
                        log.debug("%s retry on %r failed: %r", what, app_id, e)

    def set_volume(self, app_id: str, vol: int) -> None:
        level = max(0, min(100, int(vol))) / 100.0
        self._apply(app_id, lambda h: h.SetMasterVolume(level, None), "set_volume")

    def set_mute(self, app_id: str, mute: bool) -> None:
        flag = 1 if mute else 0
        self._apply(app_id, lambda h: h.SetMute(flag, None), "set_mute")

    def close(self) -> None:
        # Drop this thread's COM pointers; comtypes releases them on GC. We don't
        # CoUninitialize: comtypes does that for the main thread at exit, and other
        # threads' state lives in their own threading.local.
        self._tls.groups = {}
        self._tls.enumerator = None
        with self._name_lock:
            self._name_cache.clear()
