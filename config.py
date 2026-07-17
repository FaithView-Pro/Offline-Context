"""Central configuration for the FaithView Pro offline verse-detection engine.

All tunables live here so every pipeline stage can be unit-tested and later
swapped into the real-time operator console without hunting for magic numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
WHISPER_MODEL = "small"          # faster-whisper model id; use "small.en" for English-only
WHISPER_LANGUAGE = "en"          # set None for auto-detect
WHISPER_BEAM_SIZE = 5
WHISPER_VAD_FILTER = True
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# --- Offline behaviour -----------------------------------------------------
# When True, model loaders force local-files-only (no network). Default True so
# the pipeline never phones home once models are cached. Run download_models.py
# first to populate the cache. Override with FAITHVIEW_OFFLINE=0 to allow a
# first-time download from inside the pipeline.
OFFLINE = os.environ.get("FAITHVIEW_OFFLINE", "1") != "0"

# --- Stage 3: quote detection ---------------------------------------------
QUOTE_THRESHOLD = 0.70           # only windows >= this proceed to retrieval

# --- Stage 4: semantic retrieval ------------------------------------------
TOP_K = 10                       # retrieve top 5-10 nearest verses
RETRIEVE_BATCH = 32              # query embedding batch size

# --- Stage 5: hybrid re-ranking weights (must sum to 1.0) ------------------
@dataclass(frozen=True)
class RerankWeights:
    semantic: float = 0.45       # cosine similarity from FAISS
    lexical: float = 0.20        # keyword / token overlap
    context: float = 0.15        # sermon context match (running tracker)
    history: float = 0.10        # match against previously accepted verses
    quote_prob: float = 0.10     # quote-detection probability

RERANK_WEIGHTS = RerankWeights()
CROSS_ENCODER_TOPN = 10          # re-score only this many candidates
USE_CROSS_ENCODER = False        # opt-in; needs extra model download

# --- Stage 6: confidence banding ------------------------------------------
BAND_AUTOPILOT = "autopilot-eligible"   # >= 0.96
BAND_REVIEW = "review queue"            # 0.80 .. <0.96
BAND_IGNORED = "ignored"                # < 0.80
AUTOPILOT_THRESHOLD = 0.96
REVIEW_THRESHOLD = 0.80

# --- Stage 2: sentence buffering ------------------------------------------
BUFFER_NEXT_WORDS = 12           # lookahead words merged into the current window


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
    quote_threshold: float = QUOTE_THRESHOLD
    top_k: int = TOP_K
    use_cross_encoder: bool = USE_CROSS_ENCODER
    offline: bool = OFFLINE
    weights: RerankWeights = field(default_factory=RerankWeights)
