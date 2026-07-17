# FaithView Pro — Architecture Document

## Overview

FaithView Pro is a fully offline command-line pipeline that takes a pre-recorded sermon audio file (WAV/MP3) and produces a timestamped list of scripture citations detected in the speaker's words, matched against a dual-translation Bible corpus (Amplified Bible + NKJV). No cloud calls, no live mic, no UI — it proves the retrieval pipeline before integration into a real-time operator console.

---

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      INPUT                               │
│                 sermon.wav / sermon.mp3                  │
└─────────────────────┬────────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 1 — TRANSCRIPTION  (transcribe.py)               │
│  faster-whisper (CTranslate2, int8, CPU)                │
│  Audio → timestamped segments [{text, start, end}, ...] │
└─────────────────────┬────────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 2 — SENTENCE BUFFERING  (buffer.py)              │
│  Segments → rolling windows: prev + current + next N    │
│  words. Candidate = current sentence; context = window.  │
│  [{text, candidate_text, start_time, end_time}, ...]     │
└─────────────────────┬────────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 3 — QUOTE DETECTION  (quote_detect.py)           │
│  Local classifier scores "is this Scripture?".           │
│  Only windows ≥ QUOTE_THRESHOLD (0.70) proceed.         │
│  Three detector types behind a swappable interface:      │
│    • HeuristicQuoteDetector (rules-based, default)       │
│    • SemanticQuoteDetector (heuristic + FAISS boost)     │
│    • OnnxQuoteDetector (trained ML, slot only)           │
└─────────────────────┬────────────────────────────────────┘
                      │ (filtered windows)
                      ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 4 — SEMANTIC RETRIEVAL  (retrieve.py)            │
│  bge-small-en-v1.5 (384-dim) embeddings + FAISS         │
│  IndexFlatIP over L2-normalized vectors = exact cosine.  │
│  Searches AMP + NKJV combined → top-K candidates.        │
│  Index: ~62k verses × 384 dims ≈ 92 MB.                │
└─────────────────────┬────────────────────────────────────┘
                      │ (per-window top-K candidate lists)
                      ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 5 — HYBRID RE-RANKING  (rerank.py)               │
│  Weighted blend of 5 signals:                            │
│    45% semantic similarity (FAISS cosine)                │
│    20% keyword/lexical overlap                           │
│    15% sermon context match (book/keyword tracker)       │
│    10% history (previously accepted verses)              │
│    10% quote-detection probability                       │
│  Optional: cross-encoder re-scores top 10 at 30% blend.  │
│  Selects top candidate as representative per window.     │
└─────────────────────┬────────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 6 — CONFIDENCE SCORING  (score.py)               │
│  Pure local decision engine. Confidence bands:           │
│    ≥ 0.96 → "autopilot-eligible" (no human needed)       │
│    ≥ 0.80 → "review queue" (human review suggested)      │
│    < 0.80 → "ignored" (not surfaced by default)          │
└─────────────────────┬────────────────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────────────────┐
│                     OUTPUT                               │
│              results.json + console table                │
└──────────────────────────────────────────────────────────┘
```

---

## Module-by-Module Breakdown

### `config.py` — Central Configuration

All tunable parameters live here so every stage can be unit-tested and swapped independently:

| Category | Key | Default | Purpose |
|----------|-----|---------|---------|
| Paths | `AMP_PATH`, `NKJV_PATH` | `amplified.json`, `nkjv.json` | Bible corpus JSON files |
| Paths | `INDEX_DIR` | `index/` | FAISS index + metadata location |
| Models | `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model (384-dim) |
| Models | `WHISPER_MODEL` | `small` | faster-whisper model ID |
| Models | `CROSS_ENCODER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Optional re-ranker |
| Stage 3 | `QUOTE_THRESHOLD` | `0.70` | Minimum quote probability |
| Stage 4 | `TOP_K` | `10` | FAISS top-K per query |
| Stage 5 | `RerankWeights` | `(0.45, 0.20, 0.15, 0.10, 0.10)` | Hybrid blend weights |
| Stage 6 | `AUTOPILOT_THRESHOLD` | `0.96` | Autopilot confidence floor |
| Stage 6 | `REVIEW_THRESHOLD` | `0.80` | Review-queue confidence floor |

A `Settings` dataclass overrides globals at runtime via CLI arguments.

---

### `corpus.py` — Bible Corpus Loader

Loads the two-translation corpus from JSON files on disk.

**Schema** (inspected from data, not assumed):
```json
{"version": "AMPLIFIED",
 "books": [{"book": "Genesis",
            "chapters": [{"chapter": 1,
                          "verses": [{"verse": 1, "text": "..."}]}]}]}
```

**Key data types:**
- `Verse` — frozen dataclass: `(translation, book, chapter, verse, text)` with `reference` (e.g. `"Ezekiel 36:27"`) and `key` (translation-agnostic reference) properties
- `load_corpus(amp_path, nkjv_path)` → `list[Verse]` — loads both translations (~62k total verses)
- Normalizes translation codes: `"AMPLIFIED"` → `"AMP"`, `"NKJV"` → `"NKJV"`

---

### `models.py` — Offline-Aware Model Loaders

Singletons (thread-safe, loaded once per process) for the two ML models:

| Loader | Model | Dim | Purpose |
|--------|-------|-----|---------|
| `Embedder` | `bge-small-en-v1.5` via sentence-transformers | 384 | Encodes corpus verses and query windows |
| `get_cross_encoder()` | `ms-marco-MiniLM-L-6-v2` | — | Optional cross-encoder re-rank |

**Offline guarantee:** `_apply_offline_env()` sets `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1` so zero network calls escape at runtime.

**bge convention:** The query prefix `"Represent this sentence for searching relevant passages: "` is prepended to *queries only* (never to corpus verses), matching HuggingFace's recommended usage.

---

### `build_index.py` — One-Time FAISS Index Build

```
python build_index.py                 # offline (models already cached)
python build_index.py --online        # first run: allows embedder download
python build_index.py --limit 2000    # smoke test on subset
```

**Workflow:**
1. Load both translations via `corpus.load_corpus()`
2. Get the `Embedder` singleton
3. Embed all verses in batches → L2-normalized vectors
4. Build `faiss.IndexFlatIP` (exact cosine via inner product)
5. Write `index/verses.faiss` (~92 MB) and `index/verses_meta.json` (~17 MB)

**Index design choice:** `IndexFlatIP` over `IndexHNSWFlat`/`IndexIVFFlat` because ~62k × 384 dims is small enough for sub-millisecond flat search on CPU. For a much larger corpus, change one function (`build_index`) — the rest of the pipeline never sees the index type.

---

### `download_models.py` — One-Time Model Download

Run once with network ON to populate the HuggingFace cache (`~/.cache/huggingface`):

```
python download_models.py                  # embedder + whisper "small"
python download_models.py --cross-encoder  # also fetch cross-encoder
```

After this, all subsequent runs use default offline mode.

---

### `transcribe.py` — Stage 1: Transcription

**Technology:** `faster-whisper` (CTranslate2 backend, CPU `int8` compute type) — best CPU performance among Whisper bindings.

**Data types:**
- `Word(text, start_time, end_time, probability)` — optional word-level timestamps
- `Segment(text, start_time, end_time, words)` — per-segment with optional words

**Key functions:**
- `transcribe(audio_path, settings, word_timestamps)` → `list[Segment]` — runs Whisper
- `segments_from_dicts(dicts)` → `list[Segment]` — bypass Whisper for pre-existing transcripts or live caption feed

**Runtime:** ~2–4× realtime on 8-core CPU with `small` model (fastest option: `tiny` or `small.en`).

---

### `buffer.py` — Stage 2: Sentence Buffering

Groups raw Whisper segments into rolling sentence windows with context:

```
Window = previous sentence + current sentence (candidate) + next 12 words
```

**Why context matters:** The full window is used for retrieval and re-ranking (more context = better similarity), but quote detection scores *only* the candidate sentence (avoiding context-bleed false positives).

**Data types:**
- `Sentence(text, start_time, end_time, index)` — extracted via `.?!` splitting
- `Window(text, candidate_text, start_time, end_time, prev_text, next_text)`

**Sentence splitting:** When word-level timestamps are available, uses precise word boundaries. Otherwise falls back to per-segment splitting (acceptable for short Whisper segments).

---

### `quote_detect.py` — Stage 3: Quote Detection

Decides whether each sentence window is *actually Scripture being quoted*, versus story/joke/news/general teaching — before any Bible search runs.

#### Interface

```python
class QuoteDetector(ABC):
    def score(self, text: str) -> float: ...   # returns P(text is scripture) in [0, 1]
```

All three detector types implement this interface. The rest of the pipeline only calls `score()`, so detectors are swappable with zero pipeline edits.

#### `HeuristicQuoteDetector` (default, pure rules)

Combines 10 weighted signals into a calibrated probability:

| Signal | Max Weight | Description |
|--------|-----------|-------------|
| Cue phrase | 0.55 | "the Bible says", "it is written", "Paul writes", etc. (48 cues) |
| Scripture reference | 0.55 | Pattern match for "Romans 8:28", bare "8:28" |
| Archaic vocabulary | 0.45 | "thou", "hath", "saith", "behold", etc. (33 words) — strong verbatim signal |
| Divine-speech patterns | 0.60 | "I will put my Spirit...", "you shall love...", "truly I say..." (95 patterns) |
| Theological vocabulary | 0.25 | "righteousness", "covenant", "sanctification", etc. (110 words) |
| Quotation marks | 0.08 | Presence of `"..."` in transcript |
| Bible book mention | 0.06 | "Ezekiel", "Romans", "Psalms" etc. (66 book names) |
| Quoting verb bonus | 0.07 | "says", "writes", "tells us" + cue/reference/quote marker present |
| Negative markers | -0.25 | "breaking news", "knock knock", "stock market", etc. — down-weight |
| Base probability | 0.10 | Floor score for any text |

**Calibration:** Tuned via the self-test at the bottom of `quote_detect.py` with labeled examples. Known limitation: a verbatim quote in modern English with no cue phrase and no reference may score below 0.70 (documented recall gap, to be fixed by the trained ONNX classifier).

#### `SemanticQuoteDetector` (heuristic + FAISS pre-check)

Wraps the heuristic detector with a dual-translation concordance check:

1. Runs the heuristic first
2. Issues a lightweight FAISS search (k=5) on the candidate text
3. If the **same reference** appears from **both AMP and NKJV** with high similarity (≥0.70) → boosts probability significantly
4. Fallback: a very high single-match (≥0.80) with decent heuristic (≥0.25) also gets a moderate boost

This is the strongest offline signal that a sentence is a *verbatim* quote (not just topically related) — only real quotes match the same reference from both translations with high similarity. This is the default detector in `run.py` when the FAISS index is available.

#### `OnnxQuoteDetector` (slot, not loaded)

Architected for a trained MiniLM/DistilBERT/TinyBERT classifier exported to ONNX. Construction requires `model_path` and `tokenizer_path`. Activated via `--quote-detector onnx`. Not functional without a trained model.

#### Factory

```python
get_detector("heuristic")                    → HeuristicQuoteDetector
get_detector("semantic", retriever=retriever)  → SemanticQuoteDetector
get_detector("onnx", model_path=..., tokenizer_path=...) → OnnxQuoteDetector
```

#### Filtering

`filter_quote_windows(windows, detector, threshold)` → returns `(passed, all_scored)` where passed is `[(window, prob)]` for windows meeting the threshold. Only passed windows proceed to Stage 4 retrieval.

---

### `retrieve.py` — Stage 4: Semantic Retrieval

**Technology:** `BAAI/bge-small-en-v1.5` embeddings + `faiss.IndexFlatIP` (exact cosine via inner product on L2-normalized vectors).

**Data types:**
- `Candidate(reference, translation, book, chapter, verse, text, score)` — one FAISS result
- `Retriever(index, meta, embedder)` — the retrieval engine

**Key functions:**
- `build_index(verses, embedder)` → writes `verses.faiss` + `verses_meta.json`
- `Retriever.from_disk(embedder)` → loads index + metadata
- `Retriever.search(query_texts, top_k)` → `list[list[Candidate]]` — batch search
- `Retriever.search_one(query_text, top_k)` → `list[Candidate]` — single query

**Design decisions:**
- The corpus contains exactly two translations (AMP + NKJV) → two rows per reference in the index
- Queries use the **candidate sentence only** (not the full window), because the full window dilutes the query with non-scripture context
- FAISS is kept blind to translation — both AMP and NKJV candidates compete on cosine similarity, and the re-ranker handles deduplication

---

### `context.py` — Sermon Context Tracker

A running frequency tracker used by the re-ranker's "context match" signal (15% of the final score).

**Mechanism:** As accepted verses accumulate during the pipeline run, a `SermonContext` tracks:
- `book_freq: Counter` — how many times each Bible book has been cited
- `kw_freq: Counter` — keywords from accepted verse texts and transcript windows

**Scoring:**
- Book signal (50%): 1.0 if this book has been seen, scaled by share of history (0.5 floor if the book appears at all)
- Keyword signal (50%): fraction of the candidate verse's keywords present in the context tracker

**Swap point:** Replace with a richer model (trained sequence model, topic model, etc.) without changing the re-ranker — it only calls `SermonContext.score(candidate)`.

---

### `rerank.py` — Stage 5: Hybrid Re-Ranking

Re-scores FAISS candidates with a weighted blend of 5 independent signals:

```
final = 0.45 × semantic   (FAISS cosine similarity, normalized)
      + 0.20 × lexical    (Jaccard + overlap between query and verse)
      + 0.15 × context    (SermonContext book + keyword match)
      + 0.10 × history    (previously accepted verses this transcript)
      + 0.10 × quote_prob (Stage 3 quote-detection probability)
```

#### Lexical Score (`lexical_score`)
- 50% Jaccard + 50% overlap on stopword-filtered keyword sets
- Bonus for explicit reference mention: +0.10 if the book name appears in the query, +0.10 if the chapter:verse pattern appears

#### History Score (`history_score`)
- 1.0 if the exact same reference was already accepted
- 0.5 if the same book was already accepted
- 0.0 otherwise

#### Optional Cross-Encoder
When `--cross-encoder` is active:
- Takes top 10 ranked candidates
- Re-scores each `(query, verse_text)` pair with `ms-marco-MiniLM-L-6-v2`
- Raw logits → sigmoid → min-max normalized to [0, 1]
- Blends at 30%: `final = 0.70 × hybrid_final + 0.30 × cross_encoder`

Degrades gracefully if the model is not downloaded.

#### Representative Selection
`select_representative(ranked)` → picks the top-ranked candidate. When the same reference appears from both AMP and NKJV, the higher-scoring translation becomes the representative; the other stays in the raw candidate list for debugging.

**Data type:** `RankedCandidate(candidate, final, semantic, lexical, context, history, quote_prob, cross_encoder)` — carries all component scores for transparency.

---

### `score.py` — Stage 6: Confidence Scoring

Pure local decision engine. The selected candidate's re-rank score is the confidence.

**Bands:**

| Band | Threshold | Meaning |
|------|-----------|---------|
| `autopilot-eligible` | ≥ 0.96 | High confidence, no human review needed |
| `review queue` | 0.80 — 0.95 | Suspicious — human should verify |
| `ignored` | < 0.80 | Not surfaced by default |

**Output entry format:**
```json
{
  "start_time": 812.4,
  "end_time": 818.9,
  "transcript": "...",
  "quote_probability": 0.97,
  "selected_reference": "Ezekiel 36:27",
  "translation": "NKJV",
  "confidence": 0.94,
  "confidence_band": "review queue",
  "candidates": [
    {"reference": "Ezekiel 36:27", "translation": "NKJV", "score": 0.94},
    {"reference": "Ezekiel 36:27", "translation": "AMP", "score": 0.90},
    ...
  ]
}
```

`is_surfaced(entry, include_ignored)` — filters entries. Default: only `autopilot-eligible` + `review queue`. `--include-ignored` emits all.

---

### `run.py` — Main Pipeline Orchestrator

The CLI entry point that wires Stage 1 through Stage 6 in exact order:

```
python run.py --audio sermon.mp3 --out results.json
python run.py --audio sermon.mp3 --out results.json --cross-encoder
python run.py --transcript transcript.json --out results.json   # skip Whisper
python run.py ... --include-ignored --quote-threshold 0.65
```

**Flow:**
1. Parse CLI args → build `Settings` dataclass
2. **Stage 1:** Transcribe (or load pre-existing transcript)
3. **Stage 2:** Buffer segments into sentence windows
4. Load embedder + FAISS index (shared by Stage 3 and Stage 4)
5. **Stage 3:** Run quote detection (default: semantic); fallback to pure heuristic if semantic produces zero passes
6. **Stage 4:** Batch-retrieve top-K candidates for each passed window
7. **Stage 5+6:** Per-window re-rank, select representative, score, filter → build output entries
8. Write `results.json` + print console summary table

**Graceful degradation:**
- If semantic detector produces zero passes, retries with pure heuristic at a lower bar (0.30) and prints the top 5 for visibility
- If cross-encoder is requested but unavailable, prints a warning and continues without it
- If zero windows pass quote detection, writes an empty results array

**Console output:**
```
=== FaithView Pro -- detected scripture quotes ===
      time  verse                  trans  band                  conf  qprob
---------------------------------------------------------------------------
   0:13:32  Ezekiel 36:27          NKJV   autopilot-eligible   0.97   0.85
   0:25:14  Romans 8:28            AMP    review queue         0.91   0.78
---------------------------------------------------------------------------
total: 2  |  autopilot-eligible: 1  review queue: 1
```

---

### `search.py` — Interactive Verse Search

Standalone CLI for ad-hoc text-to-verse queries (uses the same FAISS index):

```
python search.py "I will put my spirit within you"
python search.py "love your enemies" --top-k 5 --translation NKJV
python search.py                       # interactive mode
```

Follows the same embed + FAISS + re-rank path as the pipeline.

---

### `test_pipeline.py` — Pipeline Tests (No ML Deps)

Unit-tests Stages 2–6 end-to-end using a deterministic `StubEmbedder` (hashed bag-of-words → 384-dim) instead of the real bge model, so no model downloads are needed:

```
python test_pipeline.py
```

Tests: corpus loading format assertions, buffer + quote detection on labeled examples, FAISS build/search/rerank/score path, output entry format compliance.

---

### `benchmark.py` — Performance Profiling

Measures speed of each pipeline stage on this specific CPU:

```
python benchmark.py                    # text search + audio pipeline
python benchmark.py --audio BIM.mp3    # audio pipeline only
python benchmark.py --text-only        # text search only (no audio)
```

Reports: per-query embedding time, FAISS search time, re-rank time, total throughput, and for audio: transcription realtime ratio, per-stage breakdown as % of total pipeline time.

---

### `ffprobe_utils.py` — Audio Metadata

Thin wrapper around `ffprobe` to extract audio duration (used by the benchmark).

---

## Data Flow Diagram

```
amplified.json ─┐
                ├── corpus.load_corpus() ──→ list[Verse] (~62k verses)
  nkjv.json ────┘                                        │
                                                         │
                                          ┌──────────────┘
                                          ▼
                                   build_index.py
                                   embedder.encode_corpus()
                                          │
                                          ▼
                              ┌───────────────────────┐
                              │ index/verses.faiss    │ ← 92 MB, IndexFlatIP
                              │ index/verses_meta.json│ ← 17 MB, one row/verse
                              └───────────────────────┘
                                          │
                          run.py ─────────┘
                              │
                    ┌─────────┴─────────┐
                    │  Retriever.from_disk()  │
                    │  loaded once, shared    │
                    │  by Stage 3 + Stage 4   │
                    └─────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼                               ▼
     Stage 3: quote_detect            Stage 4: retrieve
     (lightweight FAISS              (full top-K FAISS
      pre-check, k=5)                search, k=10)
              │                               │
              └───────────┬───────────────────┘
                          │
                          ▼
              Stage 5: rerank (hybrid blend)
                          │
                          ▼
              Stage 6: score (confidence banding)
                          │
                          ▼
                     results.json
```

---

## Offline Guarantee

Models are downloaded **once** (network ON) into the HuggingFace cache. After that:

- `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1` are set
- `FAITHVIEW_OFFLINE=1` is the default (override with `FAITHVIEW_OFFLINE=0`)
- FAISS index and JSON corpus are purely local files
- All scoring is pure Python — zero network calls at runtime

---

## Benchmark Performance (Measured)

All measurements below were taken on this system with a **60-second MP3 sermon clip** and the full ~62k-verse FAISS index. Models: `Whisper small` (int8, CPU), `bge-small-en-v1.5` (384-dim).

### One-Time Setup Costs

| Operation | Time | Notes |
|-----------|------|-------|
| Embedder model load | 4.79 s | `bge-small-en-v1.5` from HuggingFace cache |
| FAISS index + metadata load | 314.6 ms | 62,200 vectors × 384 dims (92 MB) |
| **Total cold-start** | **~5.1 s** | Paid once per process, amortized across queries |

### Text Search Performance (per query, averaged over 5 queries × 5 rounds)

| Query | Embed | FAISS | Re-rank | **Total** |
|-------|-------|-------|---------|-----------|
| "I will put my spirit within you..." | 24.3 ms | 8.4 ms | 0.4 ms | 33.2 ms |
| "the lord is my shepherd..." | 20.0 ms | 10.2 ms | 0.4 ms | 30.8 ms |
| "love your enemies and pray..." | 24.8 ms | 6.4 ms | 0.3 ms | 31.5 ms |
| "for god so loved the world..." | 24.6 ms | 7.3 ms | 0.4 ms | 32.5 ms |
| "faith is the substance of things hoped for..." | 23.8 ms | 8.5 ms | 0.4 ms | 32.9 ms |
| **Average** | **23.5 ms** | **8.2 ms** | **0.4 ms** | **32.2 ms** |

**Throughput: 31 queries/second** (text-only search path).

Key takeaway: Per-query search (embed + FAISS + re-rank) takes ~32 ms, dominated by the embedding step (~73%). FAISS flat search on 62k × 384 dims is ~8 ms. Re-rank is negligible (<0.4 ms). The pipeline can handle ~31 concurrent verse lookups per second on CPU.

### Full Audio Pipeline (60-second MP3 clip, "BIM.mp3")

| Stage | Time | % of Pipeline | Notes |
|-------|------|---------------|-------|
| **1. Transcribe** (Whisper small) | **13.89 s** | **98.3%** | 5 segments, 231× faster than realtime |
| 2. Buffer | 0.2 ms | 0.0% | 7 sentence windows |
| **(Model load)** | 509 ms | 3.6% | Embedder + FAISS (one-time, excluded from total) |
| 3. Quote detection (semantic) | 206.3 ms | 1.5% | 1/7 windows passed, 7 scored |
| 4. Retrieval (top-10) | 34.4 ms | 0.2% | 1 query |
| 5+6. Re-rank + score | 0.3 ms | 0.0% | 1 entry |
| **TOTAL (excl. model load)** | **14.13 s** | **100%** | |

**Processing ratio: 0.24× realtime** — the pipeline processes 4.2 seconds of audio per second of processing time. A 45-minute (2700s) sermon would take approximately **10.8 minutes** to process end-to-end.

### Stage-by-Stage Breakdown

```
98.3% ████████████████████████████████████████████████████████  Transcription (Whisper)
 1.5% ▌  Quote detection (semantic FAISS pre-check)
 0.2% ▏  Retrieval (FAISS top-10)
 0.0% ▏  Buffering + Re-rank + Score
```

**Transcription dominates.** Everything after Stage 1 is sub-250ms for a typical file. Optimizing Whisper (smaller model, GPU/AVX512, or `small.en` for English-only) yields the biggest speedup.

### Scaling Estimates by Sermon Length

| Sermon Length | Whisper Time (4.2× realtime) | Rest of Pipeline | Total |
|---------------|------------------------------|------------------|-------|
| 1 minute | 13.9 s | 0.24 s | **14.1 s** |
| 5 minutes | 69.5 s | ~1.2 s | **70.7 s** |
| 15 minutes | 3.5 min | ~3.6 s | **3.6 min** |
| 30 minutes | 7.0 min | ~7.2 s | **7.1 min** |
| 45 minutes | 10.5 min | ~10.8 s | **10.7 min** |
| 60 minutes | 14.0 min | ~14.4 s | **14.2 min** |

Non-transcription stages scale linearly with the number of detected windows (~1–2 per minute in typical sermons), remaining under 15 seconds even for hour-long audio.

### Memory Footprint

| Component | Size | Notes |
|-----------|------|-------|
| Whisper model (small, int8) | ~500 MB | Loaded once, then freed |
| Embedder (bge-small) | ~130 MB | Singleton, stays resident |
| FAISS index (62k × 384) | 92 MB | Memory-mapped on load |
| Metadata (JSON) | 17 MB | Loaded into RAM |
| **Total runtime memory** | **~240 MB** | After Whisper is freed |

### FAISS Index Build Performance

| Operation | Value |
|-----------|-------|
| Corpus size | 62,200 verses (AMP + NKJV) |
| Embedding dimensions | 384 |
| Index type | `IndexFlatIP` (exact cosine) |
| Index file size | 92 MB |
| Metadata file size | 17 MB |
| Build time | ~10–25 min (CPU, batched) |
| Per-query search time | ~8.2 ms |

---

## Swap Points for Real-Time Console Integration

| Component | What Changes | What Stays |
|-----------|-------------|-----------|
| Quote classifier | Implement `QuoteDetector.score` for ONNX model | Rest of pipeline unchanged |
| FAISS index type | Edit `retrieve.build_index` only | Rest of `Retriever` unchanged |
| Embedding model | Change `config.EMBED_MODEL`, rebuild index | Interface unchanged |
| Re-rank weights | Change `config.RerankWeights` dataclass | `rerank_candidates` unchanged |
| Sermon context | Replace `SermonContext` behind same `.score()` API | Re-ranker unchanged |
| Transcription source | Feed live captions via `segments_from_dicts()` | All downstream stages unchanged |
| Corpus | Replace `amplified.json`/`nkjv.json`, rebuild index | Loader format-tolerant |

---

See the [Benchmark Performance](#benchmark-performance-measured) section above for detailed timing measurements on this CPU. For faster transcription, use `--whisper-model small.en` (English-only) or `tiny`/`base`.
