"""NDI output for FaithView Pro -- pushes the live display as an NDI source.

The NDI sender lets OBS Studio / vMix / any NDI-compatible switcher pick up
the current verse slide over the local network ("Add Source -> NDI Source"),
which then encodes and pushes the program feed to YouTube.

Design notes:

* The NDI runtime (``libndi``) is loaded dynamically with ctypes -- the same
  pattern as the SDK's own ``Processing.NDI.DynamicLoad.h`` -- so FaithView
  Pro has **no new hard dependency**. If the runtime is not installed, the
  sender reports itself unavailable and the settings UI shows install
  instructions instead of crashing.
* Frames are rendered server-side with Pillow (radial blue gradient, wrapped
  auto-fit verse text, reference line) matching ``static/output.html``.
* NDI receivers expect a continuous frame stream, so a background thread
  pushes frames at the configured fps. Frames are only re-rendered when the
  displayed verse changes; the steady-state loop just re-sends the buffer.
* ``NDIlib_send_send_video_async_v2`` keeps ownership of the submitted buffer
  until the next send call, so we double-buffer and always render into the
  buffer the SDK is *not* currently holding.
* Timecodes are synthesized by the SDK (``NDIlib_send_timecode_synthesize``)
  -- the simplest integration path per the SDK docs.
* ``NDIlib_send_get_no_connections`` / ``NDIlib_send_get_tally`` are polled so
  rendering is skipped when no receiver is connected (a standard NDI power
  optimization) and the settings UI can show an ON AIR / preview tally.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import threading
import time
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# NDI SDK constants (public, documented C API)
# ---------------------------------------------------------------------------
FOURCC_BGRA = 0x41524742            # NDIlib_FourCC_video_type_BGRA ('B','G','R','A')
FRAME_FORMAT_PROGRESSIVE = 1        # NDIlib_frame_format_type_progressive
TIMECODE_SYNTHESIZE = 0x7FFFFFFFFFFFFFFF  # NDIlib_send_timecode_synthesize:
                                          # SDK stamps frames from the system clock

RESOLUTIONS = {
    "1920x1080": (1920, 1080),
    "1280x720": (1280, 720),
    "1080x1920": (1080, 1920),
}

DEFAULT_SOURCE_NAME = "FaithView Pro - Main Display"


class NDIlib_send_create_t(ctypes.Structure):
    _fields_ = [("p_ndi_name", ctypes.c_char_p),
                ("p_groups", ctypes.c_char_p),
                ("clock_video", ctypes.c_bool),
                ("clock_audio", ctypes.c_bool)]


class NDIlib_video_frame_v2_t(ctypes.Structure):
    _fields_ = [("xres", ctypes.c_int),
                ("yres", ctypes.c_int),
                ("FourCC", ctypes.c_int),
                ("frame_rate_N", ctypes.c_int),
                ("frame_rate_D", ctypes.c_int),
                ("picture_aspect_ratio", ctypes.c_float),
                ("frame_format_type", ctypes.c_int),
                ("timecode", ctypes.c_int64),
                ("p_data", ctypes.POINTER(ctypes.c_uint8)),
                ("line_stride_in_bytes", ctypes.c_int),
                ("p_metadata", ctypes.c_char_p),
                ("timestamp", ctypes.c_int64)]


class NDIlib_tally_t(ctypes.Structure):
    _fields_ = [("on_program", ctypes.c_bool),
                ("on_preview", ctypes.c_bool)]


# ---------------------------------------------------------------------------
# libndi discovery (Linux / macOS / Windows)
# ---------------------------------------------------------------------------
_LIB_CANDIDATES = [
    os.environ.get("NDI_LIB", ""),
    "/usr/lib/libndi.so.5", "/usr/lib/libndi.so",
    "/usr/local/lib/libndi.so.5", "/usr/local/lib/libndi.so",
    "/usr/lib/x86_64-linux-gnu/libndi.so.5", "/usr/lib/x86_64-linux-gnu/libndi.so",
    "libndi.so.5", "libndi.so",
    "/usr/local/lib/libndi.dylib",
    "/Library/NDI SDK for Apple/lib/macOS/libndi.dylib", "libndi.dylib",
    "Processing.NDI.Lib.x64.dll",
]

INSTALL_HINT = (
    "Install the free NDI Runtime from ndi.tv (on Ubuntu/Debian: download the "
    "'NDI SDK for Linux', install its runtime package providing libndi.so, "
    "then restart FaithView Pro)."
)


def find_libndi() -> Optional[str]:
    """Return a loadable libndi path/soname, or None if the runtime is absent."""
    _fix_dbus_system_bus_address()
    for cand in _LIB_CANDIDATES:
        if not cand:
            continue
        try:
            ctypes.CDLL(cand)
            return cand
        except OSError:
            continue
    # last resort: ldconfig registry + versioned sonames
    found = ctypes.util.find_library("ndi")
    if found:
        return found
    for pat in ("/usr/lib/**/libndi.so*", "/usr/local/lib/**/libndi.so*"):
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return hits[0]
    return None


def _fix_dbus_system_bus_address():
    """NDI discovery registers its mDNS service through avahi (DBus system bus).

    Under a conda/anaconda Python, a conda-provided libdbus may be loaded into
    the process; its compile-time default system-bus socket
    (``$CONDA_PREFIX/var/run/dbus/system_bus_socket``) does not exist, so the
    avahi registration silently fails with ENOENT and no NDI source is ever
    announced on the network (OBS/DistroAV finds nothing). Point the env var at
    the real system socket before libndi is loaded. No-op when the env var is
    already set or the standard socket is absent."""
    if "DBUS_SYSTEM_BUS_ADDRESS" in os.environ:
        return
    if os.path.exists("/var/run/dbus/system_bus_socket"):
        os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = (
            "unix:path=/var/run/dbus/system_bus_socket")


# ---------------------------------------------------------------------------
# Frame rendering (matches the look of static/output.html)
# ---------------------------------------------------------------------------
_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]
_FONT_REG_CANDIDATES = [p.replace("-Bold", "") for p in _FONT_BOLD_CANDIDATES[:-1]] + ["DejaVuSans.ttf"]

_font_cache: dict = {}
_gradient_cache: dict = {}


def _load_font(size: int, bold: bool = True):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_REG_CANDIDATES):
        try:
            font = ImageFont.truetype(path, size)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _gradient(width: int, height: int) -> np.ndarray:
    """Radial blue gradient: #64a3f5 centre -> #2560d0 (55%) -> #0b1e45 edges."""
    key = (width, height)
    if key in _gradient_cache:
        return _gradient_cache[key]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    r = np.sqrt(((xx - 0.5 * width) / (0.75 * width)) ** 2
                + ((yy - 0.30 * height) / (0.75 * height)) ** 2)
    c1 = np.array([100, 163, 245], np.float32)
    c2 = np.array([37, 96, 208], np.float32)
    c3 = np.array([11, 30, 69], np.float32)
    t1 = np.clip(r / 0.55, 0, 1)[..., None]
    t2 = np.clip((r - 0.55) / 0.45, 0, 1)[..., None]
    col = (c1 * (1 - t1) + c2 * t1) * (1 - t2) + c3 * t2
    rgb = col.astype(np.uint8)
    _gradient_cache[key] = rgb
    return rgb


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def render_verse_frame(width: int, height: int, reference: str,
                       translation: str, text: str) -> np.ndarray:
    """Render one verse slide; returns an HxWx4 uint8 array in NDI BGRA order."""
    img = Image.fromarray(_gradient(width, height), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(img)

    # --- auto-fit verse text ---
    max_w = int(width * 0.84)
    max_h = int(height * 0.60)
    size = int(height * 0.085)
    lines, line_h = [""], 0
    while True:
        font = _load_font(size, bold=True)
        lines = _wrap_lines(draw, text or "", font, max_w)
        line_h = int(size * 1.38)
        if line_h * len(lines) <= max_h or size <= int(height * 0.02):
            break
        size -= max(2, size // 12)

    total_h = line_h * len(lines)
    y = int(height * 0.46 - total_h / 2)
    shadow = (11, 30, 69, 160)
    for ln in lines:
        w = draw.textlength(ln, font=font)
        x = int((width - w) / 2)
        draw.text((x, y + max(2, size // 18)), ln, font=font, fill=shadow)
        draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255))
        y += line_h

    # --- reference line ---
    ref = (reference or "").upper() + (f" ({translation})" if translation else "")
    if ref.strip():
        rf = _load_font(int(height * 0.032), bold=True)
        rw = draw.textlength(ref, font=rf)
        draw.text(((width - rw) / 2, y + int(height * 0.045)), ref,
                  font=rf, fill=(201, 223, 255, 255))

    arr = np.asarray(img, dtype=np.uint8)
    return np.ascontiguousarray(arr[:, :, [2, 1, 0, 3]])  # RGBA -> BGRA


def render_black_frame(width: int, height: int) -> np.ndarray:
    return np.zeros((height, width, 4), dtype=np.uint8)


# ---------------------------------------------------------------------------
# NDI sender
# ---------------------------------------------------------------------------
class NDISender:
    """Background NDI video sender. Safe to construct without the NDI runtime:
    ``available`` will simply be False and ``start()`` becomes a no-op with a
    recorded reason (surfaced in the settings UI)."""

    def __init__(self):
        self.name = DEFAULT_SOURCE_NAME
        self.width, self.height, self.fps = 1920, 1080, 30
        self.enabled = False

        self._lib = None
        self._send = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending = ("black", None)   # ("black" | "verse", payload dict)
        self._dirty = True
        self._connections = 0
        self._on_program = False
        self._on_preview = False
        self._error: Optional[str] = None
        self._lib_path = find_libndi()

    # -- lifecycle ----------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._lib_path is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def configure(self, source_name: str = None, resolution: str = None,
                  fps: int = None):
        if source_name:
            self.name = source_name.strip() or DEFAULT_SOURCE_NAME
        if resolution in RESOLUTIONS:
            self.width, self.height = RESOLUTIONS[resolution]
        if fps in (25, 30, 60):
            self.fps = fps

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def start(self) -> bool:
        if self.running:
            return True
        if not self.available:
            self._error = f"NDI runtime (libndi) not found. {INSTALL_HINT}"
            return False
        try:
            self._lib = ctypes.CDLL(self._lib_path)
            self._bind()
            if not self._lib.NDIlib_initialize():
                raise RuntimeError("NDIlib_initialize() failed")
            create = NDIlib_send_create_t(
                p_ndi_name=self.name.encode("utf-8"),
                p_groups=None, clock_video=False, clock_audio=False)
            self._send = self._lib.NDIlib_send_create(ctypes.byref(create))
            if not self._send:
                raise RuntimeError("NDIlib_send_create() returned NULL")
        except Exception as exc:
            self._error = f"NDI init failed: {exc}"
            self._lib = None
            self._send = None
            return False
        self._error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fv-ndi", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        if self._lib is not None and self._send:
            try:
                self._lib.NDIlib_send_destroy(self._send)
            except Exception:
                pass
        self._send = None
        self._connections = 0
        self._on_program = False
        self._on_preview = False

    def apply(self, enabled: bool, source_name: str = None,
              resolution: str = None, fps: int = None) -> bool:
        """Apply settings; restarts the sender if running config changed."""
        was_running = self.running
        old = (self.name, self.width, self.height, self.fps)
        self.configure(source_name, resolution, fps)
        changed = old != (self.name, self.width, self.height, self.fps)
        self.enabled = enabled
        if enabled and (not was_running or changed):
            self.stop()
            return self.start()
        if not enabled and was_running:
            self.stop()
        return self.running

    # -- content ------------------------------------------------------------
    def show(self, reference: str, translation: str, text: str):
        with self._lock:
            self._pending = ("verse", {"reference": reference or "",
                                       "translation": translation or "",
                                       "text": text or ""})
            self._dirty = True

    def clear(self):
        with self._lock:
            self._pending = ("black", None)
            self._dirty = True

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "running": self.running,
            "connections": self._connections if self.running else 0,
            "on_program": self._on_program if self.running else False,
            "on_preview": self._on_preview if self.running else False,
            "source_name": self.name,
            "resolution": self.resolution,
            "fps": self.fps,
            "reason": None if (self.running or not self.enabled) else
                      (self._error or (None if self.available else
                       f"NDI runtime (libndi) not found. {INSTALL_HINT}")),
        }

    # -- internals ----------------------------------------------------------
    def _bind(self):
        lib = self._lib
        lib.NDIlib_initialize.restype = ctypes.c_bool
        lib.NDIlib_send_create.restype = ctypes.c_void_p
        lib.NDIlib_send_create.argtypes = [ctypes.POINTER(NDIlib_send_create_t)]
        lib.NDIlib_send_send_video_async_v2.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(NDIlib_video_frame_v2_t)]
        lib.NDIlib_send_send_video_async_v2.restype = None
        lib.NDIlib_send_get_no_connections.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.NDIlib_send_get_no_connections.restype = ctypes.c_int
        lib.NDIlib_send_get_tally.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(NDIlib_tally_t), ctypes.c_uint32]
        lib.NDIlib_send_get_tally.restype = ctypes.c_bool
        lib.NDIlib_send_destroy.argtypes = [ctypes.c_void_p]
        lib.NDIlib_send_destroy.restype = None

    def _make_frame_struct(self, buf: np.ndarray) -> NDIlib_video_frame_v2_t:
        return NDIlib_video_frame_v2_t(
            xres=self.width, yres=self.height, FourCC=FOURCC_BGRA,
            frame_rate_N=self.fps, frame_rate_D=1,
            picture_aspect_ratio=self.width / self.height,
            frame_format_type=FRAME_FORMAT_PROGRESSIVE,
            timecode=TIMECODE_SYNTHESIZE,
            p_data=buf.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            line_stride_in_bytes=self.width * 4,
            p_metadata=None, timestamp=0)

    def _render_pending_into(self, buf: np.ndarray):
        with self._lock:
            kind, payload = self._pending
            self._dirty = False
        if kind == "verse" and payload:
            np.copyto(buf, render_verse_frame(self.width, self.height,
                                              payload["reference"],
                                              payload["translation"],
                                              payload["text"]))
        else:
            buf[:] = 0

    def _run(self):
        # double-buffer: the SDK owns the previously-sent buffer until the
        # next async send, so always render into the *other* one.
        bufs = [np.zeros((self.height, self.width, 4), np.uint8) for _ in range(2)]
        frames = [self._make_frame_struct(b) for b in bufs]
        cur = 0
        last_status_check = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                if t0 - last_status_check > 2.0:
                    last_status_check = t0
                    self._connections = self._lib.NDIlib_send_get_no_connections(
                        self._send, 0)
                    tally = NDIlib_tally_t()
                    try:
                        if self._lib.NDIlib_send_get_tally(
                                self._send, ctypes.byref(tally), 0):
                            self._on_program = bool(tally.on_program)
                            self._on_preview = bool(tally.on_preview)
                    except Exception:
                        pass  # older runtimes may lack tally; keep sending
                # Power optimization: when no receiver is connected, skip the
                # render entirely and only heartbeat the last frame at 2 fps.
                # (Rendering resumes automatically the moment a receiver --
                # e.g. an OBS NDI source -- attaches.)
                idle = self._connections == 0
                interval = 0.5 if idle else 1.0 / self.fps
                if self._dirty and not idle:
                    self._render_pending_into(bufs[1 - cur])
                    cur = 1 - cur
                self._lib.NDIlib_send_send_video_async_v2(
                    self._send, ctypes.byref(frames[cur]))
            except Exception as exc:
                self._error = f"NDI send error: {exc}"
                time.sleep(1.0)
                interval = 1.0
            dt = time.monotonic() - t0
            if dt < interval:
                time.sleep(interval - dt)
