"""Stage 6 -- CONFIDENCE OUTPUT (offline decision engine).

Pure local scoring -- no cloud API. Takes the re-ranked candidates for a
sentence window and produces the final confidence + band:

    >= 0.96  -> "autopilot-eligible"
    >= 0.80  -> "review queue"
    <  0.80  -> "ignored"

Confidence is the selected candidate's hybrid re-rank score (it already fuses
semantic, lexical, context, history, and quote-probability). This module is a
set of pure functions so it can be unit-tested in isolation.
"""

from __future__ import annotations

from typing import Optional

import config
from buffer import Window
from rerank import RankedCandidate


def confidence_for(selected: RankedCandidate) -> float:
    """Final offline decision score in [0, 1]."""
    return max(0.0, min(1.0, selected.final))


def band(confidence: float) -> str:
    if confidence >= config.AUTOPILOT_THRESHOLD:
        return config.BAND_AUTOPILOT
    if confidence >= config.REVIEW_THRESHOLD:
        return config.BAND_REVIEW
    return config.BAND_IGNORED


def build_entry(
    window: Window,
    quote_prob: float,
    selected: RankedCandidate,
    ranked: list[RankedCandidate],
    max_candidates: int = 10,
) -> dict:
    """Build one output record in the spec format."""
    conf = confidence_for(selected)
    return {
        "start_time": round(window.start_time, 2),
        "end_time": round(window.end_time, 2),
        "transcript": window.candidate_text,
        "quote_probability": round(quote_prob, 4),
        "selected_reference": selected.candidate.reference,
        "translation": selected.candidate.translation,
        "confidence": round(conf, 4),
        "confidence_band": band(conf),
        "candidates": [r.as_dict() for r in ranked[:max_candidates]],
    }


def is_surfaced(entry: dict, include_ignored: bool = False) -> bool:
    """Should this entry appear in the final JSON output?"""
    if include_ignored:
        return True
    return entry["confidence_band"] != config.BAND_IGNORED
