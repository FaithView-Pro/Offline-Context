"""Stage 2 -- SENTENCE BUFFERING.

Group Whisper's raw segments into a rolling sentence window so downstream
stages judge each candidate with real context instead of a bare fragment::

    window = previous sentence + current sentence + next few words

The *candidate* is always the current sentence; its timestamp is the sentence
span. The surrounding context is carried along for scoring only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import config
from transcribe import Segment

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Sentence:
    text: str
    start_time: float
    end_time: float
    index: int


@dataclass
class Window:
    index: int
    text: str               # full context (prev + current + next words)
    candidate_text: str     # just the current sentence (the thing being scored)
    start_time: float       # candidate span start
    end_time: float         # candidate span end
    prev_text: str = ""
    next_text: str = ""

    def as_dict(self) -> dict:
        return {"text": self.text, "start_time": self.start_time, "end_time": self.end_time}


def _split_segment_text(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def split_sentences(segments: list[Segment]) -> list[Sentence]:
    """Break segments into timestamped sentences.

    Uses word timestamps when available (precise spans), otherwise falls back
    to per-segment span approximation (Whisper segments are short, so this is
    acceptable for a batch POC).
    """
    sentences: list[Sentence] = []

    # Precise path: walk a flattened word list and close sentences on .!?
    flat_words = [w for seg in segments if seg.words for w in seg.words]
    if flat_words:
        cur_words: list = []
        for w in flat_words:
            cur_words.append(w)
            if re.search(r"[.!?]['\"\']?$", w.text):
                text = " ".join(x.text for x in cur_words).strip()
                if text:
                    sentences.append(Sentence(text, cur_words[0].start_time, cur_words[-1].end_time, len(sentences)))
                cur_words = []
        if cur_words:
            text = " ".join(x.text for x in cur_words).strip()
            if text:
                sentences.append(Sentence(text, cur_words[0].start_time, cur_words[-1].end_time, len(sentences)))
        return sentences

    # Approximate path: split each segment by sentence punctuation.
    for seg in segments:
        for chunk in _split_segment_text(seg.text):
            sentences.append(Sentence(chunk, seg.start_time, seg.end_time, len(sentences)))
    return sentences


def _first_n_words(text: str, n: int) -> str:
    if n <= 0 or not text:
        return ""
    words = text.split()
    return " ".join(words[:n])


def build_windows(
    sentences: list[Sentence],
    next_words: int = config.BUFFER_NEXT_WORDS,
) -> list[Window]:
    """Build a rolling window per sentence: prev + current + next N words."""
    windows: list[Window] = []
    n = len(sentences)
    for i, s in enumerate(sentences):
        prev_text = sentences[i - 1].text if i > 0 else ""

        # Collect up to `next_words` words from following sentences.
        next_text = ""
        if next_words > 0:
            collected: list[str] = []
            j = i + 1
            while j < n and len(collected) < next_words:
                for w in sentences[j].text.split():
                    collected.append(w)
                    if len(collected) >= next_words:
                        break
                j += 1
            next_text = " ".join(collected[:next_words])

        full = " ".join(p for p in (prev_text, s.text, next_text) if p)
        windows.append(Window(
            index=i,
            text=full,
            candidate_text=s.text,
            start_time=s.start_time,
            end_time=s.end_time,
            prev_text=prev_text,
            next_text=next_text,
        ))
    return windows


def buffer_segments(
    segments: list[Segment],
    next_words: int = config.BUFFER_NEXT_WORDS,
) -> list[Window]:
    """Convenience: segments -> windows in one call."""
    return build_windows(split_sentences(segments), next_words=next_words)
