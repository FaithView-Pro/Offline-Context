"""Stage 4 -- SEMANTIC RETRIEVAL (BAAI/bge-small-en-v1.5 + FAISS).

The corpus is *exactly two* translations: amplified.json (AMP) and nkjv.json
(NKJV). One embedding per verse per translation is generated with
bge-small-en-v1.5, so the FAISS index holds two rows per reference (AMP + NKJV),
each tagged with its translation and reference.

Index type: ``faiss.IndexFlatIP`` over L2-normalised vectors => exact cosine
similarity. ~62k vectors x 384 dims is small enough for flat search on a CPU
(sub-millisecond per query). For a much larger corpus, swap in
``IndexHNSWFlat`` or ``IndexIVFFlat`` -- only ``build_index`` changes.

A match may come from either file; if the same reference scores highly in both
translations it legitimately appears twice in the candidate list (handled by
the re-ranker, not here).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class Candidate:
    reference: str          # "Ezekiel 36:27"
    translation: str        # "AMP" | "NKJV"
    book: str
    chapter: int
    verse: int
    text: str
    score: float            # raw cosine similarity from FAISS

    @property
    def key(self) -> str:
        return f"{self.book} {self.chapter}:{self.verse}"

    @classmethod
    def from_meta(cls, meta: dict, score: float) -> "Candidate":
        return cls(
            reference=meta.get("reference") or f"{meta['book']} {meta['chapter']}:{meta['verse']}",
            translation=meta["translation"],
            book=meta["book"],
            chapter=meta["chapter"],
            verse=meta["verse"],
            text=meta["text"],
            score=float(score),
        )

    def as_dict(self) -> dict:
        return {
            "reference": self.reference,
            "translation": self.translation,
            "score": round(self.score, 4),
        }


def _normalize(vecs):
    import numpy as np
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype("float32")


def build_index(verses, embedder, index_path: str = config.FAISS_INDEX_PATH,
                meta_path: str = config.META_PATH, batch_size: int = 64,
                show_progress: bool = True):
    """Embed every verse (both translations) and write FAISS index + metadata."""
    import faiss
    import numpy as np

    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    texts = [v.text for v in verses]
    print(f"[retrieve] embedding {len(texts)} verses with {embedder.model_name} ...")
    vecs = embedder.encode_corpus(texts, batch_size=batch_size, show_progress=show_progress)
    vecs = np.ascontiguousarray(_normalize(vecs), dtype="float32")

    dim = vecs.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(vecs)
    faiss.write_index(index, index_path)

    meta = [v.as_meta() for v in verses]
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)
    print(f"[retrieve] wrote index ({index.ntotal} x {dim}) -> {index_path}")
    print(f"[retrieve] wrote metadata ({len(meta)} rows) -> {meta_path}")
    return index, meta


class Retriever:
    """Loads a FAISS index + metadata and retrieves top-k candidates per query."""

    def __init__(self, index, meta: list[dict], embedder):
        self.index = index
        self.meta = meta
        self.embedder = embedder

    @classmethod
    def from_disk(cls, embedder, index_path: str = config.FAISS_INDEX_PATH,
                  meta_path: str = config.META_PATH) -> "Retriever":
        import faiss

        if not os.path.exists(index_path):
            raise FileNotFoundError(
                f"FAISS index not found at {index_path}. Run `python build_index.py` first.")
        if not os.path.exists(meta_path):
            raise FileNotFoundError(f"Metadata not found at {meta_path}.")
        index = faiss.read_index(index_path)
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        if index.ntotal != len(meta):
            print(f"[retrieve] WARNING: index has {index.ntotal} vectors but meta has {len(meta)} rows")
        return cls(index, meta, embedder)

    def search(self, query_texts: list[str], top_k: int = config.TOP_K,
               batch_size: int = config.RETRIEVE_BATCH) -> list[list[Candidate]]:
        """Return top_k candidates per query, blended across both translations."""
        if not query_texts:
            return []
        qv = self.embedder.encode_queries(query_texts, batch_size=batch_size)
        qv = _normalize(qv)
        D, I = self.index.search(qv, top_k)
        results: list[list[Candidate]] = []
        for qi in range(len(query_texts)):
            cands: list[Candidate] = []
            for score, idx in zip(D[qi], I[qi]):
                if idx < 0:
                    continue
                cands.append(Candidate.from_meta(self.meta[int(idx)], float(score)))
            results.append(cands)
        return results

    def search_one(self, query_text: str, top_k: int = config.TOP_K) -> list[Candidate]:
        return self.search([query_text], top_k=top_k)[0]
