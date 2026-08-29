"""Deepgram Nova-3 real-time streaming transcription source.

Streams mic audio to Deepgram's WebSocket API and converts results into the
exact same Segment/Word shape that live_transcribe.py produces, so buffer.py,
intent_router.py, and everything downstream requires ZERO changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Optional, Callable

import numpy as np

import config
from transcribe import Segment, Word
from transcription_source import TranscriptionSource

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")

SAMPLE_RATE = 16000
FRAME_BYTES = int(SAMPLE_RATE * 0.1) * 2  # ~100ms of int16 mono = 3200 bytes


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return key[:4] + "..." + key[-4:]


class DeepgramSource(TranscriptionSource):
    """Live transcription via Deepgram Nova-3 streaming WebSocket API.

    Audio is read from the audio source in a background thread, fed into an
    asyncio.Queue via ``run_coroutine_threadsafe``, and streamed to Deepgram
    without ever blocking the event loop.  Results are converted to
    :class:`Segment` / :class:`Word` objects matching the shape produced by
    the offline Whisper path exactly.
    """

    def __init__(
        self,
        model: str = "nova-3",
        language: str = "en",
        smart_format: bool = True,
        interim_results: bool = True,
        utterance_end_ms: int = config.DEEPGRAM_UTTERANCE_END_MS,
    ):
        self.model = model
        self.language = language
        self.smart_format = smart_format
        self.interim_results = interim_results
        self.utterance_end_ms = utterance_end_ms
        self._on_segment: Optional[Callable[[Segment], None]] = None
        self._on_interim: Optional[Callable[[Segment], None]] = None
        self._recent_interims: deque = deque(maxlen=6)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Active utterance tracking: interims update in-place, finals commit.
        self._active_utterance: Optional[dict] = None  # {"start": float, "text": str, "words": list}

    @property
    def name(self) -> str:
        return "deepgram"

    def start(self, audio_source, on_segment: Callable[[Segment], None],
              on_interim: Optional[Callable[[Segment], None]] = None) -> None:
        self._on_segment = on_segment
        self._on_interim = on_interim
        self._audio_source = audio_source
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(audio_source,), daemon=True, name="fv-deepgram",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if hasattr(self, "_audio_source") and self._audio_source is not None:
            try:
                self._audio_source.stop()
            except Exception:
                pass
            self._audio_source = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Internal — event loop runs in a dedicated thread
    # ------------------------------------------------------------------

    def _run(self, audio_source) -> None:
        key = DEEPGRAM_API_KEY
        if not key:
            print("[deepgram] DEEPGRAM_API_KEY not set -- cannot start")
            return

        params = (
            f"model={self.model}"
            f"&language={self.language}"
            f"&smart_format={'true' if self.smart_format else 'false'}"
            f"&encoding=linear16"
            f"&sample_rate={SAMPLE_RATE}"
            f"&channels=1"
            f"&interim_results={'true' if self.interim_results else 'false'}"
            f"&endpointing={self.utterance_end_ms}"
        )
        url = f"wss://api.deepgram.com/v1/listen?{params}"

        masked = _mask_key(key)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        audio_q: asyncio.Queue = asyncio.Queue(maxsize=300)
        reader_done = threading.Event()

        # ---- audio reader thread → feeds audio_q via run_coroutine_threadsafe ----
        def _audio_reader():
            try:
                for samples, _offset in audio_source.stream():
                    if self._stop.is_set():
                        break
                    if samples.size == 0:
                        continue
                    chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
                    int16 = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                    if loop.is_closed():
                        break
                    for pos in range(0, len(int16), FRAME_BYTES):
                        frame = int16[pos:pos + FRAME_BYTES]
                        if not frame:
                            break
                        try:
                            asyncio.run_coroutine_threadsafe(audio_q.put(frame), loop)
                        except RuntimeError:
                            break
            except Exception as exc:
                print(f"[deepgram] audio read error: {exc}")
            finally:
                reader_done.set()

        reader = threading.Thread(target=_audio_reader, daemon=True, name="fv-dg-reader")
        reader.start()

        # ---- async sender: drains audio_q into the Deepgram WebSocket ----
        async def _sender(ws):
            sent_bytes = 0
            while not self._stop.is_set():
                data = await audio_q.get()
                if data is None:
                    try:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass
                    break
                try:
                    await ws.send(data)
                    sent_bytes += len(data)
                except Exception:
                    break

        MAX_RETRIES = 5
        BASE_DELAY = 2  # seconds

        async def _connect_once():
            import websockets

            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {key}"},
                ping_interval=5,
                close_timeout=2,
                open_timeout=10,
            ) as ws:
                print(f"[deepgram] connected to Deepgram, streaming audio")
                sender = asyncio.ensure_future(_sender(ws))
                msg_count = 0
                try:
                    async for raw_msg in ws:
                        if self._stop.is_set():
                            break
                        self._on_deepgram_message(raw_msg)
                        msg_count += 1
                finally:
                    sender.cancel()
                    try:
                        await sender
                    except asyncio.CancelledError:
                        pass
                print(f"[deepgram] processed {msg_count} messages from Deepgram")

        async def _main():
            for attempt in range(1, MAX_RETRIES + 1):
                if self._stop.is_set():
                    break
                print(f"[deepgram] connecting to Deepgram {self.model} (key={masked}) [attempt {attempt}/{MAX_RETRIES}]")
                try:
                    await _connect_once()
                    break  # normal disconnect — no retry needed
                except Exception as exc:
                    print(f"[deepgram] WebSocket error: {exc}")
                    if attempt < MAX_RETRIES:
                        delay = min(BASE_DELAY * (2 ** (attempt - 1)), 30)
                        print(f"[deepgram] reconnecting in {delay}s ...")
                        await asyncio.sleep(delay)

        try:
            loop.run_until_complete(_main())
        except Exception as exc:
            print(f"[deepgram] event loop error: {exc}")
        finally:
            self._stop.set()  # signal audio reader to stop
            try:
                loop.run_until_complete(asyncio.sleep(0))  # drain pending callbacks
            except Exception:
                pass
            loop.close()
            reader.join(timeout=2.0)
            print("[deepgram] disconnected")

    def _on_deepgram_message(self, raw_msg) -> None:
        try:
            msg = json.loads(raw_msg if isinstance(raw_msg, str) else raw_msg.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        msg_type = msg.get("type", "")

        if msg_type == "Results":
            channel = msg.get("channel", {})
            alternatives = channel.get("alternatives", [])
            if not alternatives:
                return

            alt = alternatives[0]
            transcript = (alt.get("transcript") or "").strip()
            if not transcript:
                return

            is_final = msg.get("is_final", True)
            # speech_final: True when the speaker has stopped talking for this
            # utterance.  Deepgram sends multiple is_final=True results as the
            # transcript is refined; speech_final marks the true end.
            speech_final = alt.get("speech_final", False) or msg.get("speech_final", False)

            words = []
            for w in alt.get("words", []):
                word_text = w.get("punctuated_word") or w.get("word", "")
                if word_text:
                    words.append(Word(
                        text=word_text.strip(),
                        start_time=float(w.get("start", 0.0)),
                        end_time=float(w.get("end", 0.0)),
                        probability=float(w.get("confidence", 1.0)),
                    ))

            start_time = float(msg.get("start", 0.0))
            duration = float(msg.get("duration", 0.0))
            end_time = start_time + duration

            seg = Segment(transcript, start_time, end_time, words)

            if is_final:
                # Final result: commit the active utterance and deliver it.
                # Deepgram may send multiple finals for the same utterance as
                # the transcript is refined — only commit on speech_final or
                # when the start time changes (new utterance).
                if self._active_utterance is not None:
                    active_start = self._active_utterance["start"]
                    # Same utterance, refined transcript — update in place
                    if abs(active_start - start_time) < 0.5:
                        self._active_utterance["text"] = transcript
                        self._active_utterance["words"] = words
                        self._active_utterance["end"] = end_time
                        # Only deliver on speech_final (true utterance boundary)
                        if not speech_final:
                            return
                    # Different utterance — commit the old one first
                    else:
                        self._commit_active_utterance()

                # Build and deliver the final segment
                self._active_utterance = None
                cb = self._on_segment
                if cb is not None:
                    try:
                        cb(seg)
                    except Exception as exc:
                        print(f"[deepgram] on_segment callback error: {exc}")
            else:
                # Interim result: update the active utterance in place.
                if self._active_utterance is not None:
                    active_start = self._active_utterance["start"]
                    # Same utterance — replace text (progressive refinement)
                    if abs(active_start - start_time) < 0.5:
                        self._active_utterance["text"] = transcript
                        self._active_utterance["words"] = words
                        self._active_utterance["end"] = end_time
                    else:
                        # Different utterance — commit old, start new
                        self._commit_active_utterance()
                        self._active_utterance = {
                            "start": start_time, "text": transcript,
                            "words": words, "end": end_time,
                        }
                else:
                    # No active utterance — start tracking
                    self._active_utterance = {
                        "start": start_time, "text": transcript,
                        "words": words, "end": end_time,
                    }

                # Deliver interim to callback (display + pipeline update)
                cb = self._on_interim
                if cb is not None:
                    try:
                        cb(seg)
                    except Exception as exc:
                        print(f"[deepgram] on_interim callback error: {exc}")

        elif msg_type == "UtteranceEnd":
            # UtteranceEnd signals the speaker has stopped.  Commit whatever
            # active utterance we have.
            self._commit_active_utterance()

        elif msg_type == "Error":
            desc = msg.get("description", msg.get("message", str(msg)))
            print(f"[deepgram] server error: {desc}")

    def _commit_active_utterance(self) -> None:
        """Deliver the active utterance as a finalized segment and clear state."""
        if self._active_utterance is None:
            return
        au = self._active_utterance
        self._active_utterance = None
        seg = Segment(
            text=au["text"],
            start_time=au["start"],
            end_time=au.get("end", au["start"]),
            words=au.get("words", []),
        )
        cb = self._on_segment
        if cb is not None:
            try:
                cb(seg)
            except Exception as exc:
                print(f"[deepgram] on_segment callback error: {exc}")


# ---------------------------------------------------------------------------
# Standalone test: run DeepgramSource directly against the mic.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Deepgram live transcription test")
    ap.add_argument("--device", type=int, default=None, help="mic device index")
    args = ap.parse_args()

    from live_transcribe import MicSource, _ensure_portaudio

    _ensure_portaudio()
    source = DeepgramSource()

    def _print_segment(seg: Segment):
        print(f"[{seg.start_time:6.2f}-{seg.end_time:6.2f}] {seg.text}")

    def _print_interim(seg: Segment):
        print(f"[{seg.start_time:6.2f}-{seg.end_time:6.2f}] <interim> {seg.text}")

    print("[deepgram] live mic test | Nova-3 | Ctrl-C to stop")
    mic = MicSource(device=args.device)
    source.start(mic, _print_segment, on_interim=_print_interim)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[deepgram] stopping ...")
    finally:
        source.stop()
