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
import search as search_mod  # noqa: E402


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
        self.audio_device: Optional[int] = None  # audio device ID (None = default)
        self.source = None
        self.audio_thread: Optional[threading.Thread] = None
        self.audio_stop = threading.Event()
        self.started = False
        self.lock = threading.Lock()        # guards queue mutations
        self.transcription_source = "whisper"  # "whisper" | "deepgram"
        self.mode = "semi_autopilot"           # "autopilot" | "semi_autopilot"
        self.display: Optional[dict] = None   # last presented display/theme

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
            on_display=_on_display,
            transcription_source_type=self.transcription_source,
            mode=self.mode,
            **kwargs,
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


class SourceReq(BaseModel):
    source: str  # "whisper" | "deepgram"


class ModeReq(BaseModel):
    mode: str  # "autopilot" | "semi_autopilot" | "manual"


class ThemeReq(BaseModel):
    theme: dict


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


def _restart_audio_capture(source_type: str):
    """Stop current transcription, create a new MicSource, start new source."""
    # Stop the old MicSource FIRST to free the ALSA device
    if STATE.source is not None:
        try:
            STATE.source.stop()
        except Exception:
            pass
        STATE.source = None

    # Stop the current pipeline transcription
    if STATE.pipeline is not None:
        try:
            STATE.pipeline.stop()
        except Exception as exc:
            print(f"[server] stop pipeline error: {exc}")

    time.sleep(0.3)  # let ALSA fully release the device

    # If we were in file mode, don't restart with mic
    if STATE.audio_source_kind == "file":
        return

    # For "none" mode, just don't start audio
    if STATE.audio_source_kind == "none":
        STATE.started = False
        return

    # Create a fresh MicSource and wire it into the pipeline with the new source
    _ensure_portaudio()
    try:
        src = MicSource()
        STATE.source = src
        if STATE.pipeline is not None:
            STATE.pipeline.set_transcription_source(source_type, src)
        STATE.started = True
    except Exception as exc:
        print(f"[server] restart audio capture failed: {exc}")
        STATE.started = False


# ===========================================================================
# FastAPI app -- NOTE no CORSMiddleware: it is unnecessary (frontend is served
# from this same origin) and was observed to 403-reject the WS handshake.
# ===========================================================================
def create_app(**pipeline_kwargs) -> "FastAPI":
    # audio_source_* are server-level controls, NOT LivePipeline kwargs.
    STATE.audio_source_kind = pipeline_kwargs.pop("audio_source_kind", "mic")
    STATE.audio_file = pipeline_kwargs.pop("audio_file", None)
    STATE.transcription_source = pipeline_kwargs.pop("transcription_source", STATE.transcription_source)
    STATE.mode = pipeline_kwargs.pop("mode", STATE.mode)

    app = FastAPI(title="FaithView Pro live operator console")

    # Mount frontend JS/CSS assets for static serving
    here_static = os.path.dirname(os.path.abspath(__file__))
    frontend_dir = os.path.join(here_static, "frontend")
    legacy_static = os.path.join(here_static, "static")

    from fastapi.staticfiles import StaticFiles
    if os.path.isdir(frontend_dir):
        app.mount("/js", StaticFiles(directory=os.path.join(frontend_dir, "js")), name="frontend-js")
    if os.path.isdir(legacy_static):
        app.mount("/static", StaticFiles(directory=legacy_static), name="legacy-static")

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
        # real mic — only start if not already started via POST /transcription-source
        if STATE.started:
            return
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
        try:
            await ws.send_text(json.dumps({"type": "queue_update", "queue": STATE.queue}))
            await ws.send_text(json.dumps({"type": "source_update", "source": STATE.transcription_source}))
            await ws.send_text(json.dumps({"type": "mode_update", "mode": STATE.mode}))
            if STATE.display:
                await ws.send_text(json.dumps({"type": "display_update", **STATE.display}))
        except Exception:
            STATE.clients.discard(ws)
            return
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

    @app.post("/present-theme")
    async def present_theme(req: ThemeReq):
        # Accept a full theme JSON from the theme manager and broadcast it
        try:
            theme = req.theme or {}
        except Exception:
            return JSONResponse({"error": "invalid theme payload"}, status_code=400)
        print(f"[server] /present-theme received theme keys: {list(theme.keys())}")
        # Store the theme in STATE.display so reconnecting clients receive it
        STATE.display = {
            "reference": theme.get("reference", "") or "",
            "translation": theme.get("translation", "") or "",
            "text": theme.get("text", "") or "",
            "theme": theme,
        }
        print(f"[server] broadcasting display_update with theme (reference={STATE.display.get('reference')})")
        await STATE.broadcast({"type": "display_update", **STATE.display})
        return {"ok": True}

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
        """Health check endpoint for Tauri sidecar polling and general readiness."""
        pipeline_ready = STATE.pipeline is not None and (
            STATE.pipeline._db is not None or STATE.pipeline._retriever is not None
        )
        return {
            "ok": True,
            "status": "ready" if pipeline_ready else "loading",
            "audio": STATE.audio_source_kind,
            "floor": STATE.pipeline.live_confidence_floor if STATE.pipeline else LIVE_CONFIDENCE_FLOOR,
            "source": STATE.transcription_source,
            "mode": STATE.mode,
            "models_loaded": pipeline_ready,
            "models": {
                "bible_db": "ready" if pipeline_ready and STATE.pipeline._db is not None else "loading",
                "quote_detector": "ready" if pipeline_ready and STATE.pipeline._detector is not None else "loading",
                "faiss": "ready" if pipeline_ready and STATE.pipeline._retriever is not None else "loading",
                "embedding": "ready" if pipeline_ready and STATE.pipeline._retriever is not None else "loading",
            },
            "audio_state": {
                "device": STATE.audio_device,
                "capturing": STATE.started,
            },
            "transcription": {
                "engine": STATE.transcription_source,
                "running": STATE.started,
            },
        }

    @app.get("/ready")
    async def ready():
        """Simple readiness probe — returns 200 when backend is fully loaded."""
        if STATE.pipeline and (STATE.pipeline._db is not None or STATE.pipeline._retriever is not None):
            return {"ready": True}
        return JSONResponse({"ready": False, "state": "loading"}, status_code=503)

    @app.get("/status")
    async def status():
        """Detailed status for the Tauri sidecar."""
        return await health()

    @app.get("/audio/devices")
    async def audio_devices():
        """List available audio input devices."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            inputs = []
            for i, d in enumerate(devices):
                if d.get('max_input_channels', 0) > 0:
                    inputs.append({
                        "id": i,
                        "name": d['name'],
                        "channels": d.get('max_input_channels', 0),
                        "sample_rate": d.get('default_samplerate', 44100),
                    })
            return {"devices": inputs, "default": None}
        except Exception as exc:
            return JSONResponse({"error": f"audio device query failed: {exc}"}, status_code=500)

    # ---- Transcription source switching (Task 1) --------------------------
    @app.post("/transcription-source")
    async def set_transcription_source(req: SourceReq):
        if req.source not in ("whisper", "deepgram"):
            return JSONResponse({"error": "source must be 'whisper' or 'deepgram'"}, status_code=400)
        if req.source == "deepgram" and not os.environ.get("DEEPGRAM_API_KEY"):
            return JSONResponse({"error": "DEEPGRAM_API_KEY not set"}, status_code=400)
        if STATE.pipeline is None:
            return JSONResponse({"error": "pipeline not ready"}, status_code=503)

        STATE.transcription_source = req.source
        _restart_audio_capture(req.source)
        await STATE.broadcast({"type": "source_update", "source": req.source})
        return {"source": req.source}

    # ---- Operator mode switching (Task 2) ---------------------------------
    @app.post("/mode")
    async def set_mode(req: ModeReq):
        if req.mode not in ("autopilot", "semi_autopilot", "manual"):
            return JSONResponse({"error": "mode must be 'autopilot', 'semi_autopilot', or 'manual'"}, status_code=400)
        if STATE.pipeline is None:
            return JSONResponse({"error": "pipeline not ready"}, status_code=503)

        prev_mode = STATE.mode
        STATE.mode = req.mode
        STATE.pipeline.set_mode(req.mode)

        if req.mode == "manual" and prev_mode != "manual":
            # Pause transcription source
            STATE.pipeline.pause_transcription()
            # Clear transcript + detections panels client-side
            await STATE.broadcast({"type": "transcript_clear"})
            await STATE.broadcast({"type": "detections_clear"})
            STATE.started = False
        elif req.mode != "manual" and prev_mode == "manual":
            # Resume transcription
            _restart_audio_capture(STATE.transcription_source)

        await STATE.broadcast({"type": "mode_update", "mode": req.mode})
        return {"mode": req.mode}

    # ---- Vector search (Task 3) -------------------------------------------
    @app.get("/search")
    async def vector_search(q: str = "", top_k: int = 10):
        if not q.strip():
            return JSONResponse({"error": "query parameter 'q' is required"}, status_code=400)
        if STATE.pipeline is None:
            return JSONResponse({"error": "pipeline not ready"}, status_code=503)

        top_k = max(1, min(top_k, 50))
        try:
            STATE.pipeline._ensure_resources()
        except Exception as exc:
            return JSONResponse({"error": f"failed to load resources: {exc}"}, status_code=503)

        if STATE.pipeline._retriever is None:
            return JSONResponse({"error": "retriever not loaded"}, status_code=503)
        try:
            results = search_mod.search(
                q.strip(), STATE.pipeline._retriever, top_k=top_k, rerank=True,
            )
        except Exception as exc:
            return JSONResponse({"error": f"search failed: {exc}"}, status_code=500)

        items = []
        for r in results:
            if hasattr(r, "candidate"):
                c = r.candidate
                items.append({
                    "reference": c.reference,
                    "translation": c.translation,
                    "text": c.text,
                    "score": round(r.final, 4),
                    "semantic": round(r.semantic, 4),
                    "lexical": round(r.lexical, 4),
                    "context": round(r.context, 4),
                })
            else:
                items.append({
                    "reference": r.reference,
                    "translation": r.translation,
                    "text": r.text,
                    "score": round(r.score, 4),
                })
        return {"query": q.strip(), "results": items}

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

    # --- serve the frontend ---
    def _serve_frontend(relative_path: str):
        """Serve a file from static/ first, fall back to frontend/."""
        path = os.path.join(legacy_static, relative_path)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return HTMLResponse(fh.read())
        path = os.path.join(frontend_dir, relative_path)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return HTMLResponse(fh.read())
        return HTMLResponse(f"<h1>FaithView Pro</h1><p>{relative_path} not found.</p>")

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return _serve_frontend("index.html")

    @app.get("/output.html", response_class=HTMLResponse)
    async def output_page():
        return _serve_frontend("output.html")

    @app.get("/settings.html", response_class=HTMLResponse)
    async def settings_page():
        return _serve_frontend("settings.html")

    @app.get("/themes.html", response_class=HTMLResponse)
    async def themes_page():
        return _serve_frontend("faithview_theme_manager_mockup.html")

    # --- clear detections endpoint ---
    @app.post("/detections/clear")
    async def detections_clear():
        return {"ok": True}

    return app


# ===========================================================================
# Entry point: uvicorn
# ===========================================================================
def main():
    import argparse
    import socket
    ap = argparse.ArgumentParser(description="FaithView Pro live server (offline).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0,
                    help="Port to bind (0 = auto-select an available port)")
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
    ap.add_argument("--source", default="whisper", choices=["whisper", "deepgram"],
                    help="transcription source (default 'whisper')")
    ap.add_argument("--mode", default="semi_autopilot", choices=["autopilot", "semi_autopilot", "manual"],
                    help="operator mode (default 'semi_autopilot')")
    ap.add_argument("--device", type=int, default=None,
                    help="audio device ID (default: system default input)")
    args = ap.parse_args()

    STATE.audio_device = args.device

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
        transcription_source=args.source,
        mode=args.mode,
    )
    app = create_app(**pipeline_kwargs)

    # Resolve dynamic port (port 0 = auto-select)
    host = args.host
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port = s.getsockname()[1]

    print(f"[server] FaithView Pro live console at http://{host}:{port}")
    print(f"[server] audio={args.audio} model={args.model} detector={args.detector} "
          f"floor={args.floor}  (batch REVIEW_THRESHOLD=0.60; live broadcasts >= {args.floor})")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()