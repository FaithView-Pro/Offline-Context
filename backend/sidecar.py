"""FaithView Pro Python Sidecar — FastAPI backend entry point.

Designed to be launched by Tauri as a sidecar process. Handles:
  - Dynamic port allocation (finds an available port on 127.0.0.1)
  - Health check endpoint for Tauri to poll
  - Graceful shutdown on SIGTERM/SIGINT
  - JSON status reporting
  - Logging to platform-appropriate directory

Usage:
    python sidecar.py [--port 0] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
import threading
from pathlib import Path
from typing import Optional

# Add the parent directory to sys.path so we can import backend modules
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# IMPORTANT: import web stack BEFORE runtime modules (see server.py note)
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from paths import get_paths


# ---- Logging setup ----

def setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"faithview_{time.strftime('%Y%m%d')}.log"

    logger = logging.getLogger("faithview")
    logger.setLevel(logging.INFO)

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(fh)

    # Console handler (stderr, not stdout — stdout is reserved for port announcement)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


# ---- Find available port ----

def find_available_port(host: str = "127.0.0.1") -> int:
    """Find an available TCP port on the given host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


# ---- Health status ----

class SidecarStatus:
    def __init__(self):
        self.state = "starting"  # starting | loading_models | ready | error
        self.models_loaded = False
        self.quote_detector = "missing"
        self.embedding = "missing"
        self.faiss = "missing"
        self.bible_db = "missing"
        self.audio_device = None
        self.audio_state = "idle"
        self.transcription_engine = "whisper"
        self.transcription_running = False
        self.error = None

    def to_dict(self) -> dict:
        return {
            "status": self.state,
            "backend": self.state,
            "models_loaded": self.models_loaded,
            "models": {
                "quote_detector": self.quote_detector,
                "embedding": self.embedding,
                "faiss": self.faiss,
                "bible_db": self.bible_db,
            },
            "audio": {
                "device": self.audio_device,
                "state": self.audio_state,
            },
            "transcription": {
                "engine": self.transcription_engine,
                "running": self.transcription_running,
            },
            "error": self.error,
        }


STATUS = SidecarStatus()


# ---- Create FastAPI app ----

def create_sidecar_app() -> FastAPI:
    app = FastAPI(title="FaithView Pro Sidecar")

    @app.get("/health")
    async def health():
        return STATUS.to_dict()

    @app.get("/status")
    async def status():
        return STATUS.to_dict()

    @app.get("/ready")
    async def ready():
        """Simple readiness check — returns 200 when backend is ready."""
        if STATUS.state == "ready":
            return {"ready": True}
        return JSONResponse({"ready": False, "state": STATUS.state}, status_code=503)

    return app


# ---- Bootstrap pipeline in background ----

def bootstrap_pipeline(app: FastAPI, logger: logging.Logger, args):
    """Load models and resources in a background thread."""
    paths = get_paths()

    def _do_bootstrap():
        try:
            STATUS.state = "loading_models"
            logger.info("Starting FaithView Pro backend...")
            logger.info(f"Resource root: {paths.resource_root}")
            logger.info(f"User data: {paths.user_data_dir}")

            # Load Bible database
            try:
                logger.info("Loading Bible database...")
                import bible_db
                db = bible_db.get_bible_db(str(paths.bible_all_versions))
                STATUS.bible_db = "ready"
                logger.info(f"Bible DB loaded: {len(db.versions)} versions")
            except Exception as exc:
                logger.error(f"Bible DB failed: {exc}")
                STATUS.bible_db = f"error: {exc}"

            # Load FAISS index + embedder
            try:
                logger.info("Loading embedding model and FAISS index...")
                import config
                # Override config paths
                config.FAISS_INDEX_PATH = str(paths.faiss_index)
                config.META_PATH = str(paths.faiss_meta)

                from models import get_embedder
                from retrieve import Retriever

                if paths.faiss_index.exists():
                    embedder = get_embedder(offline=True)
                    retriever = Retriever.from_disk(
                        embedder,
                        index_path=str(paths.faiss_index),
                        meta_path=str(paths.faiss_meta),
                    )
                    STATUS.embedding = "ready"
                    STATUS.faiss = "ready"
                    logger.info(f"FAISS index loaded: {retriever.index.ntotal} vectors")
                else:
                    logger.warning(f"FAISS index not found at {paths.faiss_index}")
                    STATUS.faiss = "missing"
            except Exception as exc:
                logger.error(f"FAISS/embedding load failed: {exc}")
                STATUS.embedding = f"error: {exc}"
                STATUS.faiss = f"error: {exc}"

            # Load quote detector
            try:
                logger.info("Loading quote detector...")
                import quote_detect
                import config as cfg
                cfg.ONNX_MODEL_PATH = str(paths.onnx_model)
                cfg.ONNX_TOKENIZER_PATH = str(paths.onnx_tokenizer)
                detector = quote_detect.get_detector("hybrid")
                STATUS.quote_detector = "ready"
                STATUS.models_loaded = True
                logger.info("Quote detector loaded (hybrid mode)")
            except Exception as exc:
                logger.error(f"Quote detector load failed: {exc}")
                STATUS.quote_detector = f"error: {exc}"

            STATUS.state = "ready"
            logger.info("FaithView Pro backend is READY")

        except Exception as exc:
            STATUS.state = "error"
            STATUS.error = str(exc)
            logger.error(f"Bootstrap failed: {exc}", exc_info=True)

    t = threading.Thread(target=_do_bootstrap, daemon=True, name="fv-bootstrap")
    t.start()


# ---- Main entry point ----

def main():
    paths = get_paths()
    paths.ensure_dirs()

    logger = setup_logging(paths.log_dir)

    ap = argparse.ArgumentParser(description="FaithView Pro Python Sidecar")
    ap.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=0, help="Port (0 = auto-select)")
    ap.add_argument("--model", default="small.en", help="Whisper model")
    ap.add_argument("--detector", default="hybrid", choices=["onnx", "heuristic", "hybrid", "semantic"])
    ap.add_argument("--floor", type=float, default=0.45, help="Live confidence floor")
    ap.add_argument("--audio", default="mic", choices=["mic", "file", "none"])
    ap.add_argument("--file", default=None, help="Audio file for file mode")
    ap.add_argument("--source", default="whisper", choices=["whisper", "deepgram"])
    ap.add_argument("--mode", default="semi_autopilot", choices=["autopilot", "semi_autopilot", "manual"])
    args = ap.parse_args()

    # Find port
    port = args.port if args.port > 0 else find_available_port(args.host)

    # Create the main server app
    # Import and create the full FastAPI server
    try:
        from server import create_app
        pipeline_kwargs = dict(
            whisper_model=args.model,
            quote_detector_kind=args.detector,
            live_confidence_floor=args.floor,
            audio_source_kind=args.audio,
            audio_file=args.file,
            transcription_source=args.source,
            mode=args.mode,
            offline=True,
        )
        app = create_app(**pipeline_kwargs)
        logger.info("Full server app created")
    except Exception as exc:
        logger.warning(f"Failed to create full server app ({exc}); using minimal sidecar")
        app = create_sidecar_app()

    # Add sidecar-specific endpoints
    @app.get("/sidecar/health")
    async def sidecar_health():
        return STATUS.to_dict()

    # Announce port on stdout (Tauri reads this to know where to connect)
    print(f"PORT:{port}", flush=True)
    logger.info(f"Starting server on {args.host}:{port}")

    # Bootstrap models in background
    bootstrap_pipeline(app, logger, args)

    # Run server
    config = uvicorn.Config(
        app,
        host=args.host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Graceful shutdown
    def _shutdown_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        server.run()
    except SystemExit:
        logger.info("Sidecar shutting down")
    except Exception as exc:
        logger.error(f"Server error: {exc}", exc_info=True)
        STATUS.state = "error"
        STATUS.error = str(exc)


if __name__ == "__main__":
    main()
