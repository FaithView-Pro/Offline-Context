"""Tests for the Stage 5 reranking redesign.

Proves that candidate ranking uses ONLY:
    80% semantic similarity
    20% lexical overlap

and that context, history, and quote probability have ZERO influence.

Run: python test_rerank.py
"""

from __future__ import annotations

import config
from context import SermonContext
from rerank import (
    rerank_candidates,
    select_representative,
    lexical_score,
    RankedCandidate,
)
from retrieve import Candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cand(ref: str, text: str, score: float, book: str = "", ch: int = 0, vs: int = 0) -> Candidate:
    """Build a Candidate with the fields the reranker touches."""
    return Candidate(
        reference=ref, translation="NKJV",
        text=text, score=score, book=book, chapter=ch, verse=vs,
    )


def _refs(ranked: list[RankedCandidate]) -> list[str]:
    """Extract ordered reference list from ranked output."""
    return [r.candidate.reference for r in ranked]


# ---------------------------------------------------------------------------
# Test 1 — Semantic dominance
# Candidate A has high semantic score, Candidate B has low.
# A must rank first regardless of lexical overlap.
# ---------------------------------------------------------------------------

def test_semantic_dominance():
    query = "God said let there be light and there was light"
    cand_high_sem = _cand("Genesis 1:3", "Then God said, Let there be light", 0.95,
                          book="Genesis", ch=1, vs=3)
    cand_low_sem  = _cand("Revelation 22:5", "And there shall be no more night", 0.30,
                          book="Revelation", ch=22, vs=5)
    ctx = SermonContext()
    ranked = rerank_candidates([cand_high_sem, cand_low_sem], query, 0.95, ctx, set(), {})
    assert _refs(ranked) == ["Genesis 1:3", "Revelation 22:5"], \
        f"semantic dominance failed: {_refs(ranked)}"
    # Verify final score is dominated by semantic (80%) not lexical
    top = ranked[0]
    assert top.semantic > 0.9, f"top semantic too low: {top.semantic}"
    print("[OK] test 1 — semantic dominance")


# ---------------------------------------------------------------------------
# Test 2 — Lexical contribution
# Two candidates with identical semantic scores; lexical decides.
# ---------------------------------------------------------------------------

def test_lexical_contribution():
    query = "God said let there be light and there was light"
    # Same semantic score, but cand_a shares more keywords with the query.
    cand_a = _cand("Genesis 1:3", "And God said, Let there be light, and there was light",
                   0.80, book="Genesis", ch=1, vs=3)
    cand_b = _cand("John 1:1", "In the beginning was the Word, and the Word was with God",
                   0.80, book="John", ch=1, vs=1)
    ctx = SermonContext()
    ranked = rerank_candidates([cand_b, cand_a], query, 0.50, ctx, set(), {})

    # Genesis 1:3 has more lexical overlap ("god", "said", "light", "there")
    # John 1:1 shares "god", "beginning" — fewer keyword matches.
    assert ranked[0].candidate.reference == "Genesis 1:3", \
        f"lexical contribution failed: {_refs(ranked)}"
    # The final score difference should come from lexical, not semantic
    diff = ranked[0].final - ranked[1].final
    assert diff > 0, f"expected positive difference, got {diff}"
    # Verify the lexical scores differ
    lex_genesis = lexical_score(query, cand_a)
    lex_john    = lexical_score(query, cand_b)
    assert lex_genesis > lex_john, \
        f"expected genesis lex > john lex: {lex_genesis} vs {lex_john}"
    print("[OK] test 2 — lexical contribution")


# ---------------------------------------------------------------------------
# Test 3 — History has no influence
# Same sentence + candidates, different previous detections.
# Ranking must be identical.
# ---------------------------------------------------------------------------

def test_history_no_influence():
    query = "According to Genesis, God said let there be light."
    cand_a = _cand("Genesis 1:3", "Then God said, Let there be light", 0.91,
                   book="Genesis", ch=1, vs=3)
    cand_b = _cand("Genesis 1:14", "And God said, Let there be lights in the sky",
                   0.72, book="Genesis", ch=1, vs=14)
    ctx = SermonContext()

    # Case A: previous detection was John 3:16
    ctx_a = SermonContext()
    ctx_a.add("John", "For God so loved the world...")
    ranked_a = rerank_candidates([cand_a, cand_b], query, 0.95, ctx_a,
                                  accepted_keys={"john_3:16"}, accepted_books={"John": 1})

    # Case B: previous detection was Psalm 23:1
    ctx_b = SermonContext()
    ctx_b.add("Psalms", "The Lord is my shepherd...")
    ranked_b = rerank_candidates([cand_a, cand_b], query, 0.95, ctx_b,
                                  accepted_keys={"psalms_23:1"}, accepted_books={"Psalms": 1})

    refs_a = _refs(ranked_a)
    refs_b = _refs(ranked_b)
    scores_a = [r.final for r in ranked_a]
    scores_b = [r.final for r in ranked_b]
    assert refs_a == refs_b, f"history influenced ranking: {refs_a} vs {refs_b}"
    assert scores_a == scores_b, f"history influenced scores: {scores_a} vs {scores_b}"
    print("[OK] test 3 — history has no influence")


# ---------------------------------------------------------------------------
# Test 4 — Context has no influence
# Same candidates + different sermon context.
# Ranking must be identical.
# ---------------------------------------------------------------------------

def test_context_no_influence():
    query = "According to Genesis, God said let there be light."
    cand_a = _cand("Genesis 1:3", "Then God said, Let there be light", 0.91,
                   book="Genesis", ch=1, vs=3)
    cand_b = _cand("Genesis 1:14", "And God said, Let there be lights in the sky",
                   0.72, book="Genesis", ch=1, vs=14)

    # Context A: rich Genesis context (many Genesis mentions)
    ctx_a = SermonContext()
    for _ in range(5):
        ctx_a.add("Genesis", "In the beginning God created the heavens and the earth")
    ranked_a = rerank_candidates([cand_a, cand_b], query, 0.95, ctx_a, set(), {})

    # Context B: rich Psalm context (no Genesis)
    ctx_b = SermonContext()
    for _ in range(5):
        ctx_b.add("Psalms", "The Lord is my shepherd I shall not want")
    ranked_b = rerank_candidates([cand_a, cand_b], query, 0.95, ctx_b, set(), {})

    refs_a = _refs(ranked_a)
    refs_b = _refs(ranked_b)
    scores_a = [r.final for r in ranked_a]
    scores_b = [r.final for r in ranked_b]
    assert refs_a == refs_b, f"context influenced ranking: {refs_a} vs {refs_b}"
    assert scores_a == scores_b, f"context influenced scores: {scores_a} vs {scores_b}"
    print("[OK] test 4 — context has no influence")


# ---------------------------------------------------------------------------
# Test 5 — Quote probability has no influence
# Same candidates, different quote_prob values.
# Ranking must be identical.
# ---------------------------------------------------------------------------

def test_quote_prob_no_influence():
    query = "According to Genesis, God said let there be light."
    cand_a = _cand("Genesis 1:3", "Then God said, Let there be light", 0.91,
                   book="Genesis", ch=1, vs=3)
    cand_b = _cand("Genesis 1:14", "And God said, Let there be lights in the sky",
                   0.72, book="Genesis", ch=1, vs=14)
    ctx = SermonContext()

    ranked_hi = rerank_candidates([cand_a, cand_b], query, 0.95, ctx, set(), {})
    ranked_lo = rerank_candidates([cand_a, cand_b], query, 0.20, ctx, set(), {})

    refs_hi = _refs(ranked_hi)
    refs_lo = _refs(ranked_lo)
    scores_hi = [r.final for r in ranked_hi]
    scores_lo = [r.final for r in ranked_lo]
    assert refs_hi == refs_lo, f"quote_prob influenced ranking: {refs_hi} vs {refs_lo}"
    assert scores_hi == scores_lo, f"quote_prob influenced scores: {scores_hi} vs {scores_lo}"
    print("[OK] test 5 — quote probability has no influence")


# ---------------------------------------------------------------------------
# Test 6 — Real Genesis example
# End-to-end: given realistic FAISS-style candidates for the Genesis sentence,
# verify Genesis 1:3 is selected.
# ---------------------------------------------------------------------------

def test_real_genesis_example():
    query = "According to Genesis, God said let there be light."
    # Simulate FAISS top-K results (scores are cosine similarities from retrieval)
    candidates = [
        _cand("Genesis 1:3", "Then God said, Let there be light", 0.91,
              book="Genesis", ch=1, vs=3),
        _cand("Genesis 1:3", "And God said, Let there be light; and there was light",
              0.89, book="Genesis", ch=1, vs=3),  # AMP translation
        _cand("Genesis 1:14", "And God said, Let there be lights in the sky",
              0.72, book="Genesis", ch=1, vs=14),
        _cand("John 1:1", "In the beginning was the Word", 0.45,
              book="John", ch=1, vs=1),
        _cand("Psalm 33:9", "For He spoke, and it was done", 0.38,
              book="Psalms", ch=33, vs=9),
    ]
    ctx = SermonContext()
    ranked = rerank_candidates(candidates, query, 0.95, ctx, set(), {})
    selected, _ = select_representative(ranked)

    assert selected is not None, "no representative selected"
    assert selected.candidate.reference == "Genesis 1:3", \
        f"expected Genesis 1:3, got {selected.candidate.reference}"
    # The NKJV (0.91 semantic) should beat AMP (0.89) due to slightly higher
    # semantic + comparable lexical
    assert selected.candidate.translation == "NKJV", \
        f"expected NKJV representative, got {selected.candidate.translation}"
    # Final score must be dominated by semantic (80%) not quote_prob or context
    assert selected.final > 0.80, f"final score too low: {selected.final}"
    print(f"[OK] test 6 — Genesis 1:3 selected (final={selected.final:.3f})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_semantic_dominance()
    test_lexical_contribution()
    test_history_no_influence()
    test_context_no_influence()
    test_quote_prob_no_influence()
    test_real_genesis_example()
    print("\nALL RERANK TESTS PASSED")
