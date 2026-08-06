"""Milestone 4 -- LOCAL FASTAPI SERVER for the live operator console.

A single local FastAPI app that:

  * runs the live pipeline (mic capture -> transcribe -> router -> detect ->
    retrieve -> rerank -> score) in a background thread;
  * broadcasts live events over a WebSocket to connected UI clients; and
  * exposes REST endpoints for the operator's button actions.

Everything is local -- no external services. The WebSocket and REST shapes match
the brief.

Broadcast WebSocket event types (each is a JSON object with a ``type`` field so
the UI can switch on it):

    transcript_update  : {type, text, start_time, end_time}          (rolling)
    detection          : {type, reference, translation, text, confidence,
                         confidence_percent, confidence_band,
                         transcript_snippet, timestamp}              (a DetectionEvent)
    display_update     : {type, reference, translation, text} | {type, clear: true}
    queue_update       : {type, queue: [ {id, reference, translation, text, confidence, confidence_band?} ]}

REST endpoints (operator button actions):

    POST   /queue/add       body {reference, translation?, text?, confidence?, confidence_band?}
    POST   /present         body {reference, translation?, text?}   -> sets live display (+ anchor)
    DELETE /queue/{id}                                           -> removes queue item
    POST   /queue/clear                                          -> empties the queue
    GET    /queue                                                -> current queue JSON
    GET    /health                                               -> {ok, audio, floor}

--- Confidence policy (decision, clearly documented) -----------------------
config.REVIEW_THRESHOLD is 0.60; entries below that are "ignored" in the BATCH
report. For the LIVE operator console I DECIDE to still BROADCAST detections
whose rerank confidence is at/above a separate, lower LIVE CONFIDENCE FLOOR
(default 0.45) -- NOT cap the live feed at 0.60. Rationale:

  * A live operator benefits from *seeing more candidates* than a batch report
    emits; a verse read in noisy/fragmented live speech often lands at 0.45-0.60
    (measured: a genuine "I will put my spirit within you" surfaces at ~0.49
    with the small.en model on real sermon audio). Capping at 0.60 would hide the
    single most important live event.
  * score.py's banding still classifies these as "ignored" (0.45-0.60) vs
    "review queue" (0.60-0.96) vs "autopilot-eligible" (>=0.96), so the UI can
    COLOR-CODE by tier -- the operator sees the certainty, not a binary gate.
  * The floor is CONFIGURABLE (env ``LIVE_CONFIDENCE_FLOOR`` or ``--floor``),
    NOT hardcoded -- set to 0.0 to surface every retrieval hit, or 0.60 to
    match the batch report.

--- Implementation note: WebSocket + import order --------------------------
Importing ``fastapi``/``uvicorn`` LAZILY (i.e. after this repo's runtime modules
``config``/``live_pipeline``) was observed to make Starlette's WebSocket route
unreachable so uvicorn returns HTTP 403 on every WS handshake (no traceback, no
route match). Hoisting the fastapi/uvicorn imports to module top (before
``config``/``live_pipeline``) fixes it. This is therefore done explicitly below
and not moved back to lazy imports.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import asdict
from typing import Optional

# IMPORTANT: import the web stack BEFORE this repo's runtime modules. See the
# "Implementation note" above -- lazy imports broke the WebSocket handshake.
import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import config  # noqa: E402
from live_pipeline import LivePipeline, DetectionEvent, DisplayEvent, _parse_ref_string  # noqa: E402
from transcribe import Segment  # noqa: E402
from live_transcribe import MicSource, FileSource, _ensure_portaudio  # noqa: E402


# --- configurable live confidence floor (env-overridable) ------------------
LIVE_CONFIDENCE_FLOOR = float(os.environ.get("LIVE_CONFIDENCE_FLOOR", "0.45"))


# ===========================================================================
# Shared application state (one LivePipeline, one queue, WS broadcast bus)
# ===========================================================================
class AppState:
    def __init__(self):
        self.pipeline: Optional[LivePipeline] = None
        self.queue: list[dict] = []          # operator-curated list
        self.clients: set = set()            # websocket connections
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.audio_source_kind = "mic"      # "mic" | "file" | "none"
        self.audio_file: Optional[str] = None
        self.source = None
        self.audio_thread: Optional[threading.Thread] = None
        self.audio_stop = threading.Event()
        self.started = False
        self.lock = threading.Lock()        # guards queue mutations

    def init_pipeline(self, **kwargs):
        # The pipeline's callbacks push events onto the asyncio loop so the WS
        # broadcast happens on the event-loop thread even though transcription
        # runs on a worker thread.
        def _on_transcript(seg: Segment):
            self._emit({"type": "transcript_update",
                        "text": seg.text, "start_time": seg.start_time,
                        "end_time": seg.end_time})
        def _on_detection(ev: DetectionEvent):
            d = ev.as_dict()
            d["type"] = "detection"
            d["confidence_percent"] = round(ev.confidence * 100)
            self._emit(d)
        def _on_display(ev: DisplayEvent):
            if ev.clear:
                self._emit({"type": "display_update", "clear": True})
            else:
                self._emit({"type": "display_update", "reference": ev.reference,
                            "translation": ev.translation, "text": ev.text})
            # broadcast current_reference for the Bible reader panel
            if self.pipeline and self.pipeline.session.current_reference:
                t, b, c, v = self.pipeline.session.current_reference
                self._emit({"type": "session_update",
                            "translation": t, "book": b, "chapter": c, "verse": v})
        self.pipeline = LivePipeline(
            on_transcript=_on_transcript, on_detection=_on_detection,
            on_display=_on_display, **kwargs,
        )

    # ---- thread-safe broadcast: enqueue a coroutine onto the event loop ----
    def _emit(self, payload: dict):
        if self.loop is None:
            return
        if not getattr(self, "_emit_logged", False):
            self._emit_logged = True
            print(f"[server] _emit firing -> {payload.get('type')!r} "
                  f"(loop={self.loop!r})", flush=True)
        def _do():
            return self.broadcast(payload)
        try:
            asyncio.run_coroutine_threadsafe(_do(), self.loop)
        except RuntimeError:
            pass  # loop not running yet

    async def broadcast(self, payload: dict):
        if not getattr(self, "_bc_logged", False):
            self._bc_logged = True
            print(f"[server] broadcast -> {payload.get('type')!r} "
                  f"to {len(self.clients)} client(s)", flush=True)
        data = json.dumps(payload, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


STATE = AppState()


# ===========================================================================
# Pydantic request models -- defined at MODULE level (NOT nested in
# create_app) so FastAPI recognizes them as JSON request bodies rather than
# falling back to query parameters.
# ===========================================================================
class AddReq(BaseModel):
    reference: str
    translation: Optional[str] = None
    text: Optional[str] = None
    confidence: Optional[float] = None
    confidence_band: Optional[str] = None


class PresentReq(BaseModel):
    reference: str
    translation: Optional[str] = None
    text: Optional[str] = None


# ===========================================================================
# Queue + display operators (shared by REST + pipeline display events)
# ===========================================================================
def _new_queue_item(reference: str, translation: Optional[str], text: Optional[str],
                    confidence: Optional[float] = None,
                    confidence_band: Optional[str] = None) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "reference": reference,
        "translation": translation or "",
        "text": text or "",
        "confidence": round(float(confidence), 4) if confidence is not None else None,
        "confidence_band": confidence_band,
    }


async def _broadcast_queue():
    await STATE.broadcast({"type": "queue_update", "queue": STATE.queue})


async def _broadcast_display(ev: Optional[DisplayEvent], clear: bool = False):
    if clear or ev is None:
        await STATE.broadcast({"type": "display_update", "clear": True})
    else:
        await STATE.broadcast({"type": "display_update", "reference": ev.reference,
                               "translation": ev.translation, "text": ev.text})


# ===========================================================================
# FastAPI app -- NOTE no CORSMiddleware: it is unnecessary (frontend is served
# from this same origin) and was observed to 403-reject the WS handshake.
# ===========================================================================
def create_app(**pipeline_kwargs) -> "FastAPI":
    # audio_source_* are server-level controls, NOT LivePipeline kwargs.
    STATE.audio_source_kind = pipeline_kwargs.pop("audio_source_kind", "mic")
    STATE.audio_file = pipeline_kwargs.pop("audio_file", None)

    app = FastAPI(title="FaithView Pro live operator console")
    STATE.init_pipeline(**pipeline_kwargs)

    # --- lifecycle: start the live pipeline once the event loop is up ---
    @app.on_event("startup")
    async def _on_startup():
        # MUST be `async` so this runs ON the uvicorn event loop; a sync handler
        # runs in a worker thread where get_event_loop() returns a NON-running
        # loop, which would silently swallow every broadcast
        # (run_coroutine_threadsafe would schedule onto a dead loop).
        STATE.loop = asyncio.get_running_loop()
        # load DB/models in the background so import-time stays cheap
        t = threading.Thread(target=_bootstrap_pipeline, daemon=True,
                             name="fv-bootstrap")
        t.start()

    def _bootstrap_pipeline():
        try:
            STATE.pipeline._ensure_resources()
        except Exception as exc:
            print(f"[server] pipeline resource load failed: {exc}")
        kind = STATE.audio_source_kind
        if kind == "none":
            return
        if kind == "file" and STATE.audio_file:
            # drive a file as a mic substitute (offline/dev). Runs on a thread
            # because iter_segments is synchronous.
            STATE.audio_stop.clear()
            from live_transcribe import LiveTranscriber
            def _drive():
                lt = LiveTranscriber(
                    on_segment=STATE.pipeline._on_new_segment,
                    model_name=STATE.pipeline.s.whisper_model,
                    chunk_seconds=STATE.pipeline.chunk_seconds,
                    overlap_seconds=STATE.pipeline.overlap_seconds,
                    language=STATE.pipeline.s.whisper_language,
                    beam_size=STATE.pipeline.s.whisper_beam_size,
                    vad_filter=STATE.pipeline.s.whisper_vad_filter,
                    offline=STATE.pipeline.offline,
                    initial_prompt=(STATE.pipeline.whisper_initial_prompt
                                    if STATE.pipeline.whisper_initial_prompt is not None
                                    else STATE.pipeline.s.whisper_initial_prompt),
                    condition_on_previous_text=STATE.pipeline.condition_on_previous_text,
                    preprocess=STATE.pipeline.preprocess,
                )
                src = FileSource(STATE.audio_file, realtime=True)  # pace like a real mic so
                # the transcription thread yields the GIL between chunks and the
                # uvicorn loop can flush WS sends / pongs (as-fast-as-possible
                # replay starves the loop)
                try:
                    for _ in lt.iter_segments(src):
                        if STATE.audio_stop.is_set():
                            break
                except Exception as exc:
                    print(f"[server] file audio drive error: {exc}")
            STATE.audio_thread = threading.Thread(target=_drive, daemon=True,
                                                  name="fv-audio-file")
            STATE.audio_thread.start()
            return
        # real mic
        _ensure_portaudio()
        try:
            src = MicSource()
            STATE.source = src
            STATE.pipeline.start(src, quote_detector_kind=STATE.pipeline.quote_detector_kind)
            STATE.started = True
        except Exception as exc:
            print(f"[server] mic start failed: {exc}")

    @app.on_event("shutdown")
    def _on_shutdown():
        STATE.audio_stop.set()
        if STATE.pipeline:
            try:
                STATE.pipeline.stop()
            except Exception:
                pass

    # --- WebSocket ---
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        STATE.clients.add(ws)
        # send current state immediately so a reconnecting UI is in sync
        await ws.send_text(json.dumps({"type": "queue_update", "queue": STATE.queue}))
        try:
            while True:
                # keep the socket open; ignore inbound for now (could accept
                # operator chat later)
                await ws.receive_text()
        except WebSocketDisconnect:
            STATE.clients.discard(ws)
        except Exception:
            STATE.clients.discard(ws)

    # --- REST: operator actions ---
    @app.post("/queue/add")
    async def queue_add(req: AddReq):
        item = _new_queue_item(req.reference, req.translation, req.text,
                               req.confidence, req.confidence_band)
        with STATE.lock:
            STATE.queue.append(item)
        await _broadcast_queue()
        return item

    @app.post("/present")
    async def present(req: PresentReq):
        if STATE.pipeline is None:
            return JSONResponse({"error": "pipeline not ready"}, status_code=503)
        ev = STATE.pipeline.operator_present(
            req.reference, translation=req.translation, text=req.text,
            source="queue_present",
        )
        if ev is None:
            return JSONResponse({"error": "could not resolve reference"}, status_code=400)
        await _broadcast_display(ev)
        return asdict(ev)

    @app.delete("/queue/{item_id}")
    async def queue_remove(item_id: str):
        with STATE.lock:
            before = len(STATE.queue)
            STATE.queue = [q for q in STATE.queue if q["id"] != item_id]
            removed = before != len(STATE.queue)
        await _broadcast_queue()
        return {"removed": removed, "id": item_id}

    @app.post("/queue/clear")
    async def queue_clear():
        with STATE.lock:
            STATE.queue.clear()
        await _broadcast_queue()
        return {"cleared": True}

    @app.post("/display/clear")
    async def display_clear():
        # Operator "clear" -- hide the live display and broadcast to all UI
        # clients. NOTE: we deliberately do NOT reset the router's
        # current_reference (a later "next verse" must still anchor off the
        # last real verse shown), mirroring CLEAR_COMMAND semantics.
        await STATE.broadcast({"type": "display_update", "clear": True})
        return {"cleared": True}

    @app.get("/queue")
    async def queue_get():
        return {"queue": STATE.queue}

    @app.get("/health")
    async def health():
        return {"ok": True, "audio": STATE.audio_source_kind,
                "floor": STATE.pipeline.live_confidence_floor if STATE.pipeline else LIVE_CONFIDENCE_FLOOR}

    @app.get("/bible/{translation}/{book}/{chapter}")
    async def bible_chapter(translation: str, book: str, chapter: int):
        if STATE.pipeline is None or STATE.pipeline._db is None:
            return JSONResponse({"error": "Bible database not loaded yet"}, status_code=503)
        db = STATE.pipeline._db
        from bible_db import resolve_book
        from config import HERE as _here
        canon = resolve_book(book)
        if canon is None:
            # Treat raw book name from URL as-is; BibleDB.book_name/alias handles it
            pass
        verses = db.chapter_verses(translation, book, chapter)
        # If the translation wasn't found, try the default translation
        if not verses and translation != STATE.pipeline.default_translation:
            verses = db.chapter_verses(STATE.pipeline.default_translation, book, chapter)
            if verses:
                translation = STATE.pipeline.default_translation
        result = []
        for v in verses:
            result.append({
                "verse": v.verse,
                "text": v.text,
            })
        return {
            "translation": translation,
            "book": book,
            "chapter": chapter,
            "verses": result,
        }

    # --- serve the frontend (Milestone 5) ---
    @app.get("/", response_class=HTMLResponse)
    async def root():
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "static", "index.html")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return HTMLResponse(fh.read())
        return HTMLResponse("<h1>FaithView Pro</h1><p>static/index.html not found.</p>")

    return app


# ===========================================================================
# Entry point: uvicorn
# ===========================================================================
def main():
    import argparse
    ap = argparse.ArgumentParser(description="FaithView Pro live server (offline).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default=config.WHISPER_MODEL, help="faster-whisper model id "
                    "(default 'small.en': measured ~3.65s on 10s tile on CPU, paired with "
                    "widened 8s window for safe real-time margin)")
    ap.add_argument("--chunk", type=float, default=8.0,
                    help="chunk window seconds (was 4.0; widened to 8.0 for "
                         "small.en model headroom per benchmark)")
    ap.add_argument("--overlap", type=float, default=2.0)
    ap.add_argument("--detector", default="onnx", choices=["onnx", "heuristic", "semantic"])
    ap.add_argument("--quote-threshold", type=float, default=config.QUOTE_THRESHOLD)
    ap.add_argument("--top-k", type=int, default=config.TOP_K)
    ap.add_argument("--floor", type=float, default=LIVE_CONFIDENCE_FLOOR,
                    help="live confidence floor to broadcast detections (NOT batch 0.80; "
                         "live speech runs lower, so ~0.45 surfaces real <0.80 candidates)")
    ap.add_argument("--default-translation", default="NKJV")
    ap.add_argument("--audio", default="mic", choices=["mic", "file", "none"],
                    help="'mic' live capture; 'file' replays a sermon (dev/headless); "
                         "'none' serves UI/REST only (no transcription)")
    ap.add_argument("--file", default=None, help="audio file path when --audio file")
    ap.add_argument("--initial-prompt", default=None,
                    help="Whisper initial_prompt to bias decoding toward scripture "
                         "vocabulary; '' disables the config default")
    ap.add_argument("--condition-previous-text", action="store_true",
                    help="opt back IN to whisper's condition_on_previous_text "
                         "(default OFF for live -- avoids '...' continuation "
                         "hallucinations across rolling chunks)")
    ap.add_argument("--no-preprocess", action="store_true",
                    help="disable mic high-pass cleanup (a/b vs file path)")
    args = ap.parse_args()

    pipeline_kwargs = dict(
        whisper_model=args.model,
        chunk_seconds=args.chunk,
        overlap_seconds=args.overlap,
        quote_detector_kind=args.detector,
        quote_threshold=args.quote_threshold,
        top_k=args.top_k,
        live_confidence_floor=args.floor,
        default_translation=args.default_translation,
        offline=config.OFFLINE,
        audio_source_kind=args.audio,
        audio_file=args.file,
        whisper_initial_prompt=args.initial_prompt,
        condition_on_previous_text=bool(args.condition_previous_text),
        preprocess=not args.no_preprocess,
    )
    app = create_app(**pipeline_kwargs)

    print(f"[server] FaithView Pro live console at http://{args.host}:{args.port}")
    print(f"[server] audio={args.audio} model={args.model} detector={args.detector} "
          f"floor={args.floor}  (batch REVIEW_THRESHOLD=0.60; live broadcasts >= {args.floor})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()