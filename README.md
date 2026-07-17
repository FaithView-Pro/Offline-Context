# FaithView Pro

Offline semantic scripture-detection engine. Takes a sermon audio file (MP3/WAV), transcribes it, detects when scripture is being quoted, and matches each quote to the exact Bible verse across two translations (Amplified Bible + NKJV) — all running locally on CPU with zero cloud calls.

## How It Works

```
sermon.mp3
    │
    ▼
[1] Whisper transcription ──→ timestamped segments
    │
    ▼
[2] Sentence buffering     ──→ rolling context windows
    │
    ▼
[3] Quote detection        ──→ "is this scripture?" (heuristic + semantic pre-check)
    │
    ▼
[4] Semantic retrieval     ──→ FAISS search across AMP + NKJV (~62k verses)
    │
    ▼
[5] Hybrid re-ranking      ──→ 5-signal blend (semantic + lexical + context + history + quote-prob)
    │
    ▼
[6] Confidence scoring     ──→ autopilot / review / ignored bands
    │
    ▼
results.json
```

### Models Used

| Model | Purpose | Size |
|-------|---------|------|
| Whisper `small` (faster-whisper, int8, CPU) | Speech-to-text transcription | ~500 MB |
| `BAAI/bge-small-en-v1.5` (384-dim) | Semantic verse embeddings + FAISS search | ~130 MB |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Optional re-rank boost (opt-in) | ~90 MB |

All models run fully offline once cached. No network calls at runtime.

### Output Format

```json
{
  "start_time": 812.4,
  "end_time": 818.9,
  "transcript": "I will put my spirit within you...",
  "quote_probability": 0.97,
  "selected_reference": "Ezekiel 36:27",
  "translation": "NKJV",
  "confidence": 0.94,
  "confidence_band": "review queue",
  "candidates": [
    {"reference": "Ezekiel 36:27", "translation": "NKJV", "score": 0.94},
    {"reference": "Ezekiel 36:27", "translation": "AMP", "score": 0.90}
  ]
}
```

**Confidence bands:** `autopilot-eligible` (>=0.96), `review queue` (0.80–0.95), `ignored` (<0.80).

## Install

```bash
# Python 3.10–3.13 required
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# ffmpeg must be on PATH (faster-whisper uses it for audio decoding)
# Ubuntu/Debian: sudo apt install ffmpeg
# macOS: brew install ffmpeg
```

## Setup (one-time)

### 1. Download models

```bash
python download_models.py                  # embedder + whisper small
python download_models.py --cross-encoder  # optional: also fetch cross-encoder
```

Requires internet. Models cache to `~/.cache/huggingface`. After this, everything runs fully offline.

### 2. Build the FAISS index

```bash
python build_index.py --online    # first run: allows embedder download if needed
# subsequent rebuilds:
python build_index.py             # offline (models already cached)
```

Writes `index/verses.faiss` (~92 MB) and `index/verses_meta.json` (~17 MB).

## Usage

### Detect verses in a sermon

```bash
python run.py --audio sermon.mp3 --out results.json
python run.py --audio sermon.mp3 --out results.json --cross-encoder     # with cross-encoder
python run.py --audio sermon.mp3 --out results.json --include-ignored   # show low-confidence too
python run.py --audio sermon.mp3 --out results.json --whisper-model tiny  # faster, less accurate
```

### Skip transcription (use a pre-existing transcript)

```bash
python run.py --transcript transcript.json --out results.json
```

Transcript format: `[{"text": "...", "start_time": 0.0, "end_time": 5.0}, ...]`

### Interactive verse search

```bash
python search.py "I will put my spirit within you"
python search.py "love your enemies" --top-k 5 --translation NKJV
python search.py                       # interactive mode
```

### Run tests (no models needed)

```bash
python test_pipeline.py        # unit-tests stages 2–6 with a stub embedder
```

### Benchmark

```bash
python benchmark.py                     # text search + audio pipeline
python benchmark.py --audio BIM.mp3     # audio pipeline only
python benchmark.py --text-only         # text search only
```

## CLI Reference

### `run.py` — Full pipeline

| Flag | Default | Description |
|------|---------|-------------|
| `--audio` | — | Path to sermon audio (wav/mp3) |
| `--transcript` | — | Pre-existing transcript JSON (skips Whisper) |
| `--out` | `results.json` | Output JSON path |
| `--whisper-model` | `small` | Whisper model (`tiny`, `base`, `small`, `medium`, `small.en`) |
| `--language` | `en` | Transcription language |
| `--quote-threshold` | `0.70` | Minimum quote-probability to proceed |
| `--top-k` | `10` | FAISS top-K candidates |
| `--cross-encoder` | off | Enable cross-encoder re-rank |
| `--include-ignored` | off | Emit `<0.80` confidence entries |
| `--quote-detector` | `heuristic` | Detector: `heuristic`, `semantic`, or `onnx` |

### `search.py` — Verse search

| Flag | Default | Description |
|------|---------|-------------|
| `--top-k` | `10` | Number of results |
| `--translation` | — | Filter to `AMP` or `NKJV` |
| `--no-rerank` | off | FAISS-only results (skip hybrid re-rank) |

## Project Structure

```
FaithViewPro/
├── run.py              # Main pipeline orchestrator
├── search.py           # Interactive verse search CLI
├── test_pipeline.py    # Unit tests (no models needed)
├── benchmark.py        # Performance profiling
├── build_index.py      # One-time FAISS index builder
├── download_models.py  # One-time model downloader
├── config.py           # All tunable parameters
│
├── transcribe.py       # Stage 1: Whisper transcription
├── buffer.py           # Stage 2: Sentence buffering
├── quote_detect.py     # Stage 3: Quote detection (heuristic + semantic)
├── retrieve.py         # Stage 4: FAISS semantic retrieval
├── rerank.py           # Stage 5: Hybrid re-ranking
├── score.py            # Stage 6: Confidence scoring
│
├── corpus.py           # Bible corpus loader (AMP + NKJV)
├── models.py           # Offline-aware model singletons
├── context.py          # Sermon context tracker
├── ffprobe_utils.py    # Audio metadata utility
│
├── amplified.json      # Amplified Bible corpus (~6200 verses)
├── nkjv.json           # NKJV corpus (~6200 verses)
├── index/              # FAISS index + metadata (generated)
├── requirements.txt    # Python dependencies
├── README.md           # This file
└── ARCHITECTURE.md     # Detailed architecture + benchmarks
```

## Performance

Measured on CPU with Whisper `small` (int8), bge-small-en-v1.5, and a ~62k-verse FAISS index:

| Metric | Value |
|--------|-------|
| Text search throughput | **31 queries/sec** (~32 ms per query) |
| Audio processing ratio | **0.24× realtime** (4.2s audio / 1s processing) |
| ~45-min sermon | **~10.7 minutes** end-to-end |
| RAM usage | **~240 MB** at runtime |
| FAISS index | **92 MB** (62k vectors × 384 dims) |

Transcription dominates (~98% of runtime). Use `--whisper-model tiny` or `small.en` for faster transcription.

See `ARCHITECTURE.md` for detailed breakdowns.

## Known Limitations

- Quote detection uses heuristics (cue phrases, vocabulary patterns) — a verbatim quote in modern English with no cue phrase may be missed. An ONNX classifier slot is ready for a trained model.
- Sermon context tracking is a simple frequency counter, not a trained sequence model.
- Corpus is fixed to AMP + NKJV. No other translations.

## License

MIT
