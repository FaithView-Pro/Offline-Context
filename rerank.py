"""Stage 5 -- RE-RANKING.

Re-score retrieved Bible verse candidates using ONLY the current sentence's
evidence:

    80% semantic similarity   (cosine from FAISS retrieval)
    20% keyword/lexical overlap

Previous sermon detections, running context, and Stage 3's quote-detection
probability do NOT influence candidate ranking -- the current sentence alone
determines which verse wins.

When the same reference appears from both translations, the higher-scoring
translation is kept as the *representative* (selected) candidate, but the other
is retained in the raw candidate list for debugging.

Optionally, the Top N are re-scored with a cross-encoder
(``cross-encoder/ms-marco-MiniLM-L-6-v2``) and blended in for an extra accuracy
boost. This is opt-in (needs an extra model download) and degrades gracefully.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import config
from context import SermonContext, keywords
from retrieve import Candidate


@dataclass
class RankedCandidate:
    candidate: Candidate
    final: float
    semantic: float
    lexical: float
    context: float
    history: float
    quote_prob: float
    cross_encoder: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "reference": self.candidate.reference,
            "translation": self.candidate.translation,
            "score": round(self.final, 4),
        }


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / max(1, min(len(a), len(b)))


def lexical_score(query_text: str, cand: Candidate) -> float:
    """Keyword/token overlap between the sermon window and the verse text."""
    q = set(keywords(query_text))
    c = set(keywords(cand.text))
    base = 0.5 * _jaccard(q, c) + 0.5 * _overlap(q, c)
    # Bonus when the preacher explicitly names the book / reference.
    low = (query_text or "").lower()
    ref_bonus = 0.0
    if cand.book and cand.book.lower() in low:
        ref_bonus += 0.10
    if f"{cand.chapter}:{cand.verse}" in re.sub(r"\s", "", low):
        ref_bonus += 0.10
    return max(0.0, min(1.0, base + ref_bonus))


def history_score(cand: Candidate, accepted_keys: set[str], accepted_books: dict) -> float:
    """Match against previously accepted verses in this transcript."""
    if cand.key in accepted_keys:
        return 1.0
    if cand.book in accepted_books:
        return 0.5
    return 0.0


def _sigmoid(x: float) -> float:
    import math
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _cross_encoder_scores(ce, query: str, cands: list[Candidate]) -> list[float]:
    import numpy as np
    pairs = [(query, c.text) for c in cands]
    raw = ce.predict(pairs)
    raw = np.asarray(raw, dtype="float32").reshape(-1)
    sig = np.array([_sigmoid(float(x)) for x in raw])
    lo, hi = float(sig.min()), float(sig.max())
    if hi - lo < 1e-6:
        return [0.5] * len(cands)
    return [float((v - lo) / (hi - lo)) for v in sig]


def rerank_candidates(
    candidates: list[Candidate],
    query_text: str,
    quote_prob: float,
    context: SermonContext,
    accepted_keys: set[str],
    accepted_books: dict,
    weights: config.RerankWeights = config.RERANK_WEIGHTS,
    cross_encoder=None,
    ce_topn: int = config.CROSS_ENCODER_TOPN,
) -> list[RankedCandidate]:
    """Re-rank candidates by the semantic + lexical blend.

    Returns list sorted by final score descending.  The ``context``,
    ``accepted_keys`` and ``accepted_books`` parameters are accepted for
    backward compatibility but have zero weight in the default configuration;
    they do NOT influence ranking.
    """
    ranked: list[RankedCandidate] = []
    for c in candidates:
        sem = max(0.0, min(1.0, c.score))           # cosine ~ [0,1] for related text
        lex = lexical_score(query_text, c)
        # Context / history / quote_prob are zero-weighted by default.  Skip
        # the (potentially expensive) context.score() call when not needed.
        ctx = context.score(c) if weights.context else 0.0
        hist = history_score(c, accepted_keys, accepted_books) if weights.history else 0.0
        final = (
            weights.semantic * sem
            + weights.lexical * lex
            + weights.context * ctx
            + weights.history * hist
            + weights.quote_prob * quote_prob
        )
        ranked.append(RankedCandidate(c, max(0.0, min(1.0, final)), sem, lex, ctx, hist, quote_prob))

    ranked.sort(key=lambda r: r.final, reverse=True)

    # Optional cross-encoder boost on the top N.
    if cross_encoder is not None and ranked:
        top = ranked[:ce_topn]
        ce_vals = _cross_encoder_scores(cross_encoder, query_text, [r.candidate for r in top])
        ce_weight = 0.30
        for r, cev in zip(top, ce_vals):
            r.cross_encoder = cev
            r.final = max(0.0, min(1.0, (1.0 - ce_weight) * r.final + ce_weight * cev))
        ranked.sort(key=lambda r: r.final, reverse=True)

    return ranked


def select_representative(ranked: list[RankedCandidate]) -> tuple[Optional[RankedCandidate], list[RankedCandidate]]:
    """Pick the top candidate as representative; return (selected, all_ranked).

    The same reference may appear twice (AMP + NKJV); the higher-scoring one
    becomes the representative, the other stays in the raw list for debugging.
    """
    if not ranked:
        return None, []
    return ranked[0], ranked
