"""Central configuration for the FaithView Pro offline verse-detection engine.

All tunables live here so every pipeline stage can be unit-tested and later
swapped into the real-time operator console without hunting for magic numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- Load .env early so DEEPGRAM_API_KEY is available via os.environ ---------
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

# --- Paths -----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
AMP_PATH = os.path.join(HERE, "amplified.json")
NKJV_PATH = os.path.join(HERE, "nkjv.json")
INDEX_DIR = os.path.join(HERE, "index")
FAISS_INDEX_PATH = os.path.join(INDEX_DIR, "verses.faiss")
META_PATH = os.path.join(INDEX_DIR, "verses_meta.json")

# --- Models (downloaded once, then used fully offline) ---------------------
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
# bge-small-en-v1.5 expects this instruction prefix on *queries* only.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
WHISPER_MODEL = "small.en"       # faster-whisper model id; upgraded from "base" (see benchmark)
WHISPER_LANGUAGE = "en"          # set None for auto-detect
WHISPER_BEAM_SIZE = 1            # paired with small.en + widened 8s window per benchmark
# small.en beam=1 on 10s tile (8s + 2s overlap) = ~3.7s Whisper time measured,
# leaving ~4.3s for the rest of the pipeline (buffer → ONNX → FAISS → rerank → score).
# Window widened from 4s to 8s (10s tiles) to create that margin --
# without the widen, small.en alone at 4s would lag. See benchmark_live_chunk.py.
WHISPER_MIN_AVG_LOGPROB = -0.8   # per-segment min avg_logprob (skip below = likely hallucinated)
WHISPER_MAX_NO_SPEECH_PROB = 0.6 # per-segment max no_speech_prob (skip >= = likely silence)
WHISPER_VAD_FILTER = True
# Prime the decoder with scripture-flavored vocabulary so live ASR doesn't
# transliterate "thee/thou/behold" to "the/thou/behold-whatever". Whisper treats
# ``initial_prompt`` as the *previous context* of a hypothetical transcript, so a
# couple of representative sentences bias decoding toward KJV/NKJV diction without
# forcing any specific verse. Set to "" to disable. Loop-tunable from the CLI
# (``--initial-prompt`` on server.py / live_pipeline.py).
WHISPER_INITIAL_PROMPT = (
    "The Bible says, Behold, I will put my spirit within you, and ye shall live. "
    "Verily, verily, I say unto you, he that believeth on the Son hath everlasting "
    "life. The Lord is my shepherd; I shall not want. Beloved, let us love one "
    "another, for love is of God."
)
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# --- Offline behaviour -----------------------------------------------------
# When True, model loaders force local-files-only (no network). Default True so
# the pipeline never phones home once models are cached. Run download_models.py
# first to populate the cache. Override with FAITHVIEW_OFFLINE=0 to allow a
# first-time download from inside the pipeline.
OFFLINE = os.environ.get("FAITHVIEW_OFFLINE", "1") != "0"

# --- Stage 3: quote detection ---------------------------------------------
QUOTE_THRESHOLD = 0.40           # only windows >= this proceed to retrieval

# --- Stage 3: ONNX fallback classifier (hybrid / onnx detectors) ------------
# Trained DistilBERT quote classifier (train_quote_classifier.py ->
# export_to_onnx.py). The model's config declares id2label {0: negative,
# 1: positive}, so the positive (Scripture) class index is 1.
# Env overrides keep paths out of the code; ONNX Runtime is only imported
# lazily when an onnx/hybrid detector is actually constructed.
ONNX_MODEL_PATH = os.environ.get(
    "FAITHVIEW_ONNX_MODEL", os.path.join(HERE, "onnx_model", "model.onnx"))
ONNX_TOKENIZER_PATH = os.environ.get(
    "FAITHVIEW_ONNX_TOKENIZER", os.path.join(HERE, "onnx_model"))
ONNX_POSITIVE_INDEX = int(os.environ.get("FAITHVIEW_ONNX_POSITIVE_INDEX", "1"))

# --- Stage 4: semantic retrieval ------------------------------------------
TOP_K = 10                       # retrieve top 5-10 nearest verses
RETRIEVE_BATCH = 32              # query embedding batch size

# --- Stage 5: reranking weights (must sum to 1.0) -------------------------
# The ranking of retrieved Bible verse candidates uses ONLY the current
# sentence's evidence: semantic similarity to the retrieved verse (80%)
# and lexical keyword overlap (20%).  Previous sermon detections, running
# context, and Stage 3's quote probability must NOT influence which verse
# wins -- the current sentence alone determines the best match.
@dataclass(frozen=True)
class RerankWeights:
    semantic: float = 0.80       # cosine similarity from FAISS retrieval
    lexical: float = 0.20        # keyword / token overlap with the query
    context: float = 0.00        # DISABLED: sermon running context must not influence ranking
    history: float = 0.00        # DISABLED: previously matched verses must not influence ranking
    quote_prob: float = 0.00     # DISABLED: Stage 3 score must not influence Stage 5 ranking

RERANK_WEIGHTS = RerankWeights()
CROSS_ENCODER_TOPN = 10          # re-score only this many candidates
USE_CROSS_ENCODER = False        # opt-in; needs extra model download

# --- Stage 6: confidence banding ------------------------------------------
BAND_AUTOPILOT = "autopilot-eligible"   # >= AUTOPILOT_THRESHOLD
BAND_REVIEW = "review queue"            # >= REVIEW_THRESHOLD .. < AUTOPILOT_THRESHOLD
BAND_IGNORED = "ignored"                # < REVIEW_THRESHOLD
AUTOPILOT_THRESHOLD = 0.60
REVIEW_THRESHOLD = 0.40                 # was 0.80; lowered to surface more live detections

# --- Stage 2: sentence buffering ------------------------------------------
BUFFER_NEXT_WORDS = 12           # lookahead words merged into the current window

# --- Speech-boundary tuning (keeps long clause lists in ONE segment) ----------
# Whisper VAD: how much silence before faster-whisper splits a segment.
# Raising this keeps in-sentence pauses (breaths, comma gaps in list-style
# clauses like Romans 8:35) inside the same segment instead of fragmenting
# the retrieval input.  Lower = more responsive for short sentences but
# risks the original truncation bug. Tuned empirically: 700-900ms keeps
# list clauses whole while still closing out short sentences promptly.
WHISPER_VAD_MIN_SILENCE_MS = 800

# Deepgram endpointing: same knob, Deepgram's equivalent of VAD silence
# threshold.  Controls utterance_end_ms on the streaming connection.
DEEPGRAM_UTTERANCE_END_MS = 800

# --- Live transcription source (whisper offline or deepgram online) -----------
TRANSCRIPTION_SOURCE = "whisper"  # "whisper" | "deepgram"


@dataclass
class Settings:
    """Runtime settings, overridable from the CLI without mutating globals."""
    amp_path: str = AMP_PATH
    nkjv_path: str = NKJV_PATH
    index_dir: str = INDEX_DIR
    embed_model: str = EMBED_MODEL
    whisper_model: str = WHISPER_MODEL
    whisper_language: str | None = WHISPER_LANGUAGE
    whisper_beam_size: int = WHISPER_BEAM_SIZE
    whisper_vad_filter: bool = WHISPER_VAD_FILTER
    whisper_initial_prompt: str = WHISPER_INITIAL_PROMPT
    whisper_min_avg_logprob: float = WHISPER_MIN_AVG_LOGPROB
    whisper_max_no_speech_prob: float = WHISPER_MAX_NO_SPEECH_PROB
    quote_threshold: float = QUOTE_THRESHOLD
    top_k: int = TOP_K
    use_cross_encoder: bool = USE_CROSS_ENCODER
    offline: bool = OFFLINE
    weights: RerankWeights = field(default_factory=RerankWeights)
    transcription_source: str = TRANSCRIPTION_SOURCE
