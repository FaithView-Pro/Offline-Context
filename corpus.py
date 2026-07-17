"""Corpus loader for the two-translation Bible index.

The on-disk schema (inspected, not assumed) is::

    {"version": "AMPLIFIED" | "NKJV",
     "books": [{"book": "Genesis",
                "chapters": [{"chapter": 1,
                              "verses": [{"verse": 1, "text": "..."}]}]}]}

This loader adapts to that shape and emits flat verse records tagged with the
source translation, so downstream stages never re-parse JSON.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Iterator


@dataclass(frozen=True)
class Verse:
    """One row of the retrieval corpus (one verse in one translation)."""
    translation: str          # "AMP" | "NKJV" (normalised short code)
    book: str                 # "Ezekiel"
    chapter: int
    verse: int
    text: str

    @property
    def reference(self) -> str:
        return f"{self.book} {self.chapter}:{self.verse}"

    @property
    def key(self) -> str:
        """Translation-agnostic reference key, e.g. 'Ezekiel 36:27'."""
        return f"{self.book} {self.chapter}:{self.verse}"

    def as_meta(self) -> dict:
        d = asdict(self)
        d["reference"] = self.reference
        d["key"] = self.key
        return d


_SHORT = {"AMPLIFIED": "AMP", "AMP": "AMP", "NKJV": "NKJV", "NEW KING JAMES VERSION": "NKJV"}


def _short_code(version: str) -> str:
    v = (version or "").strip().upper()
    return _SHORT.get(v, v or "UNK")


def load_translation(path: str, translation_code: str | None = None) -> list[Verse]:
    """Load one translation JSON file into a list of :class:`Verse` records."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Translation file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Tolerate either {"version","books"} or a bare list of books.
    if isinstance(data, dict):
        version_raw = data.get("version", translation_code or "")
        books = data.get("books", [])
    elif isinstance(data, list):
        version_raw = translation_code or ""
        books = data
    else:
        raise ValueError(f"Unexpected top-level type in {path}: {type(data).__name__}")

    code = _short_code(version_raw if version_raw else (translation_code or "UNK"))
    out: list[Verse] = []
    for book in books:
        bname = book.get("book") or book.get("name") or book.get("bookName")
        for chap in book.get("chapters", []):
            cnum = chap.get("chapter") or chap.get("chapter_number") or 0
            for v in chap.get("verses", []):
                vnum = v.get("verse") or v.get("verse_number") or 0
                text = (v.get("text") or v.get("verseText") or "").strip()
                if not text:
                    continue
                out.append(Verse(code, bname, int(cnum), int(vnum), text))
    return out


def load_corpus(amp_path: str, nkjv_path: str) -> list[Verse]:
    """Load both translations. This is the entire corpus for the prototype."""
    verses = load_translation(amp_path, "AMPLIFIED") + load_translation(nkjv_path, "NKJV")
    return verses


def iter_verses(verses: list[Verse]) -> Iterator[Verse]:
    return iter(verses)
