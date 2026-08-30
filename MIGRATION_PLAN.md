# FaithView Pro — Desktop Migration Plan

## Current Architecture

**Backend**: Python 3.11+ / FastAPI / Uvicorn / WebSocket
- `server.py` — FastAPI app, REST endpoints, WS broadcast, static file serving
- `live_pipeline.py` — LivePipeline orchestrator (transcribe → detect → retrieve → rerank → score)
- `live_transcribe.py` — MicSource, FileSource, WhisperTranscriptionSource
- `deepgram_transcribe.py` — DeepgramSource (optional online transcription)
- `config.py` — Central configuration (model paths, thresholds, Settings dataclass)
- `bible_db.py` — In-memory Bible DB (7 translations), singleton
- `models.py` — Embedder (BGE-small-en-v1.5), singleton
- `retrieve.py` — FAISS Retriever, loads from disk
- `quote_detect.py` — Heuristic + ONNX + Hybrid quote detectors
- `score.py` — Confidence banding
- `rerank.py` — Semantic + lexical re-ranking
- `buffer.py` — Sentence windowing
- `intent_router.py` — Explicit ref / nav / clear / cue routing
- `context.py` — Sermon context tracker
- `transcription_source.py` — Abstract TranscriptionSource interface
- `transcribe.py` — Whisper model loader, Segment/Word dataclasses

**Frontend**: Vanilla HTML/CSS/JS (no framework)
- `static/index.html` — Operator console (1465 lines, inline CSS+JS)
- `static/output.html` — Live projector display
- `static/settings.html` — Settings + embedded theme manager
- `static/faithview_theme_manager_mockup.html` — Standalone theme manager

**Models/Data** (not in git, must be generated/downloaded):
- `onnx_model/model.onnx` — Trained DistilBERT quote classifier
- `index/verses.faiss` — FAISS index (~62k vectors x 384 dims)
- `index/verses_meta.json` — Verse metadata
- `bible_all_versions.json` — 7-translation Bible (~34MB)
- `nkjv.json`, `amplified.json` — Individual translation files

## Dependency Packaging Risks

| Library | Risk | Mitigation |
|---------|------|------------|
| `faster-whisper` (ctranslate2) | C++ native extension; needs matching platform wheel | Pre-built wheels exist for Win/Mac x64; ARM64 Mac needs ctranslate2 ARM build |
| `sentence-transformers` | Pulls PyTorch transformer dependencies | Use `sentence-transformers` without PyTorch extras if possible; or accept ~200MB |
| `faiss-cpu` | CPU-only FAISS; platform-specific builds | Pre-built wheels available for Win/Mac x64; ARM64 needs conda or source |
| `onnxruntime` | Platform-specific ONNX Runtime | Pre-built wheels for all platforms |
| `sounddevice` (PortAudio) | Needs PortAudio library | Bundled `.so` on Linux; Windows/Mac need DLL/dylib |
| `scipy` | Signal processing for audio resampling | Pre-built wheels available |
| `Pillow` | Image processing for NDI output | Pre-built wheels available |

**Strategy**: Use PyInstaller with hidden imports and data collection hooks. Accept ~200-400MB bundled size for ML dependencies.

## Path Assumptions to Fix

Current code uses `os.path.dirname(os.path.abspath(__file__))` in:
- `config.py` — HERE, paths to models/index/bible
- `bible_db.py` — HERE, ALL_VERSIONS_PATH
- `live_transcribe.py` — bundled libportaudio.so.2 path
- `server.py` — static file serving path

**Fix**: Create `paths.py` that resolves paths for both dev and PyInstaller-bundled modes.

## WebSocket Protocol

All WS messages are JSON with a `type` field:

```
transcript_update  : {type, text, start_time, end_time}
detection          : {type, reference, translation, text, confidence, confidence_percent, confidence_band, transcript_snippet, timestamp}
display_update     : {type, reference, translation, text} | {type, clear: true, theme?}
queue_update       : {type, queue: [...]}
source_update      : {type, source}
mode_update        : {type, mode}
session_update     : {type, translation, book, chapter, verse}
```

REST endpoints:
```
POST /queue/add       {reference, translation?, text?, confidence?, confidence_band?}
POST /present         {reference, translation?, text?}
POST /present-theme   {theme: {...}}
DELETE /queue/{id}
POST /queue/clear
POST /display/clear
POST /detections/clear
GET  /queue
GET  /health
GET  /search?q=...&top_k=...
GET  /bible/{translation}/{book}/{chapter}
POST /transcription-source  {source: "whisper"|"deepgram"}
POST /mode                  {mode: "autopilot"|"semi_autopilot"|"manual"}
```

## Implementation Phases

### Phase 1 — Project Structure + Tauri Shell ✓
- Created `desktop/` directory with Tauri 2 config
- Set up `package.json` with Tauri CLI
- Created `tauri.conf.json` with window config (main + output)
- Set up `src-tauri/` with Rust source (lib.rs, main.rs)
- Created capabilities for window, shell, dialog, fs, event, process

### Phase 2 — Python Sidecar + paths.py ✓
- Created `backend/paths.py` — cross-platform path resolver
- Created `backend/sidecar.py` — FastAPI sidecar with dynamic port + health check
- Created `backend/faithview_sidecar.spec` — PyInstaller spec
- Created `scripts/build_backend_windows.bat` and `scripts/build_backend_macos.sh`
- Sidecar announces port on stdout (`PORT:<number>`) for Tauri to read
- Health endpoint returns model status, audio state, transcription state

### Phase 3 — Frontend Backend Connection ✓
- Created `frontend/js/api.js` — HTTP/WS client (FaithViewAPI)
- Created `frontend/js/state.js` — centralized state management (FaithViewState)
- Created `frontend/js/display.js` — output window management (DisplayManager)
- Created `frontend/index.html` — startup screen with progress indicators
- Tauri integration: reads port from sidecar, invokes window commands
