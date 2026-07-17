"""Sermon context tracker for the re-ranker's "context match" signal.

For this batch prototype the context is a simple running tracker: the most
frequent books and keywords seen so far in the transcript (from accepted
verses). It does not need to be a trained LSTM yet -- the re-ranker talks to
``SermonContext.score`` so a richer model can drop in later.
"""

from __future__ import annotations

import re
from collections import Counter

from retrieve import Candidate

_STOP = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "for", "on", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "you", "your", "yours", "i", "we", "they", "them",
    "he", "she", "it", "his", "her", "its", "their", "our", "us", "my", "me",
    "that", "this", "these", "those", "as", "at", "by", "from", "up", "down",
    "out", "if", "then", "than", "so", "not", "no", "yes", "will", "shall",
    "would", "could", "should", "may", "might", "can", "all", "any", "every",
    "into", "upon", "over", "under", "also", "there", "here", "when", "where",
    "which", "who", "whom", "what", "why", "how", "because", "while", "about",
}


def keywords(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", (text or "").lower()) if w not in _STOP and len(w) > 2]


class SermonContext:
    """Running tracker of accepted books + keywords across the transcript."""

    def __init__(self, book_weight: float = 0.5, keyword_weight: float = 0.5):
        self.book_freq: Counter = Counter()
        self.kw_freq: Counter = Counter()
        self.book_weight = book_weight
        self.keyword_weight = keyword_weight

    def add(self, book: str, text: str) -> None:
        if book:
            self.book_freq[book] += 1
        for k in keywords(text):
            self.kw_freq[k] += 1

    def add_candidate(self, cand: Candidate, transcript_text: str = "") -> None:
        self.add(cand.book, cand.text)
        if transcript_text:
            for k in keywords(transcript_text):
                self.kw_freq[k] += 1

    def score(self, cand: Candidate) -> float:
        """Return [0,1] -- how well `cand` fits the running sermon context."""
        if not self.book_freq and not self.kw_freq:
            return 0.0
        # Book signal: 1.0 if this book has been seen, scaled by share of history.
        book_signal = 0.0
        if self.book_freq:
            total_b = sum(self.book_freq.values())
            book_signal = self.book_freq.get(cand.book, 0) / total_b if total_b else 0.0
            if cand.book in self.book_freq:
                book_signal = max(book_signal, 0.5)  # seen-this-book floor
        # Keyword signal: fraction of candidate keywords present in context.
        kw_signal = 0.0
        cand_kw = keywords(cand.text)
        if cand_kw and self.kw_freq:
            present = sum(1 for k in cand_kw if k in self.kw_freq)
            kw_signal = present / len(cand_kw)
        return self.book_weight * book_signal + self.keyword_weight * kw_signal

    def book_history(self) -> list[str]:
        return [b for b, _ in self.book_freq.most_common()]
