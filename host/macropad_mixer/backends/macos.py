"""macOS backend.

Listing: Core Audio process objects (macOS 14.2+) via ctypes -- kAudioHardwarePropertyProcessObjectList
on the system object, then per process its PID, bundle id and IsRunningOutput. Each PID is mapped to its
owning .app bundle (helpers such as "Google Chrome Helper.app" or WebKit XPC services resolve to the parent
app) the same way FineTune does, so our app ids match FineTune's.

Control: FineTune's URL scheme (guide/url-schemes.md in github.com/ronitsingh10/FineTune):
    finetune://set-volumes?app=<bundle id>&volume=<0-100>
    finetune://set-mute?app=<bundle id>&muted=true|false
FineTune can't report volumes back, so list_apps() returns vol/mute None and the daemon keeps its own record.

Icons: AppKit's NSWorkspace.iconForFile, rendered to PNG through a small JavaScript-for-Automation
script (osascript), so no pyobjc dependency is needed.
"""

from __future__ import annotations

import ctypes
import logging
import os
import plistlib
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, urlencode

from .base import AppInfo

log = logging.getLogger(__name__)

FINETUNE_BUNDLE_ID = "com.finetuneapp.FineTune"
RECENT_OUTPUT_WINDOW = 60.0  # list apps this long after they stop playing (playing=False then)
# Background helpers macOS ships here (e.g. PowerChime, the charging sound) are never shown.
SYSTEM_APP_DIRS = ("/System/Library/",)
MIN_URL_INTERVAL = 0.06      # at most one `open` per app per 60 ms

# Bundle id prefixes of system audio producers to hide (subset of FineTune's list).
SYSTEM_PREFIXES = (
    "com.apple.siri", "com.apple.Siri", "com.apple.assistant", "com.apple.audio", "com.apple.coreaudio",
    "com.apple.mediaremote", "com.apple.accessibility.heard", "com.apple.hearingd", "com.apple.systemsound",
    "com.apple.notificationcenter", "com.apple.NotificationCenter", "com.apple.usernotifications",
    "com.apple.UserNotifications", "com.apple.speech", "com.apple.dictation", "com.apple.corespeech",
    "com.apple.CoreSpeech", "com.apple.controlcenter",
)

ICON_INSET = 0.07  # macOS draws every icon on a rounded-square plate; crop inside it
ICON_CACHE = os.path.expanduser("~/Library/Caches/macropad-mixer/icons")

# Renders an app's Finder icon at 128x128 into a PNG: osascript -l JavaScript <file> <app path> <png path>
_ICON_JXA = """ObjC.import('AppKit');
function run(argv) {
  var img = $.NSWorkspace.sharedWorkspace.iconForFile(argv[0]);
  var rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(null, 128, 128, 8, 4, true, false, $.NSCalibratedRGBColorSpace, 0, 0);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext($.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep));
  img.drawInRectFromRectOperationFraction($.NSMakeRect(0, 0, 128, 128), $.NSZeroRect, $.NSCompositingOperationCopy, 1.0);
  $.NSGraphicsContext.restoreGraphicsState;
  rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(argv[1], true);
}
"""


def render_icon(app_path: str, app_id: str):
    """Return the app's icon as a PIL RGBA image (cached on disk), or None."""
    from PIL import Image

    os.makedirs(ICON_CACHE, exist_ok=True)
    png = os.path.join(ICON_CACHE, quote(app_id, safe="") + ".png")
    if not os.path.exists(png) or os.path.getmtime(png) < os.path.getmtime(app_path):
        script = os.path.join(ICON_CACHE, "icon.js")
        if not os.path.exists(script):
            with open(script, "w") as f:
                f.write(_ICON_JXA)
        subprocess.run(["osascript", "-l", "JavaScript", script, app_path, png],
                       check=True, capture_output=True, timeout=10)
    img = Image.open(png)
    img.load()
    return img


def find_app_by_bundle_id(bundle_id: str) -> str | None:
    """Locate an installed app by bundle id (Spotlight), preferring /Applications."""
    if "." not in bundle_id or "'" in bundle_id:
        return None
    try:
        out = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [p for p in out.splitlines() if p.endswith(".app") and os.path.isdir(p)]
    paths.sort(key=lambda p: (not p.startswith("/Applications/"), len(p)))
    return paths[0] if paths else None


def bundle_icns(app_path: str):
    """The app's own .icns as a PIL image: full colour, unlike the rendered icon, which follows
    the user's tinted/clear icon style. None if the app only ships an asset catalog."""
    from PIL import Image

    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            name = plistlib.load(f).get("CFBundleIconFile") or ""
    except (OSError, plistlib.InvalidFileException):
        return None
    if not name:
        return None
    path = os.path.join(app_path, "Contents", "Resources", name)
    if not path.endswith(".icns"):
        path += ".icns"
    if not os.path.exists(path):
        return None
    img = Image.open(path)
    img.load()
    return img


# --- Core Audio / libproc via ctypes ---------------------------------------------------------------


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# Constants from CoreAudio/AudioHardware.h and AudioHardwareBase.h
kAudioObjectSystemObject = 1
kAudioObjectPropertyScopeGlobal = _fourcc("glob")
kAudioObjectPropertyElementMain = 0
kAudioHardwarePropertyProcessObjectList = _fourcc("prs#")
kAudioProcessPropertyPID = _fourcc("ppid")
kAudioProcessPropertyBundleID = _fourcc("pbid")
kAudioProcessPropertyIsRunningOutput = _fourcc("piro")
kCFStringEncodingUTF8 = 0x08000100
PROC_PIDTBSDINFO = 3
PROC_PIDPATHINFO_MAXSIZE = 4096


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


class _ProcBsdInfo(ctypes.Structure):  # <sys/proc_info.h> struct proc_bsdinfo
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32), ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32), ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32), ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_libc = ctypes.CDLL(None)

_ca.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_ca.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.proc_pidpath.restype = ctypes.c_int
_libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
_libc.proc_pidinfo.restype = ctypes.c_int
try:  # private API (what Activity Monitor and FineTune use for XPC services)
    _responsible_pid = _libc.responsibility_get_pid_responsible_for_pid
    _responsible_pid.argtypes = [ctypes.c_int]
    _responsible_pid.restype = ctypes.c_int
except AttributeError:
    _responsible_pid = None


def _get(obj: int, selector: int, value: ctypes._SimpleCData | ctypes.Array) -> bool:
    addr = _PropertyAddress(selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    return _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size),
                                          ctypes.byref(value)) == 0


def _process_objects() -> list[int]:
    addr = _PropertyAddress(kAudioHardwarePropertyProcessObjectList, kAudioObjectPropertyScopeGlobal,
                            kAudioObjectPropertyElementMain)
    size = ctypes.c_uint32()
    if _ca.AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, ctypes.byref(addr), 0, None,
                                          ctypes.byref(size)) != 0:
        return []
    arr = (ctypes.c_uint32 * (size.value // 4))()
    if not arr or not _get(kAudioObjectSystemObject, kAudioHardwarePropertyProcessObjectList, arr):
        return []
    return list(arr)


def _process_bundle_id(obj: int) -> str:
    ref = ctypes.c_void_p()
    if not _get(obj, kAudioProcessPropertyBundleID, ref) or not ref.value:
        return ""
    try:
        buf = ctypes.create_string_buffer(1024)
        return buf.value.decode() if _cf.CFStringGetCString(ref, buf, len(buf), kCFStringEncodingUTF8) else ""
    finally:
        _cf.CFRelease(ref)


def _pid_path(pid: int) -> str:
    buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    n = _libc.proc_pidpath(pid, buf, len(buf))
    return buf.value.decode(errors="replace") if n > 0 else ""


def _bsd_info(pid: int) -> _ProcBsdInfo | None:
    info = _ProcBsdInfo()
    n = _libc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    return info if n == ctypes.sizeof(info) else None


def _app_path(pid: int) -> str | None:
    """Outermost .app bundle containing the executable of pid (so helper .apps map to their parent)."""
    path = _pid_path(pid)
    i = path.find(".app/")
    return path[: i + 4] if i >= 0 else None


# --- helpers --------------------------------------------------------------------------------------


def _parent(pid: int) -> int:
    info = _bsd_info(pid)
    return info.pbi_ppid if info else 0


def _resolve_app(pid: int) -> tuple[str, int] | None:
    """Find the owning .app for pid: its own executable path, then its responsible process (XPC
    services), then its ancestors. Returns (app bundle path, main pid of that app) or None."""
    candidates = [pid]
    if _responsible_pid is not None:
        candidates.append(_responsible_pid(pid))
    cur = _parent(pid)
    while cur > 1 and len(candidates) < 32:
        candidates.append(cur)
        cur = _parent(cur)
    for cand in candidates:
        app = _app_path(cand) if cand > 1 else None
        if app:
            # climb to the topmost ancestor still inside the same bundle (the main app process)
            while (parent := _parent(cand)) > 1 and _app_path(parent) == app:
                cand = parent
            return app, cand
    return None


_bundle_cache: dict[str, tuple[str | None, str]] = {}


def _bundle_info(app_path: str) -> tuple[str | None, str]:
    """(CFBundleIdentifier, friendly name) for an .app path, cached."""
    if app_path not in _bundle_cache:
        name = os.path.basename(app_path)[:-4]
        bid = None
        try:
            with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
                pl = plistlib.load(f)
            bid = pl.get("CFBundleIdentifier")
            name = pl.get("CFBundleDisplayName") or pl.get("CFBundleName") or name
        except (OSError, plistlib.InvalidFileException, ValueError) as e:
            log.debug("cannot read Info.plist of %s: %s", app_path, e)
        _bundle_cache[app_path] = (bid, name)
    return _bundle_cache[app_path]


def _start_time(pid: int) -> float:
    info = _bsd_info(pid)
    return info.pbi_start_tvsec + info.pbi_start_tvusec / 1e6 if info else time.time()


def _is_system(bundle_id: str) -> bool:
    # FineTune re-plays every app it adjusts, so it shows up as an audio app itself; hide it.
    return bundle_id.startswith(SYSTEM_PREFIXES) or bundle_id == FINETUNE_BUNDLE_ID


def finetune_installed() -> bool:
    for d in ("/Applications", os.path.expanduser("~/Applications")):
        if os.path.isdir(os.path.join(d, "FineTune.app")):
            return True
    if shutil.which("mdfind"):
        try:
            out = subprocess.run(["mdfind", f"kMDItemCFBundleIdentifier == '{FINETUNE_BUNDLE_ID}'"],
                                 capture_output=True, text=True, timeout=5).stdout
            return bool(out.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return False


FINETUNE_CHECK_INTERVAL = 30.0  # seconds between "is FineTune running?" checks


def finetune_running() -> bool:
    return subprocess.run(["pgrep", "-xq", "FineTune"], timeout=5).returncode == 0


def launch_finetune() -> None:
    """Start FineTune in the background (no window), so it needs no login item of its own."""
    subprocess.run(["open", "-g", "-b", FINETUNE_BUNDLE_ID], capture_output=True, timeout=10)


def volume_url(app_id: str, vol: int) -> str:
    return "finetune://set-volumes?" + urlencode({"app": app_id, "volume": int(vol)}, quote_via=quote)


def mute_url(app_id: str, mute: bool) -> str:
    return "finetune://set-mute?" + urlencode({"app": app_id, "muted": "true" if mute else "false"},
                                              quote_via=quote)


class _UrlSender:
    """Opens FineTune URLs on a worker thread. Per (action, app) the latest URL wins, and each app gets
    at most one `open` per MIN_URL_INTERVAL, so spinning the knob doesn't spawn dozens of processes."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], str] = {}
        self._next_ok: dict[str, float] = {}
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="finetune-url", daemon=True)
        self._thread.start()

    def submit(self, action: str, app_id: str, url: str) -> None:
        with self._cond:
            self._pending[(action, app_id)] = url
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while True:
            with self._cond:
                url = None
                while url is None:
                    if self._stop:
                        return
                    if not self._pending:
                        self._cond.wait()
                        continue
                    now = time.monotonic()
                    ready = [k for k in self._pending if self._next_ok.get(k[1], 0.0) <= now]
                    if not ready:
                        self._cond.wait(min(self._next_ok[k[1]] for k in self._pending) - now)
                        continue
                    key = ready[0]
                    url = self._pending.pop(key)
                    self._next_ok[key[1]] = now + MIN_URL_INTERVAL
            log.debug("open %s", url)
            try:
                subprocess.run(["open", "-g", url], check=False, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("open %s failed: %s", url, e)


class MacBackend:
    icon_inset = ICON_INSET

    def __init__(self) -> None:
        self._last_output: dict[str, float] = {}  # app id -> monotonic time last seen playing
        self._app_paths: dict[str, str] = {}      # app id -> .app bundle path (for icons)
        self._pids: dict[str, dict[int, str]] = {}  # app id -> {pid: executable path}, for app_alive
        self._sender = _UrlSender()
        self._finetune_ok = False
        self._finetune_checked = float("-inf")
        self._warned = False
        self._finetune_running_checked = float("-inf")
        self._check_finetune()
        self._ensure_finetune_running()

    def _check_finetune(self) -> bool:
        now = time.monotonic()
        if not self._finetune_ok and now - self._finetune_checked >= 60:
            self._finetune_checked = now
            self._finetune_ok = finetune_installed()
            if not self._finetune_ok and not self._warned:
                self._warned = True
                log.warning("FineTune (%s) is not installed; per-app volume changes will be ignored. "
                            "Install it from https://github.com/ronitsingh10/FineTune", FINETUNE_BUNDLE_ID)
            elif self._finetune_ok and self._warned:
                log.info("FineTune found")
        return self._finetune_ok

    def _ensure_finetune_running(self) -> None:
        """Our daemon starts at login; start FineTune with it (and again if it was quit)."""
        now = time.monotonic()
        if now - self._finetune_running_checked < FINETUNE_CHECK_INTERVAL or not self._finetune_ok:
            return
        self._finetune_running_checked = now
        try:
            if not finetune_running():
                log.info("starting FineTune")
                launch_finetune()
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("could not start FineTune: %s", e)

    def list_apps(self) -> list[AppInfo]:
        self._ensure_finetune_running()
        now = time.monotonic()
        me = os.getpid()
        present: dict[str, AppInfo] = {}
        pids: dict[str, dict[int, str]] = {}
        for obj in _process_objects():
            pid = ctypes.c_int32()
            if not _get(obj, kAudioProcessPropertyPID, pid) or pid.value <= 0 or pid.value == me:
                continue
            running = ctypes.c_uint32()
            playing = _get(obj, kAudioProcessPropertyIsRunningOutput, running) and running.value != 0
            resolved = _resolve_app(pid.value)
            if resolved and resolved[0].startswith(SYSTEM_APP_DIRS):
                continue
            if resolved:
                app_path, main_pid = resolved
                bid, name = _bundle_info(app_path)
                bid = bid or _process_bundle_id(obj)
                started = _start_time(main_pid)
                if bid:
                    self._app_paths[bid] = app_path
            else:
                # no .app: only keep processes with a real (non-Apple) bundle id; drops system daemons
                bid = _process_bundle_id(obj)
                if "." not in bid or bid.startswith("com.apple."):
                    continue
                name, started = bid.rsplit(".", 1)[-1], _start_time(pid.value)
            if not bid or _is_system(bid):
                continue
            if playing:
                self._last_output[bid] = now
            owner = main_pid if resolved else pid.value
            for p in {owner, pid.value}:
                pids.setdefault(bid, {})[p] = _pid_path(p)
            if bid not in present or started < present[bid].started:
                present[bid] = AppInfo(bid, name, started, playing=playing or
                                       (present[bid].playing if bid in present else False))
            elif playing:
                present[bid].playing = True
        listed = [a for a in present.values() if now - self._last_output.get(a.id, float("-inf"))
                  <= RECENT_OUTPUT_WINDOW]
        for a in listed:
            self._pids[a.id] = pids[a.id]
        return listed

    def app_alive(self, app_id: str) -> bool:
        """True while any process we saw for this app is still running the same executable
        (the path check guards against PID reuse)."""
        for pid, path in self._pids.get(app_id, {}).items():
            try:
                if path and _pid_path(pid) == path:
                    return True
            except Exception:  # noqa: BLE001 - must never raise
                log.debug("app_alive(%s) pid %d check failed", app_id, pid, exc_info=True)
        return False

    def _path_for(self, app_id: str) -> str | None:
        """The app's .app path: from the audio listing, or (pinned apps that haven't played
        since we started) looked up by bundle id via Spotlight."""
        path = self._app_paths.get(app_id)
        if path is None:
            path = find_app_by_bundle_id(app_id)
            if path:
                self._app_paths[app_id] = path
        return path

    def icon_image(self, app_id: str):
        path = self._path_for(app_id)
        return render_icon(path, app_id) if path else None

    def color_image(self, app_id: str):
        path = self._path_for(app_id)
        return bundle_icns(path) if path else None

    def set_volume(self, app_id: str, vol: int) -> None:
        if self._check_finetune():
            self._sender.submit("vol", app_id, volume_url(app_id, max(0, min(100, vol))))

    def set_mute(self, app_id: str, mute: bool) -> None:
        if self._check_finetune():
            self._sender.submit("mute", app_id, mute_url(app_id, mute))

    def close(self) -> None:
        self._sender.close()
