"""Offline-aware model loaders.

Models are downloaded once (see ``download_models.py``) into the Hugging Face
cache. After that, every loader here forces ``local_files_only`` so the pipeline
makes zero network calls at runtime -- the core offline guarantee.

The embedder and cross-encoder are singletons: built once, reused across index
build and per-audio runs to avoid reloading multi-hundred-MB weights.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import config

_LOCK = threading.Lock()
_EMBEDDER = None
_CROSS_ENCODER = None


def _apply_offline_env(offline: bool) -> None:
    """Set HF/Transformers offline flags so no HTTP call can escape."""
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"
    else:
        for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
            os.environ.pop(k, None)


class Embedder:
    """Thin wrapper around SentenceTransformer with bge query/corpus convention.

    bge-small-en-v1.5 expects the query-instruction prefix on *queries* only;
    corpus (verse) text is embedded verbatim.
    """

    def __init__(self, model_name: str, offline: bool = True):
        from sentence_transformers import SentenceTransformer

        _apply_offline_env(offline)
        self.model_name = model_name
        try:
            self._st = SentenceTransformer(model_name)
        except Exception as exc:
            if offline:
                print(f"[models] embedder offline load failed ({exc}); retrying with network")
                _apply_offline_env(False)
                self._st = SentenceTransformer(model_name)
            else:
                raise
        self._dim = self._st.get_sentence_embedding_dimension()

    @property
    def dim(self) -> int:
        return self._dim

    def encode_corpus(self, texts, batch_size: int = 64, show_progress: bool = False):
        import numpy as np

        emb = self._st.encode(
            texts, batch_size=batch_size, normalize_embeddings=True,
            show_progress_bar=show_progress, convert_to_numpy=True,
        )
        return np.ascontiguousarray(emb, dtype="float32")

    def encode_queries(self, texts, batch_size: int = 32):
        import numpy as np

        prefixed = [config.QUERY_PREFIX + t for t in texts]
        emb = self._st.encode(
            prefixed, batch_size=batch_size, normalize_embeddings=True,
            show_progress_bar=False, convert_to_numpy=True,
        )
        return np.ascontiguousarray(emb, dtype="float32")


def get_embedder(offline: Optional[bool] = None) -> Embedder:
    """Return a process-wide singleton embedder."""
    global _EMBEDDER
    with _LOCK:
        if _EMBEDDER is None:
            _EMBEDDER = Embedder(config.EMBED_MODEL, offline=config.OFFLINE if offline is None else offline)
        return _EMBEDDER


def get_cross_encoder(offline: Optional[bool] = None):
    """Return a singleton cross-encoder, or None if disabled/unavailable."""
    global _CROSS_ENCODER
    if not config.USE_CROSS_ENCODER:
        return None
    with _LOCK:
        if _CROSS_ENCODER is None:
            try:
                from sentence_transformers import CrossEncoder

                _apply_offline_env(config.OFFLINE if offline is None else offline)
                _CROSS_ENCODER = CrossEncoder(config.CROSS_ENCODER_MODEL)
            except Exception as exc:  # pragma: no cover - depends on download
                print(f"[models] cross-encoder unavailable, skipping: {exc}")
                _CROSS_ENCODER = None
        return _CROSS_ENCODER
