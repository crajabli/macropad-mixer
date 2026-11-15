"""App icons for the MacroPad OLED: fetch in the background, convert to 32x32 1-bit line art.

The OLED is monochrome, so colour icons are turned into outlines (edge detection), which keeps
logos recognisable at 32x32 far better than thresholding or dithering does.
"""

from __future__ import annotations

import base64
import colorsys
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PIL import Image, ImageChops, ImageFilter, ImageOps

log = logging.getLogger(__name__)

ICON_SIZE = 32
_WORK = 64  # edge detection runs at 2x, then downsamples


def to_bits(img: Image.Image, inset: float = 0.0, size: int = ICON_SIZE,
            keep_outline: bool = False) -> bytes:
    """Convert an RGBA icon to packed 1-bit rows (MSB first, rows padded to whole bytes).

    `inset` crops that fraction off each side of the opaque area first, e.g. to drop the
    rounded-square plate that macOS draws around every icon. `keep_outline` keeps the edge of
    the icon's silhouette (Windows icons have no plate, so the silhouette is the logo's shape);
    otherwise only the drawing inside the silhouette is kept.
    """
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    dx, dy = int(w * inset), int(h * inset)
    img = img.crop((dx, dy, w - dx, h - dy)).resize((_WORK, _WORK), Image.LANCZOS)

    flat = Image.new("RGBA", img.size, (0, 0, 0, 255))
    flat.alpha_composite(img)
    edges = ImageOps.autocontrast(flat.convert("L")).filter(ImageFilter.FIND_EDGES)
    # Ignore the outline of the icon's own silhouette; keep the drawing inside it.
    inner = img.getchannel("A").point(lambda a: 255 if a > 128 else 0)
    if not keep_outline and inner.getextrema() != (255, 255):  # only with a real silhouette
        edges = ImageChops.multiply(edges, inner.filter(ImageFilter.MinFilter(9)))
    small = edges.resize((size, size), Image.LANCZOS).point(lambda v: 255 if v > 40 else 0)

    px = small.load()
    row_bytes = (size + 7) // 8
    out = bytearray(row_bytes * size)
    for y in range(1, size - 1):  # leave a 1 px empty ring: crop edges cause stray lines
        for x in range(1, size - 1):
            if px[x, y]:
                out[y * row_bytes + x // 8] |= 0x80 >> (x % 8)
    return bytes(out)


def dominant_color(img: Image.Image) -> int | None:
    """The icon's main brand colour as an LED-friendly 0xRRGGBB, or None for grey icons.

    Opaque, reasonably saturated pixels are binned by hue (weighted by saturation x brightness);
    the heaviest bin wins. The result is pushed to full brightness and at least 70 % saturation
    because NeoPixels wash out pale colours.
    """
    img = img.convert("RGBA")
    img.thumbnail((64, 64))
    bins: dict[int, list[float]] = {}
    data = img.tobytes()
    for i in range(0, len(data), 4):
        r, g, b, a = data[i : i + 4]
        if a < 200:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.35 or v < 0.25:
            continue
        w = s * v
        acc = bins.setdefault(int(h * 24) % 24, [0.0, 0.0, 0.0, 0.0])
        acc[0] += w
        acc[1] += r * w
        acc[2] += g * w
        acc[3] += b * w
    if not bins:
        return None
    w, r, g, b = max(bins.values())
    h, s, _ = colorsys.rgb_to_hsv(r / w / 255, g / w / 255, b / w / 255)
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(h, max(s, 0.7), 1.0))
    return (r << 16) | (g << 8) | b


def icon_msg(app_id: str, bits: bytes | None, color: int | None, size: int = ICON_SIZE) -> dict:
    msg: dict = {"t": "icon", "id": app_id}
    if bits:
        msg.update(w=size, h=size, b=base64.b64encode(bits).decode())
    if color is not None:
        msg["c"] = color
    return msg


Entry = tuple[bytes | None, int | None]  # (packed icon bits, LED colour)

RETRY_EMPTY = 60.0  # seconds before retrying an app whose icon couldn't be loaded


def default_store_dir() -> Path:
    """Where converted icons/colours are kept, so pinned apps keep their look across restarts."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "macropad-mixer" / "icon-data"


class IconCache:
    """Fetches icons on a worker thread so a slow lookup never stalls the knob.

    `loader(app_id)` returns an RGBA image or None. `color_loader(app_id)`, if given, returns the
    image to take the brand colour from (macOS: the untinted .icns, since the rendered icon follows
    the user's tinted/clear icon style); otherwise the colour comes from the same image.
    `get()` returns (bits, color) once ready, None while loading.
    """

    def __init__(
        self,
        loader: Callable[[str], Image.Image | None],
        inset: float = 0.0,
        color_loader: Callable[[str], Image.Image | None] | None = None,
        keep_outline: bool = False,
        store_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store_dir
        self._clock = clock
        self._empty_at: dict[str, float] = {}  # app id -> when a load came back empty
        self._loader = loader
        self._keep_outline = keep_outline
        self._color_loader = color_loader
        self._inset = inset
        self._bits: dict[str, Entry] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="icons")

    def get(self, app_id: str) -> Entry | None:
        with self._lock:
            if app_id in self._bits:
                entry = self._bits[app_id]
                empty_since = self._empty_at.get(app_id)
                if empty_since is None or self._clock() - empty_since < RETRY_EMPTY:
                    return entry
                # Came back empty a while ago (e.g. a pinned app we couldn't locate): try again.
                del self._bits[app_id], self._empty_at[app_id]
            if app_id in self._pending:
                return None
            self._pending.add(app_id)
        self._pool.submit(self._load, app_id)
        return None

    def ready(self, app_id: str) -> bool:
        with self._lock:
            return app_id in self._bits

    def _load(self, app_id: str) -> None:
        bits = color = None
        img = None
        try:
            img = self._loader(app_id)
            if img is not None:
                bits = to_bits(img, self._inset, keep_outline=self._keep_outline)
        except Exception:
            log.debug("icon for %s failed", app_id, exc_info=True)
        try:
            src = self._color_loader(app_id) if self._color_loader else None
            src = src if src is not None else img
            if src is not None:
                color = dominant_color(src)
        except Exception:
            log.debug("colour for %s failed", app_id, exc_info=True)
        if bits or color is not None:
            self._save(app_id, bits, color)
        else:
            bits, color = self._restore(app_id)  # app not found now: use what we saw before
        with self._lock:
            self._bits[app_id] = (bits, color)
            if not bits and color is None:
                self._empty_at[app_id] = self._clock()
            self._pending.discard(app_id)
        log.debug("icon for %s: %s, colour %s", app_id, "ok" if bits else "none",
                  f"#{color:06x}" if color is not None else "none")

    def _path(self, app_id: str) -> Path | None:
        return self._store / (quote(app_id, safe="") + ".json") if self._store else None

    def _save(self, app_id: str, bits: bytes | None, color: int | None) -> None:
        path = self._path(app_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {"bits": base64.b64encode(bits).decode() if bits else None, "color": color}
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as e:
            log.debug("could not save icon for %s: %s", app_id, e)

    def _restore(self, app_id: str) -> Entry:
        path = self._path(app_id)
        if path is None or not path.exists():
            return None, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            bits = base64.b64decode(data["bits"]) if data.get("bits") else None
            color = data.get("color") if isinstance(data.get("color"), int) else None
            return bits, color
        except (OSError, ValueError, KeyError) as e:
            log.debug("could not read saved icon for %s: %s", app_id, e)
            return None, None

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
