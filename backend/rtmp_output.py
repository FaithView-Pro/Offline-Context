"""RTMP output for FaithView Pro — streams the live display as an RTMP video feed.

Encodes the server-side rendered slide (reusing ``ndi_output``'s Pillow renderer)
with ffmpeg and pushes it to a streaming service (YouTube, Facebook Live, etc.).

Design mirrors ``NDISender`` so the display / NDI / RTMP outputs all expose the
same apply / start / stop / status lifecycle — only the encode-and-send step
differs. ffmpeg is spawned as a subprocess and fed raw BGR frames on stdin; the
slide is re-rendered only when its content changes, so the steady-state loop
just re-pushes the last frame.

No new hard dependency: if ffmpeg is absent the sender reports itself
unavailable and the settings UI shows install instructions instead of crashing.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from ndi_output import RESOLUTIONS, render_black_frame, render_verse_frame


class RTMPSender:
    """Background RTMP video sender. Safe to construct without ffmpeg:
    ``available`` is simply False and ``start()`` becomes a no-op with a
    recorded reason (surfaced in the settings UI)."""

    def __init__(self):
        self.url = ""
        self.key = ""
        self.width, self.height, self.fps = 1920, 1080, 30
        self.bitrate = "4500k"
        self.enabled = False

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending = ("black", None)  # ("black" | "verse", payload dict)
        self._dirty = True
        self._error: Optional[str] = None
        self._last_stderr: Optional[str] = None

    # -- capability / state --------------------------------------------------
    @property
    def available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._proc is not None

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def destination(self) -> str:
        url = self.url.rstrip("/")
        return f"{url}/{self.key}" if self.key else url

    # -- config / lifecycle -------------------------------------------------
    def configure(self, url: str = None, key: str = None, resolution: str = None,
                  fps: int = None, bitrate: str = None):
        if url is not None:
            self.url = url.strip()
        if key is not None:
            self.key = key.strip()
        if resolution in RESOLUTIONS:
            self.width, self.height = RESOLUTIONS[resolution]
        if fps in (25, 30, 60):
            self.fps = fps
        if bitrate:
            self.bitrate = str(bitrate)

    def start(self) -> bool:
        if self.running:
            return True
        if not self.available:
            self._error = "ffmpeg not found. Install ffmpeg to use RTMP output."
            return False
        if not self.url:
            self._error = "RTMP URL is not set."
            return False
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
            "-i", "-",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", str(self.fps * 2), "-b:v", self.bitrate,
            "-f", "flv", self.destination,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            self._error = f"ffmpeg spawn failed: {exc}"
            return False

        self._last_stderr = None
        self._error = None
        self._stop.clear()
        self._stderr_thread = threading.Thread(target=self._drain_stderr,
                                               name="fv-rtmp-stderr", daemon=True)
        self._stderr_thread.start()
        self._thread = threading.Thread(target=self._run, name="fv-rtmp", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    def apply(self, enabled: bool, url: str = None, key: str = None,
              resolution: str = None, fps: int = None, bitrate: str = None) -> bool:
        """Apply settings; restart the sender if the running config changed."""
        was_running = self.running
        old = (self.url, self.key, self.width, self.height, self.fps, self.bitrate)
        self.configure(url, key, resolution, fps, bitrate)
        changed = old != (self.url, self.key, self.width, self.height, self.fps, self.bitrate)
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
            "url": self.url,
            "key": self.key,
            "resolution": self.resolution,
            "fps": self.fps,
            "bitrate": self.bitrate,
            "reason": None if (self.running or not self.enabled) else (
                self._error or (None if self.available else
                                "ffmpeg not found. Install ffmpeg to use RTMP output.")),
            "last_error": self._last_stderr,
        }

    # -- internals ----------------------------------------------------------
    def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    self._last_stderr = line
        except Exception:
            pass

    def _render_into(self, buf: np.ndarray):
        with self._lock:
            kind, payload = self._pending
            self._dirty = False
        if kind == "verse" and payload:
            frame = render_verse_frame(self.width, self.height,
                                       payload["reference"],
                                       payload["translation"],
                                       payload["text"])
            np.copyto(buf, frame[:, :, :3])  # BGRA -> BGR
        else:
            buf[:] = 0

    def _run(self):
        buf = np.zeros((self.height, self.width, 3), np.uint8)
        interval = 1.0 / max(1, self.fps)
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self._dirty:
                self._render_into(buf)
            proc = self._proc
            if proc is None or proc.stdin is None:
                break
            try:
                proc.stdin.write(buf.tobytes())
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._error = f"ffmpeg stream failed: {exc}"
                break
            dt = time.monotonic() - t0
            if dt < interval:
                time.sleep(interval - dt)
