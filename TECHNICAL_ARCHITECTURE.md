# FaithView Pro — Full Technical Architecture

## Table of Contents

1. [System Overview](#system-overview)
2. [High-Level Architecture](#high-level-architecture)
3. [Detailed Data Flow & Pipeline](#detailed-data-flow--pipeline)
4. [Module-by-Module Deep Dive](#module-by-module-deep-dive)
5. [Machine Learning Models](#machine-learning-models)
6. [Live Real-Time Pipeline](#live-real-time-pipeline)
7. [Intent Router & Session Management](#intent-router--session-management)
8. [Bible Data Layer](#bible-data-layer)
9. [Server, WebSocket & REST API](#server-websocket--rest-api)
10. [Configuration System](#configuration-system)
11. [Performance Profiling & Benchmarks](#performance-profiling--benchmarks)
12. [Design Patterns & Extensibility](#design-patterns--extensibility)
13. [Entry Points Summary](#entry-points-summary)

---

## System Overview

FaithView Pro is an **offline semantic scripture-detection engine** operating in two modes:

| Mode | Entry Point | Description |
|------|------------|-------------|
| **Batch** | `run.py` | Process a pre-recorded sermon audio file (MP3/WAV) end-to-end, producing `results.json` |
| **Live** | `server.py` + `live_pipeline.py` | Real-time microphone capture with an operator console served over WebSocket/REST |

The system transcribes spoken English sermons, detects when scripture is being quoted, and matches each detected passage to the exact Bible verse across up to 7 translations — all running locally on CPU with zero cloud calls, except for an optional Deepgram cloud-STT path.

### Core Capability

```
Spoken sermon audio  →  Timestamped list of {verse reference, translation, confidence, exact text}
```

### Three ML Models (all offline after one-time download)

| Model | Size | Purpose |
|-------|------|---------|
| `faster-whisper small.en` (int8, CPU) | ~500 MB | Speech-to-text transcription |
| `BAAI/bge-small-en-v1.5` (384-dim) | ~130 MB | Semantic embeddings + FAISS vector search |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` (optional) | ~90 MB | Re-rank accuracy boost |

---

## High-Level Architecture

```
                              ┌──────────────────────────────────┐
                              │          AUDIO INPUT             │
                              │  Microphone stream | .mp3/.wav   │
                              └──────────────┬───────────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │   TRANSCRIPTION  │
                                    │  faster-whisper  │
                                    │  small.en, int8  │
                                    │  (or Deepgram)   │
                                    └────────┬─────────┘
                                             │  list[Segment]
                                    ┌────────▼─────────┐
                                    │ SENTENCE BUFFER  │
                                    │  .!? splitting   │
                                    │  rolling windows │
                                    └────────┬─────────┘
                                             │  list[Window]
                              ┌──────────────┴──────────────┐
                              │      INTENT ROUTER          │
                              │  (live mode only)           │
                              └──┬────────┬────────┬────────┘
                                 │        │        │
                    ┌────────────▼──┐ ┌───▼────┐ ┌─▼──────────┐
                    │ EXPLICIT_REF  │ │  NAV   │ │ CLEAR      │
                    │ direct DB     │ │command │ │command     │
                    │ lookup        │ │        │ │            │
                    └───────────────┘ └────────┘ └────────────┘
                                 │
                                 │ CUE_PHRASE (fallback)
                        ┌────────▼────────┐
                        │ QUOTE DETECTION │
                        │ Heuristic/ONNX  │
                        │ P(scripture)    │
                        └────────┬────────┘
                                 │  windows ≥ threshold
                        ┌────────▼────────┐
                        │ FAISS RETRIEVAL │
                        │ bge-small embed │
                        │ IndexFlatIP     │
                        │ 62k verses      │
                        └────────┬────────┘
                                 │  top-K candidates
                        ┌────────▼────────┐
                        │ HYBRID RE-RANK  │
                        │ 5-signal blend  │
                        │ + cross-encoder │
                        └────────┬────────┘
                                 │  selected + score
                        ┌────────▼────────┐
                        │   CONFIDENCE    │
                        │    BANDING      │
                        │ autopilot/review│
                        └────────┬────────┘
                                 │
                        ┌────────▼────────┐
                        │    OUTPUT       │
                        │ results.json /  │
                        │ WebSocket events│
                        └─────────────────┘
```

### Two Data Retrieval Paths

The system has **two independent data retrieval mechanisms** designed for different use cases:

| Path | Mechanism | Search Type | When Used | Versions Available |
|------|-----------|-------------|-----------|--------------------|
| **Semantic Search** | FAISS + bge-small embeddings | Nearest-neighbor cosine similarity | CUE_PHRASE path (unknown scripture quote) | AMP + NKJV (2 translations indexed) |
| **Direct Lookup** | `BibleDB` in-memory hash table | Exact `(translation, book, chapter, verse)` key | EXPLICIT_REF, NAV_COMMAND, `/present` REST endpoint | KJV, NIV, NKJV, NLT, AMP, ESV, MSB (7 translations) |

These two paths are deliberately separate: the FAISS index only embeds 2 translations because vector search is expensive to build and tune; the direct-lookup DB covers all 7 translations because exact-key lookups are cheap and operator-requested.

---

## Detailed Data Flow & Pipeline

### Batch Pipeline (run.py): Stage-by-Stage Transformation

```
Stage 1: transcribe.py
  Input:   Audio file path (string)
  Process: faster-whisper (CTranslate2 backend, int8 quantized)
  Output:  list[Segment]   where Segment = {text: str, start_time: float, end_time: float,
                                              words: list[Word] | None}
  Time:    ~98% of total pipeline duration

Stage 2: buffer.py
  Input:   list[Segment]
  Process: 1. Split segments into sentences via .!? boundary detection
           2. Build rolling windows: prev_sentence + current_sentence (candidate) + next_12_words
           3. The full window is context for retrieval; candidate_sentence is what gets scored
  Output:  list[Window]    where Window = {text: str, candidate_text: str, start_time: float,
                                            end_time: float, prev_text: str, next_text: str,
                                            index: int}

Stage 3: quote_detect.py
  Input:   list[Window]
  Process: For each window, call detector.score(candidate_text) → float in [0,1]
           Filter to windows where score >= QUOTE_THRESHOLD (default 0.70)
  Output:  list[(Window, float)]  — filtered windows with quote probability

Stage 4: retrieve.py (only for passed windows)
  Input:   list[(Window, float)]
  Process: 1. Embed each candidate_text with bge-small-en-v1.5 (with query prefix)
           2. Each embedding → FAISS.IndexFlatIP.search(k=TOP_K) across 62k verses
           3. Returns candidates from both AMP and NKJV translations
  Output:  list[list[Candidate]]  where Candidate = {reference, translation, book, chapter,
                                                      verse, text, score (FAISS cosine)}

Stage 5: rerank.py
  Input:   list[list[Candidate]], quote probabilities, SermonContext, history
  Process: Hybrid 5-signal weighted blend:
             45% semantic  (FAISS cosine similarity, normalized across candidates)
             20% lexical   (Jaccard + overlap between query keywords and verse text)
             15% context   (SermonContext book-frequency + keyword tracker)
             10% history   (previously accepted verses in this transcript)
             10% quote_prob (Stage 3 probability)
           Optional cross-encoder: if enabled, re-scores top 10 with ms-marco-MiniLM-L-6-v2
           at 30% blend: final = 0.70 × hybrid + 0.30 × cross_encoder
           select_representative() picks the highest-scoring translation per unique reference
  Output:  list[(RankedCandidate, list[RankedCandidate])]

Stage 6: score.py
  Input:   Window, quote_probability, selected RankedCandidate, all candidates
  Process: confidence = selected_candidate.final_score
           band = "autopilot-eligible" if ≥ AUTOPILOT_THRESHOLD else
                  "review queue" if ≥ REVIEW_THRESHOLD else "ignored"
           build_entry() assembles the final JSON object
  Output:  list[dict] — each dict contains {start_time, end_time, transcript,
           quote_probability, selected_reference, translation, confidence,
           confidence_band, candidates: [...]}
```

### Live Pipeline (server.py + live_pipeline.py): Real-Time Flow

```
MicSource.stream() → 16kHz mono PCM chunks (streaming iterator)
        │
        ▼
LiveTranscriber → Rolling window transcription (8s window, 2s overlap)
        │          Each chunk: Whisper transcribes → deduplicate overlap → emit Segment
        │          condition_on_previous_text=False (avoids "..." hallucination)
        ▼
_on_new_segment(Segment) → broadcast transcript_update via WebSocket
        │                   accumulate segments → re-buffer → get new windows
        ▼
_route_window(Window)
        │
        ├── route() → EXPLICIT_REF  → resolve_book → BibleDB.lookup(T, B, C, V) → DisplayEvent
        ├── route() → NAV_COMMAND   → step_verse(+1/-1), swap_translation → DisplayEvent
        ├── route() → CLEAR_COMMAND → DisplayEvent(clear=True), anchor survives
        │
        └── route() → CUE_PHRASE → _extend_cue_accum(Window)
                │
                │  CUE accumulator joins text across segment boundaries:
                │  - Opens on first CUE window meeting min_words threshold
                │  - Extends with subsequent CUE windows (up to word cap)
                │  - Closes on sentence-ending punctuation or non-CUE intent
                └── _run_accumulated_search(full_text, is_final)
                        │
                        ├── detector.score(text) or live_rescue fallback
                        ├── Build query variants: full, tail-window, clause-split
                        ├── For each variant: FAISS search → rerank → score
                        ├── Cross-variant agreement bonus (same ref from multiple variants)
                        ├── Filter by live_confidence_floor (default 0.45)
                        └── Emit DetectionEvent (always) + autopilot DisplayEvent (if autopilot mode)
```

### Data Shapes Through the Pipeline

```
Segment:
  { text: "I will put my spirit within you",
    start_time: 812.4,
    end_time: 818.9,
    words: [{text, start_time, end_time, probability}, ...] | None }

Window:
  { text: "And God says I will put my spirit within you and ye shall live.",   ← full window (context)
    candidate_text: "I will put my spirit within you and ye shall live.",       ← scored sentence
    start_time: 812.4,
    end_time: 828.1,
    prev_text: "And God says",
    next_text: "",
    index: 14 }

Candidate (from FAISS):
  { reference: "Ezekiel 36:27",
    translation: "AMP",
    book: "Ezekiel", chapter: 36, verse: 27,
    text: "I will put my Spirit within you and cause you to walk in My statutes...",
    score: 0.8742 }

RankedCandidate (from rerank):
  { candidate: Candidate(...),
    final: 0.9421,
    semantic: 0.8742, lexical: 0.1834, context: 0.0, history: 0.0,
    quote_prob: 0.85, cross_encoder: None }

Output entry (to results.json):
  { start_time: 812.4, end_time: 828.1,
    transcript: "I will put my spirit within you and ye shall live.",
    quote_probability: 0.85,
    selected_reference: "Ezekiel 36:27",
    translation: "NKJV",
    confidence: 0.942,
    confidence_band: "review queue",
    candidates: [{reference: "Ezekiel 36:27", translation: "NKJV", score: 0.924},
                 {reference: "Ezekiel 36:27", translation: "AMP", score: 0.919}, ...] }
```

---

## Module-by-Module Deep Dive

### `config.py` — Central Configuration

All tunable parameters in one place. The `Settings` dataclass allows CLI overrides without mutating module-level globals.

| Category | Key | Default | Purpose |
|----------|-----|---------|---------|
| **Paths** | `AMP_PATH`, `NKJV_PATH` | `amplified.json`, `nkjv.json` | Bible corpus JSON for FAISS indexing |
| **Paths** | `INDEX_DIR` | `index/` | FAISS index + metadata directory |
| **Models** | `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Sentence transformer for embeddings |
| **Models** | `EMBED_DIM` | `384` | Embedding vector dimension |
| **Models** | `WHISPER_MODEL` | `small.en` | faster-whisper model ID |
| **Models** | `WHISPER_LANGUAGE` | `en` | Transcription language (None = auto-detect) |
| **Models** | `WHISPER_BEAM_SIZE` | `1` | Beam search width (1 = greedy, fastest) |
| **Models** | `CROSS_ENCODER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Optional re-ranker |
| **Offline** | `OFFLINE` | `True` (env `FAITHVIEW_OFFLINE` override) | Force local-files-only |
| **Stage 2** | `BUFFER_NEXT_WORDS` | `12` | Lookahead words in rolling window |
| **Stage 3** | `QUOTE_THRESHOLD` | `0.70` | Minimum quote probability for retrieval |
| **Stage 4** | `TOP_K` | `10` | FAISS nearest neighbors per query |
| **Stage 4** | `RETRIEVE_BATCH` | `32` | Batch size for embedding queries |
| **Stage 5** | `RerankWeights` | `(0.45, 0.20, 0.15, 0.10, 0.10)` | 5-signal blend weights |
| **Stage 5** | `CROSS_ENCODER_TOPN` | `10` | Max candidates for cross-encoder re-rank |
| **Stage 5** | `USE_CROSS_ENCODER` | `False` | Opt-in cross-encoder |
| **Stage 6** | `AUTOPILOT_THRESHOLD` | `0.60` | Autopilot confidence floor |
| **Stage 6** | `REVIEW_THRESHOLD` | `0.60` | Review-queue confidence floor |
| **VAD** | `WHISPER_VAD_MIN_SILENCE_MS` | `800` | VAD silence threshold |
| **Deepgram** | `DEEPGRAM_UTTERANCE_END_MS` | `800` | Deepgram utterance end threshold |
| **Live** | `TRANSCRIPTION_SOURCE` | `whisper` | Default transcription backend |

The `Settings` dataclass (`config.py:110`) subclasses the module constants so CLI flags can override specific values without touching module-level state, enabling clean unit-testing with isolated settings.

---

### `corpus.py` — Bible Corpus Loader (FAISS Data)

Loads the dual-translation corpus for FAISS indexing. Distinct from `bible_db.py`.

**Schema (from data files):**
```json
{
  "version": "AMPLIFIED",
  "books": [{
    "book": "Genesis",
    "chapters": [{
      "chapter": 1,
      "verses": [{"verse": 1, "text": "IN THE beginning God created..."}]
    }]
  }]
}
```

**Key data types:**
- `Verse` — frozen dataclass: `(translation, book, chapter, verse, text)`
  - Properties: `reference` → `"Ezekiel 36:27"`, `key` → translation-agnostic reference
- `load_corpus(amp_path, nkjv_path)` → `list[Verse]` — ~62k total verses
- `load_translation(path)` → `list[Verse]` — single translation
- Normalizes `"AMPLIFIED"` → `"AMP"`

---

### `bible_db.py` — Direct-Lookup Database (All 7 Versions)

Separate from `corpus.py` by design — the live operator console needs all 7 translations for direct lookups and translation swaps, not just the 2 embedded in FAISS.

**On-disk schema (`bible_all_versions.json`):**
```json
{
  "versions": ["KJV", "NIV", "NKJV", "NLT", "AMPLIFIED", "ESV", "MSB"],
  "books": [{
    "book": "Genesis",
    "chapters": [{
      "chapter": 1,
      "verses": [{
        "verse": 1,
        "text": {"KJV": "...", "NIV": "...", "NKJV": "...", ...}
      }]
    }]
  }]
}
```

**Internal storage:** Two in-memory dicts built at load time:
- `_by_tv: {(translation_code, book, chapter, verse) → text}` — primary key
- `_by_ref: {(book, chapter, verse) → {translation_code: text}}` — reverse index

**Key types and functions:**
- `VerseRef` — frozen dataclass: `(translation, book, chapter, verse, text)` with computed `reference` and `key` properties
- `BibleDB.lookup(translation, book, chapter, verse)` → `VerseRef | None`
- `BibleDB.translations_for(book, chapter, verse)` → `list[str]` — all translations covering a reference
- `BibleDB.max_verse(book, chapter)` → `int` — highest verse number in a chapter
- `BibleDB.max_chapter(book)` → `int` — highest chapter number in a book
- `BibleDB.chapter_verses(translation, book, chapter)` → `list[VerseRef]` — entire chapter for Bible reader panel
- `resolve_book(name)` → canonical book name from aliases (handles "first samuel", "2 kings", "iii john", etc.)
- `resolve_translation(name)` → short code from aliases (handles "amplified bible", "the message", etc.)
- `get_bible_db()` → thread-safe singleton, lazy-loaded

**66-book canonical order** is hardcoded to match the FAISS corpus structure.

**Alias tables** map 80+ spoken/written variants for books and 25+ for translations.

---

### `models.py` — ML Model Singletons

Thread-safe, lazy-loaded model singletons. Once loaded, models persist for the process lifetime.

**`Embedder` class:**
- Wraps `sentence-transformers` with `BAAI/bge-small-en-v1.5`
- `encode(texts, batch_size)` → `np.ndarray` of 384-dim L2-normalized vectors
- `encode_corpus(verses, batch_size)` → corpus-level embeddings
- Automatically prepends `QUERY_PREFIX` ("Represent this sentence for searching relevant passages: ") to queries **only** — never to corpus verses (matches bge convention)

**`get_embedder(offline=True)` → `Embedder`:**
- Singleton pattern with module-level lock
- Sets `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1` when offline

**`get_cross_encoder(offline=True)` → `CrossEncoder`:**
- Optional — only loaded when `--cross-encoder` is requested
- Degrades gracefully if model is not cached

---

### `transcribe.py` — Stage 1: Audio Transcription

**Technology:** `faster-whisper` with CTranslate2 backend, `int8` compute type — optimized for CPU.

**Data types:**
- `Word(text, start_time, end_time, probability)` — optional word-level timestamps
- `Segment(text, start_time, end_time, words)` — per-segment with optional words

**Key functions:**
- `transcribe(audio_path, settings, word_timestamps)` → `list[Segment]`
  - Loads Whisper model, transcribes audio, returns timestamped segments
  - Filters low-quality segments via `whisper_min_avg_logprob` and `whisper_max_no_speech_prob`
- `segments_from_dicts(dicts)` → `list[Segment]` — bypass Whisper for pre-existing transcripts or live feed

---

### `buffer.py` — Stage 2: Sentence Buffering

Groups raw Whisper segments into rolling sentence windows with context.

**Algorithm:**
1. Concatenate all segment texts
2. Split into sentences at `.`, `!`, `?` boundaries
3. For each sentence, build a `Window`:
   - `window.text` = previous sentence + current sentence + next `BUFFER_NEXT_WORDS` words (full context for retrieval)
   - `window.candidate_text` = current sentence only (what gets scored by quote detection)
   - `window.start_time` / `window.end_time` — from word-level timestamps when available, segment boundaries otherwise

**Why context separation matters:** The full window provides more context for FAISS retrieval (more words = better similarity signal), but quote detection scores only the candidate sentence to avoid false positives from neighboring non-scripture speech.

**Data types:**
- `Sentence(text, start_time, end_time, index)`
- `Window(text, candidate_text, start_time, end_time, prev_text, next_text, index)`

- `buffer_segments(segments)` → `list[Window]`

---

### `quote_detect.py` — Stage 3: Quote Detection

Decides whether a sentence window is scripture being quoted (vs. story, joke, teaching). Three detector types behind a common interface.

#### Interface

```python
class QuoteDetector(ABC):
    def score(self, text: str) -> float:  # returns P(text is scripture) in [0, 1]
```

#### `HeuristicQuoteDetector` (default, pure rules, zero ML)

10 weighted signals combined into a calibrated probability:

| Signal | Max Weight | Mechanism |
|--------|-----------|-----------|
| Cue phrase | 0.55 | 48 patterns: "the Bible says", "it is written", "Paul writes", "Scripture declares", etc. |
| Scripture reference | 0.55 | Regex for "Romans 8:28", bare "8:28", "chapter 3 verse 16" |
| Archaic vocabulary | 0.45 | 33 words: "thou", "hath", "saith", "behold", "cometh", "goeth", etc. — strong verbatim signal |
| Divine-speech patterns | 0.60 | 95 patterns: "I will put my Spirit…", "you shall love…", "truly I say…", "thus says the Lord…" |
| Theological vocabulary | 0.25 | 110 words: "righteousness", "covenant", "sanctification", "salvation", etc. |
| Quotation marks | 0.08 | Presence of `"..."` in transcript |
| Bible book mention | 0.06 | 66 book names: "Ezekiel", "Romans", "Psalms" |
| Quoting verb bonus | 0.07 | "says", "writes", "tells us" + another signal present |
| Negative markers | -0.25 | "breaking news", "knock knock", "stock market", "weather forecast" — strong down-weight |
| Base probability | 0.10 | Floor for any text |

**Calibration:** Tuned via labeled examples in `quote_detect.py`'s built-in self-test. Known limitation: verbatim scripture in modern English with no cue phrase and no reference can score below 0.70.

#### `SemanticQuoteDetector` (heuristic + FAISS concordance pre-check)

Wraps the heuristic with an early FAISS check:
1. Run heuristic first
2. If heuristic passes: run FAISS search (k=5) on candidate text
3. If the **same reference** appears from **both AMP and NKJV** with high cosine (≥0.70) → strong boost (dual-translation agreement is the strongest offline signal of verbatim quoting)
4. Fallback: single-match ≥0.80 with heuristic ≥0.25 → moderate boost

This is the strongest offline signal — only real scripture quotes match the same reference from two independent translations with high similarity.

#### `OnnxQuoteDetector` (trained ML, slot ready)

A fine-tuned MiniLM/DistilBERT classifier exported to ONNX. Model exists at `onnx_model/` directory. Accepts `model_path` and `tokenizer_path` at construction.

#### Factory and Filtering

- `get_detector("heuristic")` → `HeuristicQuoteDetector`
- `get_detector("semantic", retriever=retriever)` → `SemanticQuoteDetector`
- `get_detector("onnx", model_path=..., tokenizer_path=...)` → `OnnxQuoteDetector`
- `filter_quote_windows(windows, detector, threshold)` → `(passed, all_scored)`

---

### `retrieve.py` — Stage 4: Semantic Retrieval

**Technology:** `bge-small-en-v1.5` embeddings + `faiss.IndexFlatIP` (exact inner product on L2-normalized vectors = exact cosine similarity).

**Data types:**
- `Candidate(reference, translation, book, chapter, verse, text, score)` — one FAISS result
  - `score` is the raw FAISS cosine similarity [0, 1]
  - Properties: `key` (translation-agnostic reference)
- `Retriever(index, meta, embedder)` — the search engine

**Key functions:**
- `build_index(verses, embedder, output_dir)` → writes `verses.faiss` + `verses_meta.json`
  - Encodes all verses in batches, L2-normalizes, builds `IndexFlatIP`
- `Retriever.from_disk(embedder, index_path, meta_path)` → `Retriever`
  - Memory-maps the FAISS index, loads metadata into Python list
- `Retriever.search(query_texts, top_k)` → `list[list[Candidate]]` — batch search
- `Retriever.search_one(query_text, top_k)` → `list[Candidate]` — single query

**Design decisions:**
- Corpus = AMP + NKJV combined (~62k verses × 2 rows per reference)
- FAISS is blind to translation — both AMP and NKJV compete on cosine similarity
- The re-ranker handles deduplication when the same reference appears from both translations
- Query embedding uses the **candidate sentence only** (not the full window) to avoid diluting the query with non-scripture context
- `IndexFlatIP` over approximate indexes because 62k × 384 dims is small enough for exact sub-10ms search on CPU

---

### `rerank.py` — Stage 5: Hybrid Re-Ranking

Re-scores FAISS candidates with 5 independent signals, blended with configurable weights.

#### Signal Computation

1. **Semantic (45%)**: FAISS cosine similarity, min-max normalized across the candidate set
2. **Lexical (20%)**: `lexical_score(query, candidate)`
   - 50% Jaccard similarity + 50% token overlap on stopword-filtered keyword sets
   - Bonus: +0.10 if book name appears in query, +0.10 if chapter:verse pattern appears
3. **Context (15%)**: `SermonContext.score(candidate)`
   - 50% book signal: 1.0 if book has been seen, scaled by frequency share (≥0.5 floor if book appears at all)
   - 50% keyword signal: fraction of candidate verse's keywords present in the running context tracker
4. **History (10%)**: `history_score(candidate, accepted_keys, accepted_books)`
   - 1.0 if exact reference already accepted in this transcript
   - 0.5 if same book already accepted
   - 0.0 otherwise
5. **Quote probability (10%)**: Stage 3's raw quote-detection score

Final: `weighted_sum(semantic, lexical, context, history, qprob)`

#### Optional Cross-Encoder

When `--cross-encoder` is active:
- Takes top `CROSS_ENCODER_TOPN` (default 10) candidates
- Re-scores each `(query, verse_text)` pair with `ms-marco-MiniLM-L-6-v2`
- Raw logits → sigmoid → min-max normalized to [0, 1]
- Blends at 30%: `final = 0.70 × hybrid + 0.30 × cross_encoder`
- Degrades gracefully if model is not downloaded

**Data type:** `RankedCandidate(candidate, final, semantic, lexical, context, history, quote_prob, cross_encoder)` — all component scores preserved for transparency.

#### Representative Selection

`select_representative(ranked)` → picks the top candidate. When the same reference appears from both AMP and NKJV, the higher-scoring translation becomes the representative; the other stays in the raw candidate list.

**Key functions:**
- `rerank_candidates(candidates, query_text, qprob, context, accepted_keys, accepted_books, weights, cross_encoder)` → `list[RankedCandidate]`
- `select_representative(ranked)` → `(RankedCandidate | None, list[RankedCandidate])`

---

### `score.py` — Stage 6: Confidence Scoring & Banding

Pure local decision engine. The selected candidate's re-rank score is the confidence.

**Bands:**

| Band | Threshold | Meaning |
|------|-----------|---------|
| `autopilot-eligible` | ≥ 0.60 | High confidence — no human review needed |
| `review queue` | 0.60 — 0.95 | Suspicious — human should verify |
| `ignored` | < 0.60 | Not surfaced by default |

**Key functions:**
- `confidence_for(ranked_candidate)` → `float` — the final score
- `band(confidence)` → `str` — one of the three band labels
- `build_entry(window, qprob, selected, ranked)` → `dict` — JSON-serializable entry
- `is_surfaced(entry, include_ignored=False)` → `bool`

**Important:** The live pipeline uses a separate `live_confidence_floor` (default 0.45) to surface more detections in real time, since live speech produces lower-confidence matches than batch. The band labels remain the same so the UI can color-code by tier.

---

### `context.py` — Sermon Context Tracker

Running frequency tracker used by the re-ranker's context-match signal.

**`SermonContext`:**
- `book_freq: Counter` — how many times each Bible book has been cited
- `kw_freq: Counter` — keywords from accepted verse texts and transcript windows
- `add(book, text)` — increment frequencies
- `add_candidate(candidate, transcript_text)` — add from a detection result
- `score(candidate)` → `float` — book match (50%) + keyword overlap (50%)
- `book_history()` → `list[str]` — most-cited books in order

**Design:** The re-ranker only calls `SermonContext.score(candidate)`, so the context model can be replaced with a richer model (trained sequence model, topic model, etc.) without changing the re-ranker.

---

### `intent_router.py` — Live Intent Router

Classifies each transcribed window into one of four intents. Pure functions — no I/O beyond the BibleDB passed as argument.

#### Four Intent Types

| Intent | Trigger | Resolution | Session Impact |
|--------|---------|------------|----------------|
| `EXPLICIT_REF` | Reference regex matches: "Romans 8:28", "2 Kings 6:2", "Genesis chapter 1 verse 1" | `resolve_book()` → `BibleDB.lookup()` direct exact lookup | Anchors `current_reference` |
| `NAV_COMMAND` | "next verse", "previous verse", "next chapter", "give me that in the Amplified" | Increment/decrement verse or chapter, swap translation, resolve via `BibleDB` | Updates `current_reference` |
| `CLEAR_COMMAND` | "remove it", "clear it", "take it down", "not that one" (≤8 word utterances) | Emit `DisplayEvent(clear=True)` | **Does NOT reset** `current_reference` |
| `CUE_PHRASE` | Everything else (fallthrough) | Pass to full detection pipeline (quote_detect → retrieve → rerank → score) | None |

#### Reference Parser

The reference regex supports:
- Ordinal prefixes: `1`, `2`, `3`, `I`, `II`, `III`, `first`, `second`, `third`, `1st`, `2nd`, `3rd`
- Multi-word books: "Song of Solomon"
- Various separator patterns: `Romans 8:28`, `Romans 8 28`, `Genesis chapter 1 verse 1`
- Translation detection within the same utterance: "in the Amplified", "from the ESV"

#### Navigation Commands

- **Verse stepping:** `_step_verse()` advances +1/-1, wrapping chapter boundaries correctly
- **Chapter stepping:** Advances chapter +1 or -1, resolving to first/last available verse
- **Translation swap:** 11 different regex patterns match "give me that in the NKJV", "now in the ESV", etc. A broad fallback pattern is gated to short (≤6 word) utterances for safety.
- **Session anchor:** `SessionState.current_reference` is updated on every display and survives CLEAR commands

#### Self-Test

The module contains a comprehensive `__main__` self-test with 20+ test cases covering all intent types, edge cases (end-of-book wrapping, CLEAR-anchor survival, translation resolution), making the router verifiable without audio or ML dependencies.

---

### `live_transcribe.py` — Live Microphone Transcription

Wraps `faster-whisper` for real-time audio streaming.

**`AudioSource` (ABC):**
- `stream()` → `Iterator[tuple[np.ndarray, float]]` — yields (16kHz mono float32 chunk, timestamp)

**Implementations:**
- `MicSource(device=None)` — microphone via `sounddevice` (PortAudio)
- `FileSource(path, realtime=True)` — replays a file as if it were a mic

**`LiveTranscriber`:**
- Chunked transcription: rolling windows (default 8s chunk, 2s overlap)
- Each chunk → Whisper transcribes → deduplicate overlap region → emit `Segment` via callback
- Audio preprocessing: Butterworth high-pass filter for mic cleanup (`preprocess=True`)
- `initial_prompt` biases decoding toward scripture vocabulary (KJV/NKJV phrasing)
- `condition_on_previous_text=False` for live mode (avoids "..." continuation hallucinations across chunks)
- `vad_filter=True` uses Whisper's internal Voice Activity Detection

**PortAudio:** The library bundles a `lib/libportaudio.so.2` for self-contained offline mic support.

---

### `transcription_source.py` — Abstract Transcription Interface

```python
class TranscriptionSource(ABC):
    def start(self, audio_source, on_segment: Callable, on_interim: Callable | None) -> None: ...
    def stop(self) -> None: ...
    @property
    def name(self) -> str: ...
```

Two implementations:
- `WhisperTranscriptionSource` (fully offline, wraps `LiveTranscriber`)
- `DeepgramSource` (cloud STT, requires `DEEPGRAM_API_KEY`)

Both produce `Segment` objects through the same `on_segment` callback, so all downstream stages (`buffer.py`, `intent_router.py`, etc.) are completely agnostic to the transcription source.

---

### `deepgram_transcribe.py` — Deepgram Cloud Transcription

Alternative transcription backend using Deepgram Nova-3 WebSocket API.

- Streams mic audio as 16kHz int16 PCM over WebSocket
- `utterance_end_ms=800` for endpointing
- Converts Deepgram's `is_final` results into `Segment` objects (same shape as Whisper output)
- Supports interim results via `on_interim` callback (for live captions)
- Requires `DEEPGRAM_API_KEY` environment variable

---

### `ffprobe_utils.py` — Audio Metadata

Thin wrapper around `ffprobe` to extract audio duration (used by the benchmark).

---

### `build_index.py` — One-Time FAISS Index Construction

```
python build_index.py                 # offline (models already cached)
python build_index.py --online        # first run: allows embedder download
python build_index.py --limit 2000    # smoke test on subset
```

**Workflow:**
1. Load AMP + NKJV corpus via `corpus.load_corpus()` (~62k verses)
2. Get `Embedder` singleton
3. Encode all verses in batches → L2-normalized 384-dim vectors
4. Build `faiss.IndexFlatIP`
5. Write `index/verses.faiss` (~92 MB) + `index/verses_meta.json` (~17 MB)

Must be re-run when corpus data changes. The rest of the pipeline works with the index blindly.

---

### `download_models.py` — One-Time Model Download

Run once with network ON to populate the HuggingFace cache (`~/.cache/huggingface`):

```
python download_models.py                  # embedder + whisper small.en
python download_models.py --cross-encoder  # also fetch cross-encoder
```

After this, all subsequent runs use the default offline mode (zero network calls).

---

## Machine Learning Models

### Model Loading Strategy

All models use a **singleton pattern with lazy loading**:
- First invocation: download from HuggingFace Hub (if online) or load from local cache (offline)
- Subsequent invocations: return cached singleton
- Thread-safe via module-level locks

### Offline Guarantee

Three mechanisms enforce zero network calls at runtime:
1. `config.OFFLINE = True` by default (env `FAITHVIEW_OFFLINE=1`)
2. `models.py` sets `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_DATASETS_OFFLINE=1`
3. FAISS index and JSON corpus are purely local files

### Model 1: faster-whisper (Speech-to-Text)

| Property | Value |
|----------|-------|
| Model | `small.en` (English-only) |
| Backend | CTranslate2 |
| Precision | int8 (quantized) |
| Size | ~500 MB |
| Beam size | 1 (greedy decoding) |
| VAD | Enabled (`min_silence_ms=800`) |
| Initial prompt | Scripture-flavored text biases toward KJV/NKJV vocabulary |
| Chunk window (live) | 8 seconds with 2s overlap |
| Processing ratio | ~4.2× realtime (batch) |

### Model 2: bge-small-en-v1.5 (Embeddings)

| Property | Value |
|----------|-------|
| Model | `BAAI/bge-small-en-v1.5` |
| Framework | sentence-transformers |
| Vector dimension | 384 |
| Size | ~130 MB |
| Query prefix | "Represent this sentence for searching relevant passages: " |
| Normalization | L2-normalized |
| Used for | FAISS index construction + all search queries |

### Model 3: cross-encoder/ms-marco-MiniLM-L-6-v2 (Re-Rank)

| Property | Value |
|----------|-------|
| Model | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| Size | ~90 MB |
| Usage | Optional, opt-in via `--cross-encoder` |
| Input | `(query_text, verse_text)` pairs |
| Output | Relevancy logits → sigmoid → min-max normalized |
| Blend weight | 30% of final score when active |

### Model 4: ONNX Quote Classifier (Trained In-House)

| Property | Value |
|----------|-------|
| Architecture | MiniLM/DistilBERT fine-tuned |
| Format | ONNX (runtime: onnxruntime) |
| Location | `onnx_model/model.onnx` + tokenizer |
| Training data | Synthetic sermon-ASR dataset (~20k rows) |
| Generation | `generate_dataset.py` via LLM (GLM API) |
| Training | `train_quote_classifier.py` |
| Evaluation | `evaluate_quote_classifier.py` |
| Export | `export_to_onnx.py` |

---

## Live Real-Time Pipeline

### Architecture

The live pipeline runs transcription in a background thread and routes results through the same Stage 2–6 modules as the batch pipeline, with an intent router as a new pre-processing step.

```
Background thread (audio processing):
  MicSource.stream() → LiveTranscriber → _on_new_segment(Segment)

Main thread / event loop:
  WebSocket broadcasts + REST endpoint handling
```

Thread-safety is achieved by:
- `asyncio.run_coroutine_threadsafe()` to push events from the audio thread onto the event loop
- `threading.Lock` on shared queue mutations
- Pipeline state (`SessionState`, `SermonContext`, `accepted_keys/books`) accessed only from the audio thread

### CUE Accumulator

A key live-mode optimization: when consecutive CUE_PHRASE windows arrive (e.g., a verse read across multiple transcription pauses), the accumulator joins their text for a single retrieval pass with the full quote text:

1. First CUE window meeting `rescue_min_words` (4) opens the accumulator
2. Subsequent CUE windows extend the accumulator text
3. A new trigger phrase (e.g., "the Bible says") closes the previous accumulator and opens a new one
4. Sentence-ending punctuation (`[.!?]"?'?\s*$`) closes the accumulator
5. Word cap (35 words) force-closes the accumulator
6. Non-CUE intents (EXPLICIT_REF, NAV, CLEAR) close the accumulator

### Query Variants (Multi-View Retrieval)

When the accumulator finalizes, the pipeline runs retrieval on multiple query variants and picks the highest-confidence result:

1. **Full accumulated text**
2. **Tail window** — last 20 words (focuses on the most recent stated verse)
3. **Clause-split variants** — each `.`/`!`/`?`-delimited clause as an independent query

**Cross-variant agreement bonus:** If the same reference appears from ≥2 variants, confidence gets a boost (+0.03 per agreeing variant, max +0.12).

### Live Rescue (Verbatim Recall)

The ONNX classifier is calibrated for cue-introduced scripture ("the Bible says…"), so bare verbatim quotes ("I will put my spirit within you…") may score too low. Live rescue is a live-only orchestration fallback:
- If ONNX score < threshold AND heuristic score ≥ 0.15 AND word count ≥ 4
- Trigger retrieval anyway
- This surfaces genuine verbatim reads that would otherwise be missed

### Three Operator Modes

| Mode | Behavior |
|------|----------|
| `autopilot` | Auto-presents `autopilot-eligible` detections to the live display without operator confirmation |
| `semi_autopilot` (default) | Broadcasts all detections; operator clicks "Present" to show on display |
| `manual` | Pauses transcription; operator types references manually; clears transcript/detection panels |

Mode switching is live: switching to `manual` pauses the transcription source and clears client-side panels; switching back restarts the mic capture.

---

## Intent Router & Session Management

### Routing Precedence

1. `EXPLICIT_REF` — highest priority (unambiguous structured reference)
2. `CLEAR_COMMAND` — second priority (usually short utterances, ≤8 words)
3. `NAV_COMMAND` — third priority (requires session anchor)
4. `CUE_PHRASE` — fallthrough (everything else)

### Session State

```python
@dataclass
class SessionState:
    current_reference: Optional[tuple[str, str, int, int]] = None
    # (translation_code, book, chapter, verse)
```

Updated by `note_display()` on every verse presentation (from any path). CLEAR never resets it — a "next verse" after "remove it" resolves from the last real verse shown.

### Book Resolution

`resolve_book()` handles 150+ alias variants:
- Ordinal prefixes: `first samuel` → `1 Samuel`, `ii kings` → `2 Kings`, `iii john` → `3 John`
- Abbreviations: `gen` → `Genesis`, `rev` → `Revelation`
- Punctuation-scrubbing fallback for noisy ASR input
- Whitespace-collapsed fallback

### Translation Resolution

`resolve_translation()` handles 25+ alias variants:
- Full names: `new king james version` → `NKJV`
- Colloquial: `the message` → `MSB`
- Direct codes: `ESV` → `ESV`

---

## Bible Data Layer

### Data Architecture

```
amplified.json ──────┐
                     ├── corpus.py ──→ FAISS index (AMP + NKJV, 62k verses, 384-dim)
nkjv.json ───────────┘                   ├── index/verses.faiss (92 MB)
                                         └── index/verses_meta.json (17 MB)

bible_all_versions.json ──→ bible_db.py ──→ BibleDB (7 translations, in-memory dict)
versions/*.json              (direct lookup, no embedding)
```

### Translation Coverage

| Translation | Code | In FAISS Index | In BibleDB |
|-------------|------|:---:|:---:|
| King James Version | KJV | | X |
| New International Version | NIV | | X |
| New King James Version | NKJV | X | X |
| New Living Translation | NLT | | X |
| Amplified Bible | AMP | X | X |
| English Standard Version | ESV | | X |
| The Message | MSB | | X |
| Luganda | — | | X (versions/) |
| Runyankole | — | | X (versions/) |
| Swahili | — | | X (versions/) |

Individual translation JSONs in `versions/` follow the same schema as the main files.

### Why Only 2 Translations in FAISS

- Embedding all 7 translations would quadruple the index size and build time
- The FAISS index is for **semantic nearest-neighbor search** — two translations provide enough signal for cross-translation concordance verification
- Direct lookups cover the remaining 5 translations for operator-presented verses
- The system was designed for English; African language translations are loaded for reference but not indexed

---

## Server, WebSocket & REST API

### Server Stack

| Component | Technology |
|-----------|-----------|
| Web framework | FastAPI |
| ASGI server | uvicorn |
| Real-time transport | WebSocket (Starlette) |
| Frontend | Static HTML/CSS/JS (`static/index.html`) |
| Port | localhost:8000 (default) |

### WebSocket Endpoint

**Path:** `ws://localhost:8000/ws`

**Behavior:** On connect, sends current queue, transcription source, and mode state. Then broadcasts events as they occur. Clients can send text (reserved for future operator commands).

#### Event Types

| Event Type | Fields | Trigger |
|------------|--------|---------|
| `transcript_update` | `{type, text, start_time, end_time}` | Every finalized transcription segment |
| `detection` | `{type, reference, translation, text, confidence, confidence_percent, confidence_band, transcript_snippet, timestamp}` | CUE path detection meeting confidence floor |
| `display_update` | `{type, reference, translation, text}` or `{type, clear: true}` | Display change (any source) |
| `queue_update` | `{type, queue: [...]}` | Queue mutation |
| `session_update` | `{type, translation, book, chapter, verse}` | Current verse anchor (Bible reader panel) |
| `source_update` | `{type, source: "whisper" \| "deepgram"}` | Transcription source change |
| `mode_update` | `{type, mode: "autopilot" \| "semi_autopilot" \| "manual"}` | Operator mode change |
| `transcript_clear` | `{type}` | Manual mode entry — clear transcript panel |
| `detections_clear` | `{type}` | Manual mode entry — clear detections panel |

### REST Endpoints

| Method | Path | Purpose | Request Body | Response |
|--------|------|---------|-------------|----------|
| `GET` | `/` | Serve operator console HTML | — | HTML page |
| `POST` | `/queue/add` | Add a verse to the operator queue | `{reference, translation?, text?, confidence?, confidence_band?}` | `{id, reference, translation, text, confidence, confidence_band}` |
| `POST` | `/present` | Show a verse on the live display | `{reference, translation?, text?}` | DisplayEvent as JSON |
| `DELETE` | `/queue/{item_id}` | Remove a queue item | — | `{removed, id}` |
| `POST` | `/queue/clear` | Clear the entire queue | — | `{cleared: true}` |
| `POST` | `/display/clear` | Clear the live display | — | `{cleared: true}` |
| `GET` | `/queue` | Get current queue state | — | `{queue: [...]}` |
| `GET` | `/health` | Health check | — | `{ok, audio, floor, source, mode}` |
| `POST` | `/transcription-source` | Switch transcription backend | `{source: "whisper" \| "deepgram"}` | `{source}` |
| `POST` | `/mode` | Switch operator mode | `{mode: "autopilot" \| "semi_autopilot" \| "manual"}` | `{mode}` |
| `GET` | `/search` | Vector search | `?q=<query>&top_k=<int>` | `{query, results: [...]}` |
| `GET` | `/bible/{translation}/{book}/{chapter}` | Bible chapter reader | — | `{translation, book, chapter, verses: [{verse, text}]}` |

### Operator Console (Frontend)

The `static/index.html` provides a 4-panel operator console:
1. **Live Transcript** — rolling captions from the transcription pipeline
2. **Detections** — color-coded list of detected scripture (confidence-banded)
3. **Live Display** — what the congregation sees (the active verse)
4. **Queue** — operator-curated list of verses for later presentation

---

## Configuration System

### Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `DEEPGRAM_API_KEY` | Deepgram cloud STT API key | (none) |
| `FAITHVIEW_OFFLINE` | Force local-files-only mode | `1` |
| `LIVE_CONFIDENCE_FLOOR` | Override live detection floor | `0.45` |
| `GLM_API_KEY` | LLM API key for synthetic data generation | (none) |

### Internal Offline Enforcers

Set in `models.py` at module load time:
- `HF_HUB_OFFLINE=1`
- `TRANSFORMERS_OFFLINE=1`
- `HF_DATASETS_OFFLINE=1`

### CLI Flags Summary

#### Batch (`run.py`)

| Flag | Default | Description |
|------|---------|-------------|
| `--audio` | — | Audio file path (wav/mp3) |
| `--transcript` | — | Pre-existing transcript JSON (skip Whisper) |
| `--out` | `results.json` | Output path |
| `--whisper-model` | `small.en` | Whisper model ID |
| `--quote-threshold` | `0.70` | Quote probability floor |
| `--top-k` | `10` | FAISS candidates per query |
| `--cross-encoder` | off | Enable cross-encoder re-rank |
| `--include-ignored` | off | Emit <0.60 entries |
| `--quote-detector` | `heuristic` | `heuristic`, `semantic`, `onnx` |
| `--offline` / `--online` | offline | Force offline/online |

#### Live Server (`server.py`)

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Port |
| `--audio` | `mic` | `mic`, `file`, `none` |
| `--file` | — | File path when `--audio file` |
| `--model` | `small.en` | Whisper model |
| `--chunk` | `8.0` | Chunk window seconds |
| `--overlap` | `2.0` | Overlap seconds |
| `--detector` | `onnx` | Quote detector |
| `--floor` | `0.45` | Live confidence floor |
| `--source` | `whisper` | Transcription source |
| `--mode` | `semi_autopilot` | Operator mode |

---

## Performance Profiling & Benchmarks

All measurements on CPU with the full 62k-verse FAISS index, `Whisper small.en` (int8), `bge-small-en-v1.5`.

### One-Time Setup Costs

| Operation | Time |
|-----------|------|
| Embedder model load | ~4.8 s |
| FAISS index + metadata load | ~315 ms |
| **Total cold-start** | **~5.1 s** |

### Per-Query Performance (Text-Only Search)

| Step | Time | % |
|------|------|---|
| Embedding (bge-small) | ~23.5 ms | 73% |
| FAISS search (62k × 384) | ~8.2 ms | 25% |
| Re-rank + score | ~0.4 ms | 2% |
| **Total per query** | **~32.2 ms** | 100% |

**Throughput: ~31 queries/second.**

### Full Audio Pipeline (60-second MP3)

| Stage | Time | % |
|-------|------|---|
| 1. Transcription (Whisper) | 13.89 s | 98.3% |
| 2. Buffering | 0.2 ms | 0.0% |
| 3. Quote detection | 206.3 ms | 1.5% |
| 4. Retrieval | 34.4 ms | 0.2% |
| 5+6. Re-rank + score | 0.3 ms | 0.0% |
| **Total (excl. model load)** | **14.13 s** | 100% |

**Processing ratio: 0.24× realtime** — 4.2 seconds of audio processed per second of CPU time. A 45-minute sermon takes ~10.8 minutes.

### Memory Footprint

| Component | Size | Notes |
|-----------|------|-------|
| Whisper small.en (int8) | ~500 MB | Loaded once, then freed |
| Embedder (bge-small) | ~130 MB | Singleton, stays resident |
| FAISS index (62k × 384) | 92 MB | Memory-mapped |
| Metadata JSON | 17 MB | Parsed into RAM |
| BibleDB (7 translations) | ~25 MB | In-memory hash tables |
| **Total runtime memory** | **~265 MB** | After Whisper freed |

### Scaling by Sermon Length

| Sermon Length | Transcription | Rest | Total |
|---------------|---------------|------|-------|
| 1 min | 13.9 s | 0.24 s | 14.1 s |
| 5 min | 69.5 s | ~1.2 s | 70.7 s |
| 15 min | 3.5 min | ~3.6 s | 3.6 min |
| 30 min | 7.0 min | ~7.2 s | 7.1 min |
| 45 min | 10.5 min | ~10.8 s | 10.7 min |
| 60 min | 14.0 min | ~14.4 s | 14.2 min |

Non-transcription stages scale linearly with the number of detected windows (~1–2 per minute), remaining under 15 seconds even for hour-long audio.

---

## Design Patterns & Extensibility

### Strategy Pattern (Swappable Components)

| Component | Interface | Implementations |
|-----------|-----------|-----------------|
| Quote Detector | `QuoteDetector.score(text) → float` | `HeuristicQuoteDetector`, `SemanticQuoteDetector`, `OnnxQuoteDetector` |
| Transcription Source | `TranscriptionSource.start/stop/name` | `WhisperTranscriptionSource`, `DeepgramSource` |
| Audio Source | `AudioSource.stream()` | `MicSource`, `FileSource` |
| Sermon Context | `SermonContext.score(candidate) → float` | Current: frequency tracker. Swap point for trained model |

### Singleton Pattern

- `Embedder` — one instance per process (heavy model, thread-safe loading)
- `BibleDB` — one instance per process (lazy-loaded, thread-safe)
- `CrossEncoder` — one instance per process (optional, lazy-loaded)

### Pipeline Pattern (Stages)

The batch pipeline is a linear sequence of independent stages:
1. Each stage accepts well-defined input types and produces well-defined output types
2. Stages can be unit-tested independently
3. Stages remain unchanged in the live pipeline (the router sits in front of Stage 3)

### Observer Pattern (Callbacks)

`LivePipeline` uses three callback functions for loose coupling:
- `on_transcript(Segment)` — every new transcribed sentence
- `on_detection(DetectionEvent)` — scripture detected via CUE path
- `on_display(DisplayEvent)` — display state change from any path

The `server.py` wires these to WebSocket broadcasts; the standalone CLI (`live_pipeline.py __main__`) wires them to console printing.

### Swap Points for Future Enhancement

| Component | What Changes | What Stays the Same |
|-----------|-------------|---------------------|
| Quote classifier | Implement `QuoteDetector.score` for a new ML model | Entire pipeline downstream |
| FAISS index type | Edit `retrieve.build_index()` only | `Retriever` interface + all callers |
| Embedding model | Change `config.EMBED_MODEL`, rebuild FAISS index | `Retriever` interface unchanged |
| Re-rank weights | Change `config.RerankWeights` dataclass | `rerank_candidates()` unchanged |
| Sermon context model | Replace `SermonContext` behind `.score()` API | Re-ranker unchanged |
| Transcription source | Implement `TranscriptionSource` interface | All downstream stages unchanged |
| Bible corpus | Replace JSON files, rebuild index | All loaders unchanged |
| UI frontend | Replace `static/index.html` | REST + WebSocket API unchanged |

---

## Entry Points Summary

### Production Entry Points

| Command | File | Purpose |
|---------|------|---------|
| `python run.py --audio sermon.mp3 --out results.json` | `run.py` | Batch offline pipeline |
| `python server.py` | `server.py` | Live operator console at localhost:8000 |
| `python live_pipeline.py` | `live_pipeline.py` | Standalone live pipeline (console output, no web UI) |
| `python search.py` | `search.py` | Interactive verse search CLI |
| `python search.py "I will put my spirit within you"` | `search.py` | One-shot verse search |

### Setup Entry Points (Run Once)

| Command | File | Purpose |
|---------|------|---------|
| `python download_models.py` | `download_models.py` | Download ML models from HuggingFace |
| `python download_models.py --cross-encoder` | `download_models.py` | Also download cross-encoder |
| `python build_index.py` | `build_index.py` | Build FAISS index from AMP + NKJV corpus |
| `python build_index.py --online` | `build_index.py` | Build index with network access for first-time embedder download |

### Development & Testing Entry Points

| Command | File | Purpose |
|---------|------|---------|
| `python test_pipeline.py` | `test_pipeline.py` | Unit tests (stages 2–6, stub embedder, no ML deps) |
| `python test_server_ws.py` | `test_server_ws.py` | WebSocket + REST smoke test |
| `python test_server_live_ws.py` | `test_server_live_ws.py` | Live server integration test |
| `python benchmark.py` | `benchmark.py` | Performance profiling |
| `python benchmark_live_chunk.py` | `benchmark_live_chunk.py` | Live chunk timing |
| `python batch_transcribe.py` | `batch_transcribe.py` | Batch-transcribe all audio in a directory |
| `python generate_dataset.py` | `generate_dataset.py` | Generate synthetic sermon-ASR training data |
| `python train_quote_classifier.py` | `train_quote_classifier.py` | Train ONNX quote classifier |
| `python evaluate_quote_classifier.py` | `evaluate_quote_classifier.py` | Evaluate trained classifier |
| `python export_to_onnx.py` | `export_to_onnx.py` | Export classifier to ONNX |
| `python bible_db.py` | `bible_db.py` | Smoketest BibleDB loading and lookups |
| `python intent_router.py` | `intent_router.py` | Self-test: 20+ intent routing cases |
