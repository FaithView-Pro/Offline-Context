"""Milestone 2 -- LIVE (STREAMING) TRANSCRIPTION.

faster-whisper is a *batch* transcriber (it transcribes a whole audio array at
once); it does not natively stream. This module implements the rolling-window
chunking the brief calls for so we still get live captions from a microphone,
fully offline:

  * capture mic audio continuously and locally (sounddevice, PortAudio) -- no
    cloud STT anywhere.
  * buffer into overlapping chunks (default ~4s window, ~1s overlap).
  * transcribe each chunk with the SAME whisper model/settings the file pipeline
    already uses (``transcribe._load_whisper`` + ``config.Settings``).
  * de-duplicate the overlapping region between consecutive chunks by trimming
    the leading words of the new chunk that already appeared as the trailing
    words of the previous chunk.
  * emit ``Segment``/``Word`` objects with the EXACT same shape as
    ``transcribe.py``, so ``buffer.py`` (Stage 2) needs ZERO changes -- it does
    not care whether segments came from a file or a live stream.

Why overlapping windows at all? Pure 4s tiles would split words across the seam
and lose them. A 1s overlap means a word straddling the seam is captured whole
in at least one tile; de-dup then collapses the doubled region.

A genuine microphone is not always available (headless CI, no mic). The audio
SOURCE is therefore an interface: ``MicSource`` (sounddevice) for the real live
case, and ``FileSource`` (decodes an audio file and replays it as a chunk
stream) for offline testing of the exact same rolling-window + de-dup path.
``python live_transcribe.py`` runs the mic; ``--file BIM.mp3`` runs the file
source so the chunking logic is verifiable without a microphone.

PortAudio is bundled at ``lib/libportaudio.so.2`` and loaded with a tiny
``ctypes.util.find_library`` shim so the live app is self-contained and offline:
the operator never needs to set LD_LIBRARY_PATH by hand.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from math import gcd
from typing import Optional, Callable, Iterator

import numpy as np

import config
from transcribe import Segment, Word, _load_whisper


SAMPLE_RATE = 16000          # Whisper wants 16 kHz mono


# ===========================================================================
# scipy.signal lazy loader -- used for anti-aliased resampling + high-pass.
# Lazy (not imported at module top) so server/import-time stays cheap and so the
# app still runs (with degrade-to-prior linear-interp path) if scipy is absent.
# ===========================================================================
_SCIPY_SIGNAL = None  # None=uninitialized, False=unavailable, module=loaded


def _scipy_signal():
    global _SCIPY_SIGNAL
    if _SCIPY_SIGNAL is None:
        try:
            from scipy import signal
            _SCIPY_SIGNAL = signal
        except Exception:
            _SCIPY_SIGNAL = False
    return _SCIPY_SIGNAL if _SCIPY_SIGNAL is not False else None


# High-pass filter coeffs cached per (fs, cutoff) so we don't re-design on every
# 4-5s chunk (radioactive: sounds a tiny bit louder-quiet-but-cleaner). Whisper
# is run at 16 kHz so this is effectively a single design for the whole session.
_HP_CACHE: dict[tuple[int, float], object] = {}


def _highpass(chunk: np.ndarray, fs: int = SAMPLE_RATE, cutoff: float = 85.0) -> np.ndarray:
    """2nd-order Butterworth high-pass at ``cutoff`` Hz. Strips HVAC rumble,
    50/60 Hz electrical hum and DC offset that the mic captures and a clean MP3
    does NOT have. Coeffs are cached per (fs, cutoff); falls back to identity if
    scipy is unavailable. Idempotent if the same audio passes through twice
    (overlapping windows) because the filter is linear + time-invariant."""
    sig = _scipy_signal()
    if sig is None or chunk.size == 0:
        return chunk
    key = (fs, cutoff)
    sos = _HP_CACHE.get(key)
    if sos is None:
        try:
            sos = sig.butter(2, cutoff / (fs / 2.0), btype="highpass", output="sos")
            _HP_CACHE[key] = sos
        except Exception:
            _HP_CACHE[key] = False
            sos = False
    if sos is False:
        return chunk
    try:
        return sig.sosfilt(sos, chunk).astype(np.float32, copy=False)
    except Exception:
        return chunk


def _preprocess_audio(chunk: np.ndarray, fs: int = SAMPLE_RATE) -> np.ndarray:
    """Light mic cleanup: high-pass rumble removal only.

    No amplitude scaling -- Whisper per-channel-normalizes its log-mel input
    internally, so an absolute-level change to the PCM is net-neutral on SNR
    while BOOSTING the noise floor on quiet chunks (a click during silence
    would otherwise lift mic hiss into the speech band). High-pass is subtractive
    noise Whisper does NOT remove itself, so it is a pure win.
    """
    if chunk.size == 0:
        return chunk.astype(np.float32, copy=False)
    return _highpass(chunk, fs=fs).astype(np.float32, copy=False)


# ===========================================================================
# PortAudio bootstrap (self-contained offline -- no env vars required by hand)
# ===========================================================================
def _ensure_portaudio() -> None:
    """Make sounddevice importable using a bundled libportaudio, if present.

    sounddevice finds its lib via ``ctypes.util.find_library('portaudio')``,
    which on Linux only sees the ldconfig cache -- not our project-local copy.
    So we (a) preload the bundled .so with RTLD_GLOBAL, and (b) patch
    ``find_library`` to return the bundled path for the 'portaudio' probe. This
    keeps the live app a single self-contained, offline directory.
    """
    import ctypes
    import ctypes.util

    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(here, "lib", "libportaudio.so.2")
    if not os.path.exists(bundled):
        return  # fall back to whatever the system has (may raise on import sd)

    try:
        ctypes.CDLL(bundled, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass

    _orig_find = ctypes.util.find_library

    def _patched(name):
        if name == "portaudio" and os.path.exists(bundled):
            return bundled
        return _orig_find(name)

    ctypes.util.find_library = _patched


# ===========================================================================
# Audio sources -- everything that feeds the rolling transcriber looks the same
# ===========================================================================
class AudioSource:
    """Produce mono float32 PCM at SAMPLE_RATE as an iterable stream.

    ``stream()`` yields ``(samples, capture_offset_sec)`` where
    ``capture_offset_sec`` is the wall-clock offset (seconds since the source
    started) of the FIRST sample in ``samples``. Mic sources compute these from
    the audio clock; file-replay sources compute them from real time (so they
    pace like live speech) or from the file clock (so they run as fast as the
    transcriber can keep up).
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def stream(self) -> Iterator[tuple[np.ndarray, float]]: raise NotImplementedError


class MicSource(AudioSource):
    """Live microphone capture via PortAudio/sounddevice.

    Whisper wants 16 kHz mono, but most hardware mics only offer 44100/48000 Hz
    (this box's input rejects 16000 with ``Invalid sample rate -9997``). So we
    capture at the device's best-supported rate and resample down to 16 kHz so
    the rolling-window math stays in 16 kHz. Offsets are wall-clock seconds, so
    they are unchanged by resampling.

    The polyphase resample is applied to ~``hunk_seconds`` hunks in the consumer
    loop (NOT per PortAudio block) so the FIR filter keeps continuity across the
    hunk -- per-block resampling was the dominant cause of mic-vs-file
    transcription drift (see ``_callback`` and ``stream``).
    """

    # Candidate device sample rates to try, in preference order.
    _DEVICE_RATES = (SAMPLE_RATE, 48000, 44100, 32000, 24000, 22050, 96000, 88200)

    def __init__(self, device: Optional[int] = None, block_seconds: float = 0.5,
                 requested_rate: int = SAMPLE_RATE, hunk_seconds: float = 2.0):
        _ensure_portaudio()
        import sounddevice as sd  # noqa
        self._sd = sd
        self.device = device
        self.block_seconds = block_seconds
        self.requested_rate = requested_rate
        self.dev_rate = requested_rate   # resolved on start()
        # Resample in larger hunks, NOT per PortAudio block. The polyphase FIR
        # behind ``scipy.signal.resample_poly`` has a startup transient and a
        # truncated tail; calling it once per ~0.5s mic block therefore sows an
        # 8-Hz train of micro-glitches into every 4s chunk Whisper sees -- the
        # audible hiss/clicks that make the live mic mistranscribe where the
        # same words read off a file decode cleanly. Buffer ~hunk_seconds of
        # *device-rate* audio and resample whole hunks instead, so the boundary
        # artifact drops to a single transient per hunk and the mic PCM that
        # reaches Whisper is as clean as PyAV's FileSource output (which
        # resamples one continuous file stream). This is the live-vs-file
        # accuracy lever the original docstring already pointed at -- it now
        # lives in the consumer loop, not in the audio-thread callback.
        self.hunk_seconds = hunk_seconds
        self._q: "queue.Queue[Optional[tuple[np.ndarray, float]]]" = queue.Queue(maxsize=400)
        self._stream = None
        self._start_time = 0.0
        self._samples_seen = 0  # for capture-offset accounting
        self._stopped = False
        self._audio_clock_ok = False

    def _pick_rate(self) -> int:
        """Pick a device-supported sample rate: the requested one first, then
        the device's native default, then the preference list."""
        sd = self._sd
        default_rate = None
        try:
            default_rate = sd.query_devices(self.device, 'input')["default_samplerate"]
        except Exception:
            pass

        order = [self.requested_rate]
        if default_rate is not None:
            order.append(default_rate)
        order += list(self._DEVICE_RATES)

        for rate in order:
            if not rate:
                continue
            try:
                sd.check_input_settings(device=self.device, channels=1,
                                        dtype="float32", samplerate=int(rate))
                return int(rate)
            except Exception:
                continue
        raise RuntimeError(f"mic device {self.device} supports none of {order}")

    def _resample_to_target(self, block: np.ndarray) -> np.ndarray:
        """High-quality anti-aliased resample from ``dev_rate`` to SAMPLE_RATE.

        The PREVIOUS implementation used ``np.interp`` (linear interpolation),
        which decimates WITHOUT a low-pass filter. High-frequency content above
        SAMPLE_RATE/2 (8 kHz) FOLDS BACK into the speech band as aliasing noise,
        which Whisper hears as whistling/hissing and mistranscribes. ``FileSource``
        uses PyAV's ``av.AudioResampler`` (windowed-sinc FIR), which is why a
        sermon MP3 transcribes cleaner than the same words spoken into the mic.
        We now match PyAV's quality here with ``scipy.signal.resample_poly``
        (polyphase FIR, same anti-aliasing design family). Resampling done RIGHT
        is the single biggest lever on live mic accuracy vs file accuracy.
        """
        if self.dev_rate == SAMPLE_RATE or block.size == 0:
            return block.astype(np.float32, copy=False)
        sig = _scipy_signal()
        if sig is not None:
            up = SAMPLE_RATE
            down = self.dev_rate
            try:
                g = gcd(up, down)
                out = sig.resample_poly(block, up // g, down // g)
                return out.astype(np.float32, copy=False)
            except Exception:
                pass  # fall through to linear-interp fallback (rare)
        # fallback: linear interp -- kept for envs without scipy (worse aliasing)
        src_n = block.size
        dst_n = int(round(src_n * SAMPLE_RATE / self.dev_rate))
        if dst_n <= 0:
            return np.zeros(0, dtype=np.float32)
        src_idx = np.arange(src_n, dtype=np.float64)
        dst_idx = np.linspace(0.0, max(src_n - 1, 1), num=dst_n, endpoint=False)
        return np.interp(dst_idx, src_idx, block).astype(np.float32)

    def _callback(self, indata, frames, time_info, status):
        # time_info.inputBufferDacTime is the ADC capture time of the first
        # frame. NOTE: we enqueue *device-rate* samples here and leave the
        # polyphase resample to ``stream()`` -- calling ``resample_poly`` per
        # ~0.5s block was the largest source of mic-vs-file transcription drift
        # (one filter-transient glitch per block concatenated into the chunk).
        try:
            if not getattr(self, "_audio_clock_ok", False):
                raise ValueError("Audio clock not OK")
            offset = time_info.inputBufferDacTime - self._start_time
        except Exception:
            offset = self._samples_seen / float(self.dev_rate)
        self._samples_seen += frames
        try:
            self._q.put_nowait((indata.copy().reshape(-1), float(offset)))
        except queue.Full:
            pass  # drop if the consumer falls far behind (back-pressure)

    def start(self):
        sd = self._sd
        self.dev_rate = self._pick_rate()
        self._start_time = time.time()  # fallback; refined on first callback
        self._stream = sd.InputStream(
            samplerate=self.dev_rate, channels=1, dtype="float32",
            blocksize=int(self.dev_rate * self.block_seconds),
            device=self.device, callback=self._callback,
        )
        self._stream.start()
        try:
            self._start_time = self._stream.time  # base audio clock
            self._audio_clock_ok = True
        except Exception:
            self._start_time = time.time()
            self._audio_clock_ok = False

    def stop(self):
        self._stopped = True
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        finally:
            self._q.put(None)

    def stream(self):
        self.start()
        # Consume DEVICE-RATE blocks from the callback and resample in hunks of
        # ``hunk_seconds``. Running the polyphase resample on a contiguous ~2s
        # window keeps the filter's startup/truncation artifacts at ONE per
        # hunk rather than ~8 per 4s chunk, so Whisper sees clean PCM like
        # FileSource produces via PyAV's continuous resampler. The downstream
        # ``LiveTranscriber._capture_loop`` is rate-agnostic -- it just
        # accumulates whatever (samples, offset) blocks we yield into 4s
        # windows, so making the block ~2s instead of ~0.5s is a transparent
        # no-op for it (only its `carry` accumulates fewer, larger items).
        hunk_target = int(self.dev_rate * self.hunk_seconds)
        blocks: list[np.ndarray] = []
        n = 0
        base_offset = 0.0
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                dev_samples, offset = item
                if not blocks:
                    base_offset = offset
                blocks.append(dev_samples)
                n += len(dev_samples)
                if self._stopped:
                    break
                if n >= hunk_target:
                    raw = blocks[0] if len(blocks) == 1 else np.concatenate(blocks)
                    res = self._resample_to_target(raw)
                    yield res.reshape(-1), float(base_offset)
                    blocks = []
                    n = 0
            # flush any device-rate tail so the final partial hunk isn't lost
            if blocks:
                raw = blocks[0] if len(blocks) == 1 else np.concatenate(blocks)
                res = self._resample_to_target(raw)
                yield res.reshape(-1), float(base_offset)
        finally:
            self.stop()


class FileSource(AudioSource):
    """Replay an audio file as a chunk stream for offline testing of the live
    path. Decodes via PyAV (av / ffmpeg) to 16 kHz mono float32.

    ``realtime=False`` runs as fast as the transcriber can process (useful for
    benchmarks); ``realtime=True`` paces output to real wall-clock so it
    behaves like a live mic.
    """

    def __init__(self, path: str, realtime: bool = False, block_seconds: float = 0.5):
        self.path = path
        self.realtime = realtime
        self.block_seconds = block_seconds

    def _decode(self) -> np.ndarray:
        import av
        container = av.open(self.path)
        try:
            container.streams.audio[0]
        except (AttributeError, IndexError):
            container.close()
            raise ValueError(f"no audio stream in {self.path}")
        resampler = None
        out = []
        total = 0
        try:
            for frame in container.decode(audio=0):
                if resampler is None:
                    resampler = av.AudioResampler(
                        format="fltp", layout="mono", rate=SAMPLE_RATE,
                    )
                resampled = resampler.resample(frame)
                for rf in resampled:
                    arr = rf.to_ndarray().astype(np.float32).reshape(-1)
                    out.append(arr)
                    total += len(arr)
        finally:
            container.close()
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out)

    def stream(self):
        pcm = self._decode()
        block = int(SAMPLE_RATE * self.block_seconds)
        n = len(pcm)
        start = time.time()
        pos = 0
        while pos < n:
            chunk = pcm[pos:pos + block]
            this_block = len(chunk)
            offset = pos / SAMPLE_RATE
            yield chunk, float(offset)
            if self.realtime:
                target = start + offset + this_block / SAMPLE_RATE
                while time.time() < target:
                    time.sleep(0.01)
            pos += block


# ===========================================================================
# Rolling-window transcriber
# ===========================================================================
def _overlap_trim(prev_text: str, new_text: str) -> tuple[str, int]:
    """Trim leading words of ``new_text`` that already appeared as the trailing
    words of ``prev_text`` (the duplicated overlap region).

    Returns ``(trimmed_text, words_dropped)``. Word-level: finds the longest
    suffix of prev_words that equals a prefix of new_words, within a window of
    the overlap region so a coincidental short match doesn't over-trim.

    Simple by design -- intentionally not over-engineered for the first pass.
    """
    if not prev_text or not new_text:
        return new_text, 0
    pw = prev_text.split()
    nw = new_text.split()
    if not nw:
        return new_text, 0
    # search from the longest plausible overlap down, but cap the search so we
    # don't trim a whole repeated sentence off a fresh chunk
    max_k = min(len(pw), len(nw), 10)
    best = 0
    for k in range(max_k, 0, -1):
        if pw[-k:] == nw[:k]:
            best = k
            break
    if best == 0:
        return new_text, 0
    return " ".join(nw[best:]), best


@dataclass
class _PendingChunk:
    audio: np.ndarray
    offset: float   # capture offset (s) of first sample
    seconds: float  # total audio duration of chunk


class LiveTranscriber:
    """Rolling-window live transcriber.

    Consumes ``AudioSource.stream()``; for each window (~``chunk_seconds`` s of
    audio with ``overlap_seconds`` s carried over from the previous window),
    transcribes the chunk with Whisper, de-duplicates the overlap, and emits a
    ``Segment`` via the ``on_segment`` callback. Timestamps are absolute seconds
    since the source started (``chunk_offset + seg.start``).
    """

    def __init__(
        self,
        on_segment: Callable[[Segment], None],
        model_name: str = config.WHISPER_MODEL,
        chunk_seconds: float = 4.0,
        overlap_seconds: float = 1.0,
        language: Optional[str] = config.WHISPER_LANGUAGE,
        beam_size: int = config.WHISPER_BEAM_SIZE,
        vad_filter: bool = True,
        vad_min_silence_ms: int = config.WHISPER_VAD_MIN_SILENCE_MS,
        offline: bool = config.OFFLINE,
        word_timestamps: bool = False,
        max_queue: int = 50,
        initial_prompt: Optional[str] = None,
        condition_on_previous_text: bool = False,
        preprocess: bool = True,
        min_avg_logprob: float = config.WHISPER_MIN_AVG_LOGPROB,
        max_no_speech_prob: float = config.WHISPER_MAX_NO_SPEECH_PROB,
    ):
        self.on_segment = on_segment
        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.vad_min_silence_ms = vad_min_silence_ms
        self.word_timestamps = word_timestamps
        self.offline = offline
        self.model_name = model_name
        # New live-accuracy knobs:
        #   initial_prompt -- biases decoding toward domain vocabulary (scripture,
        #     archaic pronouns) so Whisper doesn't transliterate them to commons.
        #   condition_on_previous_text -- defaulting to FALSE for live chunked
        #     transcription; on rolling ~4s tiles this flag's True-behavior
        #     splices "...continuing..." hallucinations onto the next verse.
        #   preprocess -- mic-only high-pass cleanup (HVAC/DC rumble).  Set False
        #     to compare A/B vs FileSource on the same chunk path.
        #   min_avg_logprob / max_no_speech_prob -- per-segment confidence filters
        #     (Task 2). Skip emitting a segment if Whisper itself reports very low
        #     token confidence (likely hallucinated) or high probability of
        #     silence/noise. Tunable via config.py so they can be adjusted
        #     empirically without a code change.
        self.initial_prompt = initial_prompt or config.WHISPER_INITIAL_PROMPT
        self.condition_on_previous_text = condition_on_previous_text
        self.preprocess = preprocess
        self.min_avg_logprob = min_avg_logprob
        self.max_no_speech_prob = max_no_speech_prob
        self._model = None
        self._pending: "queue.Queue[Optional[_PendingChunk]]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._capture_thread = None
        self._transcribe_thread = None
        self._prev_text = ""
        self._prev_chunk_audio = None  # overlap carry-over (raw PCM)
        # diagnostics for tuning (processing vs duration)
        self.chunk_count = 0
        self.total_proc_sec = 0.0
        self.total_audio_sec = 0.0

    def _load_model(self):
        if self._model is None:
            self._model = _load_whisper(self.model_name, self.offline)

    # ---- public control ----
    def start(self, source: AudioSource):
        self._load_model()
        self._stop.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, args=(source,), name="fv-capture", daemon=True)
        self._transcribe_thread = threading.Thread(
            target=self._transcribe_loop, name="fv-transcribe", daemon=True)
        self._capture_thread.start()
        self._transcribe_thread.start()

    def stop(self):
        self._stop.set()
        self._pending.put(None)
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
        if self._transcribe_thread:
            self._transcribe_thread.join(timeout=5.0)

    def join(self):
        if self._transcribe_thread:
            self._transcribe_thread.join()

    # ---- capture: assemble full windows from the source block stream ----
    def _capture_loop(self, source: AudioSource):
        win_samples = int(self.chunk_seconds * SAMPLE_RATE)
        half = win_samples // 2
        overlap_samples = int(self.overlap_seconds * SAMPLE_RATE)
        carry: list[np.ndarray] = []
        carry_samples = 0
        base_offset = 0.0
        for block, offset in source.stream():
            if self._stop.is_set():
                break
            base_offset = offset
            carry.append(block)
            carry_samples += len(block)
            # emit windows whenever we've collected >= chunk_seconds of NEW audio
            while carry_samples >= win_samples and not self._stop.is_set():
                buf = np.concatenate(carry) if len(carry) > 1 else carry[0]
                window_audio = buf[:win_samples]
                remainder = buf[win_samples:]
                # window's first sample offset:
                woff = base_offset - (carry_samples - win_samples) / SAMPLE_RATE
                chunk_audio = self._with_overlap(window_audio)
                try:
                    self._pending.put(_PendingChunk(chunk_audio, float(woff), float(self.chunk_seconds)))
                except queue.Full:
                    pass
                carry = [remainder] if len(remainder) else []
                carry_samples = len(remainder)
        # final flush of the tail
        if carry_samples > 0 and not self._stop.is_set():
            buf = np.concatenate(carry) if len(carry) > 1 else carry[0]
            woff = base_offset - (carry_samples - win_samples) / SAMPLE_RATE if carry_samples >= win_samples else base_offset
            try:
                self._pending.put(_PendingChunk(buf, float(max(woff, 0.0)), float(len(buf) / SAMPLE_RATE)))
            except queue.Full:
                pass
        self._pending.put(None)

    def _with_overlap(self, window_audio: np.ndarray) -> np.ndarray:
        """Prepend the trailing ``overlap_seconds`` of the previous chunk's
        audio so the seam word is captured whole in this tile too."""
        if self._prev_chunk_audio is None or self.overlap_seconds <= 0:
            self._prev_chunk_audio = window_audio
            return window_audio
        ov = int(self.overlap_seconds * SAMPLE_RATE)
        prev_tail = self._prev_chunk_audio[-ov:]
        chunk = np.concatenate([prev_tail, window_audio])
        self._prev_chunk_audio = window_audio
        return chunk

    # ---- transcribe: Whisper + de-dup + Segment emission ----
    def _transcribe_loop(self):
        while True:
            item = self._pending.get()
            if item is None:
                break
            if self._stop.is_set():
                break
            self._process_chunk(item)

    def _process_chunk(self, pc: _PendingChunk):
        t0 = time.time()
        audio = _preprocess_audio(pc.audio) if self.preprocess else pc.audio
        try:
            segs, _info = self._model.transcribe(
                audio,
                beam_size=self.beam_size,
                language=self.language,
                vad_filter=self.vad_filter,
                vad_parameters={"min_silence_duration_ms": self.vad_min_silence_ms},
                word_timestamps=self.word_timestamps,
                initial_prompt=self.initial_prompt,
                condition_on_previous_text=self.condition_on_previous_text,
                without_timestamps=False,
            )
            segs = list(segs)   # forces the actual transcription work
        except Exception as exc:  # never let one bad tile kill the live stream
            proc = time.time() - t0
            print(f"[live_transcribe] chunk failed after {proc:.2f}s: {exc}")
            return
        proc = time.time() - t0
        self.chunk_count += 1
        self.total_proc_sec += proc
        self.total_audio_sec += pc.seconds

        for seg in segs:
            # Task 2 per-segment confidence filters (tune via config.py):
            ns = getattr(seg, "no_speech_prob", None)
            al = getattr(seg, "avg_logprob", None)
            if ns is not None and ns > self.max_no_speech_prob:
                continue  # likely silence / background noise
            if al is not None and al < self.min_avg_logprob:
                continue  # very low token confidence -> likely hallucinated
            text = (seg.text or "").strip()
            if not text:
                continue
            trimmed, dropped = _overlap_trim(self._prev_text, text)
            self._prev_text = text if not trimmed else trimmed
            if not trimmed.strip():
                continue
            words = []
            if self.word_timestamps and getattr(seg, "words", None):
                for w in seg.words:
                    words.append(Word(w.word.strip(),
                                      float(w.start) + pc.offset,
                                      float(w.end) + pc.offset,
                                      float(w.probability)))
            abs_start = float(seg.start) + pc.offset
            abs_end = float(seg.end) + pc.offset
            out = Segment(trimmed, abs_start, abs_end, words)
            try:
                self.on_segment(out)
            except Exception as exc:
                print(f"[live_transcribe] on_segment callback error: {exc}")
        if proc > 0 and pc.seconds > 0:
            ratio = proc / pc.seconds
            if ratio > 0.95:  # approaching realtime -> would lag
                print(f"[live_transcribe] tile {self.chunk_count}: proc={proc:.2f}s "
                      f"for {pc.seconds:.1f}s audio ({ratio:.2f}x realtime)")

    # ---- simple synchronous helper for tests / single-segment callbacks ----
    def iter_segments(self, source: AudioSource, max_chunks: Optional[int] = None) -> Iterator[Segment]:
        """Drive capture+transcribe synchronously and yield Segment-s.

        THIS IS A GENERATOR -- you must iterate it for anything to run. Used by
        ``--file`` and by tests so the same path runs without threads. Each
        yielded segment is also passed to ``on_segment`` so a CLI console
        printer sees it.
        """
        self._load_model()
        self._prev_text = ""
        self._prev_chunk_audio = None
        win_samples = int(self.chunk_seconds * SAMPLE_RATE)
        carry: list[np.ndarray] = []
        carry_samples = 0
        base_offset = 0.0
        count = 0
        for block, offset in source.stream():
            if self._stop.is_set():
                break
            base_offset = offset
            carry.append(block)
            carry_samples += len(block)
            while carry_samples >= win_samples:
                buf = np.concatenate(carry) if len(carry) > 1 else carry[0]
                window_audio = buf[:win_samples]
                remainder = buf[win_samples:]
                woff = base_offset - (carry_samples - win_samples) / SAMPLE_RATE
                chunk_audio = self._with_overlap(window_audio)
                pc = _PendingChunk(chunk_audio, float(max(woff, 0.0)), float(self.chunk_seconds))
                for seg in self._process_chunk_sync(pc):
                    try:
                        self.on_segment(seg)
                    except Exception as exc:
                        print(f"[live_transcribe] on_segment callback error: {exc}")
                    yield seg
                    count += 1
                    if max_chunks and count >= max_chunks:
                        return
                carry = [remainder] if len(remainder) else []
                carry_samples = len(remainder)

    def _process_chunk_sync(self, pc: _PendingChunk) -> list[Segment]:
        out_segs: list[Segment] = []
        t0 = time.time()
        audio = _preprocess_audio(pc.audio) if self.preprocess else pc.audio
        try:
            segs, _info = self._model.transcribe(
                audio, beam_size=self.beam_size, language=self.language,
                vad_filter=self.vad_filter,
                vad_parameters={"min_silence_duration_ms": self.vad_min_silence_ms},
                word_timestamps=self.word_timestamps,
                initial_prompt=self.initial_prompt,
                condition_on_previous_text=self.condition_on_previous_text,
            )
            segs = list(segs)   # forces the actual transcription work
        except Exception as exc:
            print(f"[live_transcribe] chunk failed after {time.time()-t0:.2f}s: {exc}")
            return out_segs
        proc = time.time() - t0
        self.chunk_count += 1
        self.total_audio_sec += pc.seconds
        self.total_proc_sec += proc
        for seg in segs:
            ns = getattr(seg, "no_speech_prob", None)
            al = getattr(seg, "avg_logprob", None)
            if ns is not None and ns > self.max_no_speech_prob:
                continue
            if al is not None and al < self.min_avg_logprob:
                continue
            text = (seg.text or "").strip()
            if not text:
                continue
            trimmed, dropped = _overlap_trim(self._prev_text, text)
            self._prev_text = text if not trimmed else trimmed
            if not trimmed.strip():
                continue
            abs_start = float(seg.start) + pc.offset
            abs_end = float(seg.end) + pc.offset
            out_segs.append(Segment(trimmed, abs_start, abs_end))
        return out_segs


# ===========================================================================
# Convenience: turn a plain callback into a rolling printer (for the CLI)
# ===========================================================================
def _console_on_segment(seg: Segment):
    print(f"[{seg.start_time:6.2f}-{seg.end_time:6.2f}] {seg.text}")


# ===========================================================================
# CLI: real mic, or file-source dry run; plus a tiny benchmark mode
# ===========================================================================
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="FaithView Pro live transcription (offline).")
    ap.add_argument("--file", help="decode this file instead of the mic (offline dry-run)")
    ap.add_argument("--realtime", action="store_true",
                    help="pace file replay to wall-clock (default: as fast as possible)")
    ap.add_argument("--model", default=config.WHISPER_MODEL, help="faster-whisper model id")
    ap.add_argument("--chunk", type=float, default=4.0, help="chunk window seconds")
    ap.add_argument("--overlap", type=float, default=1.0, help="overlap seconds")
    ap.add_argument("--max-chunks", type=int, default=None, help="stop after N windows (file/dry-run)")
    ap.add_argument("--no-vad", action="store_true", help="disable VAD filter")
    ap.add_argument("--device", type=int, default=None, help="mic input device index")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--benchmark", action="store_true",
                    help="measure per-chunk proc vs audio duration and exit (file mode)")
    ap.add_argument("--initial-prompt", default=None,
                    help="Whisper initial_prompt; '' to DISABLE config default")
    ap.add_argument("--condition-previous-text", action="store_true",
                    help="opt back IN to whisper's condition_on_previous_text "
                         "(default OFF for live)")
    ap.add_argument("--no-preprocess", action="store_true",
                    help="disable high-pass mic cleanup (a/b vs file path)")
    args = ap.parse_args()

    _ensure_portaudio()
    import sounddevice as sd
    if args.list_devices:
        print("Input devices:")
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  [{i}] {d['name']}")
        return

    lt = LiveTranscriber(
        on_segment=_console_on_segment,
        model_name=args.model, chunk_seconds=args.chunk,
        overlap_seconds=args.overlap, vad_filter=not args.no_vad,
        initial_prompt=args.initial_prompt,
        condition_on_previous_text=bool(args.condition_previous_text),
        preprocess=not args.no_preprocess,
    )

    if args.file:
        src = FileSource(args.file, realtime=args.realtime)
        print(f"[live_transcribe] file dry-run: {args.file} | model={args.model} "
              f"chunk={args.chunk}s overlap={args.overlap}s")
        # iter_segments is a generator -- must iterate to drive transcription.
        for _seg in lt.iter_segments(src, max_chunks=args.max_chunks):
            pass
        rt = (lt.total_proc_sec / lt.total_audio_sec) if lt.total_audio_sec else 0.0
        print(f"\n[live_transcribe] chunks={lt.chunk_count} "
              f"audio={lt.total_audio_sec:.1f}s proc={lt.total_proc_sec:.2f}s "
              f"ratio={rt:.2f}x realtime -> {'OK' if rt < 1.0 else 'LAGGING'}")
        if args.benchmark:
            print("[live_transcribe] benchmark complete.")
        return

    # live mic
    src = MicSource(device=args.device)
    print(f"[live_transcribe] LIVE mic | model={args.model} chunk={args.chunk}s "
          f"overlap={args.overlap}s | Ctrl-C to stop")
    lt.start(src)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[live_transcribe] stopping ...")
    finally:
        lt.stop()
        rt = (lt.total_proc_sec / lt.total_audio_sec) if lt.total_audio_sec else 0.0
        print(f"[live_transcribe] chunks={lt.chunk_count} audio={lt.total_audio_sec:.1f}s "
              f"proc={lt.total_proc_sec:.2f}s ratio={rt:.2f}x realtime")


# ===========================================================================
# WhisperTranscriptionSource -- adapter so Whisper satisfies TranscriptionSource
# ===========================================================================
class WhisperTranscriptionSource:
    """Wraps LiveTranscriber into the TranscriptionSource interface.

    Loads the Whisper model once and reuses it across stop/start cycles so
    hot-swapping to Deepgram and back does not reload the model.
    """

    def __init__(
        self,
        model_name: str = config.WHISPER_MODEL,
        chunk_seconds: float = 4.0,
        overlap_seconds: float = 1.0,
        language: Optional[str] = config.WHISPER_LANGUAGE,
        beam_size: int = config.WHISPER_BEAM_SIZE,
        vad_filter: bool = True,
        vad_min_silence_ms: int = config.WHISPER_VAD_MIN_SILENCE_MS,
        offline: bool = config.OFFLINE,
        word_timestamps: bool = False,
        initial_prompt: Optional[str] = None,
        condition_on_previous_text: bool = False,
        preprocess: bool = True,
        min_avg_logprob: float = config.WHISPER_MIN_AVG_LOGPROB,
        max_no_speech_prob: float = config.WHISPER_MAX_NO_SPEECH_PROB,
    ):
        self.model_name = model_name
        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.vad_min_silence_ms = vad_min_silence_ms
        self.offline = offline
        self.word_timestamps = word_timestamps
        self.initial_prompt = initial_prompt
        self.condition_on_previous_text = condition_on_previous_text
        self.preprocess = preprocess
        self.min_avg_logprob = min_avg_logprob
        self.max_no_speech_prob = max_no_speech_prob
        self._transcriber: Optional[LiveTranscriber] = None
        self._on_segment: Optional[Callable[[Segment], None]] = None

    @property
    def name(self) -> str:
        return "whisper"

    def start(self, audio_source, on_segment: Callable[[Segment], None],
              on_interim: Optional[Callable[[Segment], None]] = None) -> None:
        self._on_segment = on_segment
        if self._transcriber is None:
            self._transcriber = LiveTranscriber(
                on_segment=on_segment,
                model_name=self.model_name,
                chunk_seconds=self.chunk_seconds,
                overlap_seconds=self.overlap_seconds,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
                vad_min_silence_ms=self.vad_min_silence_ms,
                offline=self.offline,
                word_timestamps=self.word_timestamps,
                initial_prompt=self.initial_prompt,
                condition_on_previous_text=self.condition_on_previous_text,
                preprocess=self.preprocess,
                min_avg_logprob=self.min_avg_logprob,
                max_no_speech_prob=self.max_no_speech_prob,
            )
        else:
            self._transcriber.on_segment = on_segment
            self._transcriber._prev_text = ""
            self._transcriber._prev_chunk_audio = None
        self._transcriber.start(audio_source)

    def stop(self) -> None:
        if self._transcriber is not None:
            try:
                self._transcriber.stop()
            except Exception as exc:
                print(f"[whisper_source] stop error: {exc}")

    # ---- passthrough for diagnostics ----
    @property
    def chunk_count(self) -> int:
        return self._transcriber.chunk_count if self._transcriber else 0

    @property
    def total_proc_sec(self) -> float:
        return self._transcriber.total_proc_sec if self._transcriber else 0.0

    @property
    def total_audio_sec(self) -> float:
        return self._transcriber.total_audio_sec if self._transcriber else 0.0


if __name__ == "__main__":
    _cli()