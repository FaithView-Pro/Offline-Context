# FaithView Pro — Application Overview

## Purpose

FaithView Pro is a real-time Bible verse detection and display system for church services. It listens to live sermon audio via microphone, transcribes speech, detects when the speaker quotes or references scripture, and instantly presents the matched verse on a projector or live output screen — all running locally with no external cloud dependency for the core pipeline.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        FaithView Pro Architecture                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────┐    ┌──────────────┐    ┌──────────────────────────┐   │
│  │ Microphone / │───▶│ Transcriber  │───▶│ Quote Detector (ONNX)   │   │
│  │ Audio File   │    │ Whisper/Deep- │    │ DistilBERT classifier   │   │
│  │              │    │ gram         │    │ gates to retrieval       │   │
│  └─────────────┘    └──────────────┘    └──────────┬───────────────┘   │
│                                                     │                   │
│                                          ┌──────────▼───────────────┐   │
│                                          │ FAISS Semantic Retrieval │   │
│                                          │ BGE-small-en embeddings  │   │
│                                          │ Top-K nearest verses     │   │
│                                          └──────────┬───────────────┘   │
│                                                     │                   │
│                                          ┌──────────▼───────────────┐   │
│                                          │ Score + Confidence Band  │   │
│                                          │ Autopilot / Review /     │   │
│                                          │ Ignored                  │   │
│                                          └──────────┬───────────────┘   │
│                                                     │                   │
│  ┌──────────────────────────────────────────────────▼───────────────┐   │
│  │                    FastAPI Server (server.py)                     │   │
│  │  ┌────────────┐  ┌──────────────┐  ┌────────────────────────┐   │   │
│  │  │ REST API   │  │ WebSocket    │  │ Static File Server     │   │   │
│  │  │ /queue     │  │ /ws          │  │ / → index.html         │   │   │
│  │  │ /present   │  │ Broadcasts   │  │ /output.html           │   │   │
│  │  │ /search    │  │ live events  │  │ /settings.html         │   │   │
│  │  │ /mode      │  │              │  │ /themes.html           │   │   │
│  │  └─────┬──────┘  └──────┬───────┘  └────────────────────────┘   │   │
│  └────────┼────────────────┼────────────────────────────────────────┘   │
│           │                │                                            │
│  ┌────────▼────────────────▼────────────────────────────────────────┐   │
│  │                      Browser UI (Vanilla JS)                     │   │
│  │  ┌──────────┐  ┌────────────┐  ┌─────────┐  ┌──────────────┐   │   │
│  │  │ Operator │  │  Output    │  │ Settings│  │ Theme Manager│   │   │
│  │  │ Console  │  │  Display   │  │  Page   │  │   (standalone│   │   │
│  │  │ index.html│ │ output.html│  │settings │  │    or embed) │   │   │
│  │  └──────────┘  └────────────┘  └─────────┘  └──────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Backend (Python)

| File | Role |
|------|------|
| `server.py` | FastAPI app — REST endpoints, WebSocket broadcast, static file serving, lifecycle management |
| `live_pipeline.py` | Core `LivePipeline` class — orchestrates transcribe → detect → retrieve → rerank → score in a background thread |
| `live_transcribe.py` | `MicSource` (live mic capture via PyAudio) and `FileSource` (offline file replay), wraps faster-whisper streaming |
| `deepgram_transcribe.py` | Alternative streaming transcriber using Deepgram's live WebSocket API (requires API key) |
| `config.py` | Central configuration — model paths, thresholds, weights, paths to Bible JSON files |
| `bible_db.py` | `BibleDB` class — loads NKJV/Amplified JSON, resolves book names/aliases, serves chapter verses |
| `search.py` | Vector search helper — wraps retriever + optional reranking for the `/search` endpoint |
| `quote_detect.py` | Quote detection — ONNX DistilBERT classifier that scores whether a transcript window contains a Bible quote |
| `score.py` | Confidence scoring and banding logic |
| `retrieve.py` | FAISS retrieval — embeds the query, finds top-K nearest verses |
| `rerank.py` | Cross-encoder reranking (optional, disabled by default) |
| `buffer.py` | Sentence buffering — merges rolling transcription chunks into complete sentences |
| `intent_router.py` | Routes detected text through the pipeline stages |
| `transcription_source.py` | Transcription source abstraction |

### Frontend (Vanilla HTML/CSS/JS — no framework)

| File | Route | Role |
|------|-------|------|
| `static/index.html` | `/` | **Operator Console** — main 4-column home page: transcript, detections, live display, queue + Bible reader + search engine |
| `static/output.html` | `/output.html` | **Live Output Display** — fullscreen projector view rendered by themes |
| `static/settings.html` | `/settings.html` | **Settings Page** — configuration + embedded theme manager |
| `static/faithview_theme_manager_mockup.html` | `/themes.html` | **Standalone Theme Manager** — full visual editor for verse/ref/background elements |

### Data Flow

1. **Audio Capture** — `MicSource` records from the default mic (or `FileSource` replays an audio file at real-time pace)
2. **Streaming Transcription** — Whisper (`small.en` model, ~3.7s per 10s chunk on CPU) or Deepgram streams transcription segments in real time
3. **Sentence Buffering** — Rolling chunks are merged into complete sentences with lookahead words
4. **Quote Detection** — ONNX DistilBERT classifier scores each sentence window; only sentences above `QUOTE_THRESHOLD` (0.40) proceed
5. **Semantic Retrieval** — The sentence is embedded with BGE-small-en, then FAISS finds the top-K (10) nearest Bible verses by vector similarity
6. **Confidence Scoring** — Semantic similarity (80%) + lexical keyword overlap (20%) produce a final score
7. **Confidence Banding** — Score is classified into: `autopilot-eligible` (≥0.60), `review queue` (0.40–0.60), or `ignored` (<0.40)
8. **WebSocket Broadcast** — Detection events, display updates, queue changes, and transcript updates are broadcast to all connected browser clients in real time
9. **Operator Action** — The operator can present a verse to the live display, add it to a queue, or dismiss it
10. **Live Output** — The output page (`output.html`) renders the presented verse using the active theme's background, fonts, and element layout

---

## Features

### Live Transcription
- Real-time speech-to-text via Whisper (offline) or Deepgram (online)
- Rolling transcription display with current-line highlighting
- Configurable chunk size (8s default), overlap (2s), and VAD settings
- Whisper initial prompt primes the decoder with scripture-flavored vocabulary

### Bible Verse Detection
- ONNX DistilBERT classifier (trained, exported to ONNX) — lightweight, runs on CPU
- FAISS vector search with BGE-small-en embeddings for semantic matching
- Configurable confidence thresholds and live confidence floor (default 0.45)
- Three confidence bands: autopilot-eligible, review queue, ignored

### Operator Console (`index.html`)
- **Top Navigation** — FaithView Pro badge, mode toggle (Semi/Auto/Man), source toggle (Offline/Online), Present/Clear buttons, nav icons, dark/light mode toggle
- **Four-Column Main Grid**:
  - Column 1: Live transcript (scrolling, auto-follows current line)
  - Column 2: Recent detections (confidence-colored cards with Present/Add-to-Queue actions)
  - Column 3: Live display (current verse shown, with Clear button)
  - Column 4: Output frame preview (16:9 aspect ratio, mirrors live output) + Queue list
- **Bottom Bar**:
  - Bible reader with book autocomplete, version selector, and verse display
  - Search Engine panel for manual verse lookup
  - Preview panel with Present button
- **Interactions**:
  - Single-click detection = preview in Bible reader
  - Double-click detection = present directly to live display
  - Queue items can be presented or deleted individually

### Mode System
- **Autopilot** — verses are automatically presented to the live display when confidence exceeds the threshold
- **Semi-Autopilot** (default) — detections appear in the queue for operator review before presenting
- **Manual** — transcription pauses; operator manually enters verses via Bible reader

### Theme System
Two theme manager implementations share the same localStorage key (`faithview-themes`):

**Standalone Theme Manager** (`/themes.html`):
- Full visual editor with draggable verse, reference, logo, and copyright elements
- Background color/image/video support with filters
- Font family, size, weight, letter-spacing, line-height controls
- Saved themes appear in a clickable list — click to load into canvas
- "Use Theme" sends the theme live via `POST /present-theme`
- Export/import theme JSON files

**Settings Embedded Theme Manager** (`/settings.html`):
- Same capabilities in an embedded layout
- Theme list with thumbnails and time-ago labels
- New/delete/rename/switch themes
- Save button pushes theme to live output immediately

**Theme Format** — Both managers save to localStorage as structured JSON with `canvas` (bgType, bgColor, bgImage, bgFit) and `elements` array (type, text, position, font, color, size, weight, etc.). The `/present-theme` endpoint broadcasts the full theme to all clients. `output.html` renders the theme using `renderTheme()` which handles both the structured format (settings) and raw inline-style format (standalone).

### Bible Reader
- Full NKJV Bible database loaded from `nkjv.json` (or Amplified from `amplified.json`)
- Book name autocomplete with abbreviations (Gen→Genesis, Rom→Romans, etc.)
- Chapter and verse navigation
- Click any verse to present it to the live display

### Vector Search Engine
- `GET /search?q=<query>&top_k=<n>` — semantic search across all Bible verses
- Uses the same FAISS index and BGE embeddings as the detection pipeline
- Results include score, semantic similarity, lexical overlap, and context score
- Integrated into the bottom bar of the operator console

### Live Output Display (`output.html`)
- Fullscreen-capable projector view
- Renders active theme with background, verse text, reference, and other elements
- Receives display updates via WebSocket
- Handles both theme formats (structured and inline styles)
- Semi-transparent dark overlay for readability

---

## Configuration

All tunables are in `config.py` and can be overridden via CLI arguments or environment variables:

| Setting | Default | Description |
|---------|---------|-------------|
| `WHISPER_MODEL` | `small.en` | faster-whisper model ID |
| `WHISPER_BEAM_SIZE` | `1` | Beam search width (1 = greedy) |
| `WHISPER_VAD_FILTER` | `True` | Voice activity detection |
| `QUOTE_THRESHOLD` | `0.40` | Minimum score to proceed to retrieval |
| `TOP_K` | `10` | Number of nearest verses to retrieve |
| `AUTOPILOT_THRESHOLD` | `0.60` | Score ≥ this = autopilot-eligible |
| `REVIEW_THRESHOLD` | `0.40` | Score ≥ this = review queue |
| `LIVE_CONFIDENCE_FLOOR` | `0.45` | Live broadcast floor (lower than batch to surface more candidates) |
| `OFFLINE` | `True` | Force local-only model loading |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Sentence embedding model |
| `CROSS_ENCODER_MODEL` | `ms-marco-MiniLM-L-6-v2` | Reranking model (opt-in) |

---

## How to Run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Download models (first time only)
python download_models.py

# 3. Build FAISS index (first time only)
python build_index.py

# 4. Start the server
python server.py --host 127.0.0.1 --port 8000

# 5. Open in browser
#    Operator Console: http://127.0.0.1:8000
#    Live Output:      http://127.0.0.1:8000/output.html
#    Settings:         http://127.0.0.1:8000/settings.html
#    Theme Manager:    http://127.0.0.1:8000/themes.html
```

**CLI options:**
```
--audio mic|file|none     Audio source (default: mic)
--file <path>             Audio file path (when --audio file)
--source whisper|deepgram Transcription engine
--mode autopilot|semi_autopilot|manual  Operator mode
--model <whisper-model>   Whisper model ID
--detector onnx|heuristic|semantic  Quote detector
--floor <float>           Live confidence floor
```

---

## File Structure

```
FaithViewPro/
├── server.py                    # FastAPI server (REST + WebSocket + static)
├── live_pipeline.py             # Core pipeline orchestration
├── live_transcribe.py           # Mic/file audio capture + Whisper streaming
├── deepgram_transcribe.py       # Deepgram streaming transcriber
├── config.py                    # Central configuration
├── bible_db.py                  # Bible database (NKJV, Amplified)
├── search.py                    # Vector search helper
├── quote_detect.py              # ONNX quote detection
├── score.py                     # Confidence scoring
├── retrieve.py                  # FAISS retrieval
├── rerank.py                    # Cross-encoder reranking
├── buffer.py                    # Sentence buffering
├── intent_router.py             # Pipeline routing
├── transcribe.py                # Transcription segment model
├── transcription_source.py      # Source abstraction
├── requirements.txt             # Python dependencies
├── .env                         # Environment variables (DEEPGRAM_API_KEY)
├── nkjv.json                    # NKJV Bible text
├── amplified.json               # Amplified Bible text
├── bible_all_versions.json      # Multi-version Bible data
├── onnx_model/                  # Trained DistilBERT ONNX model
│   └── model.onnx
├── index/                       # FAISS index + metadata
│   ├── verses.faiss
│   └── verses_meta.json
├── static/                      # Frontend files
│   ├── index.html               # Operator Console (home page)
│   ├── output.html              # Live Output Display
│   ├── settings.html            # Settings + embedded theme manager
│   └── faithview_theme_manager_mockup.html  # Standalone theme manager
├── pewbeam/                     # PewBeam integration (streaming output)
├── quote_classifier_model/      # Training artifacts
├── datasets/                    # Training data
├── train_quote_classifier.py    # Model training script
├── export_to_onnx.py            # ONNX export script
├── download_models.py           # Model download script
├── build_index.py               # FAISS index builder
├── overview.md                  # This file
└── README.md                    # Project README
```

---

## Technology Stack

- **Backend:** Python 3.11+, FastAPI, uvicorn, Pydantic
- **ML/Transcription:** faster-whisper (Whisper), ONNX Runtime, sentence-transformers (BGE-small-en), FAISS
- **Frontend:** Vanilla HTML/CSS/JS (no framework, no build step)
- **Real-time:** WebSocket for live event broadcast
- **Audio:** PyAudio (mic capture), ffmpeg (file processing)
- **Bible Data:** JSON (NKJV, Amplified versions)
- **Optional:** Deepgram API (online transcription), cross-encoder reranking
