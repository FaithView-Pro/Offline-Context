"""Direct-lookup Bible database over ALL versions in ``bible_all_versions.json``.

The offline batch engine (``corpus.py`` + FAISS index) only embeds AMP + NKJV
because that index is what semantic retrieval (Stage 4) searches. But the live
operator console also needs the *other* versions -- KJV, NIV, NLT, ESV, MSB --
for two reasons that the batch engine never had:

  * EXPLICIT_REF: a preacher says "Romans 1:16" -> direct (book, chapter,
    verse) lookup, no search. We should answer from any translation the
    operator requests, not just the two in the FAISS index.
  * NAV_COMMAND translation-swap: "give me that in the Amplified" / "in the
    ESV" -> same reference, different translation. Only a direct lookup can
    return versions that were never embedded.

This module loads ``bible_all_versions.json`` once into an in-memory dict keyed
by ``(translation, book, chapter, verse)`` plus a reverse index
``(book, chapter, verse) -> {translation: text}``. It is deliberately separate
from ``corpus.py`` (which we do not modify): ``corpus.py`` returns ``Verse``
objects for the FAISS-indexed translations only; this is a plain-text lookup
table for the full 7-version set used by the live router.

On-disk schema (inspected from bible_all_versions.json)::

    {"versions": ["KJV", "NIV", "NKJV", "NLT", "AMPLIFIED", "ESV", "MSB"],
     "books": [{"book": "Genesis",
                "chapters": [{"chapter": 1,
                              "verses": [{"verse": 1,
                                          "text": {"KJV": "...", ...}}]}]}]}

Translation codes are normalised to match ``corpus.py``'s convention
("AMPLIFIED" -> "AMP") so the live app speaks one language end to end.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Optional


HERE = os.path.dirname(os.path.abspath(__file__))
# Check project root first, then versions/ subdirectory
_candidate = os.path.join(HERE, "bible_all_versions.json")
if os.path.exists(_candidate):
    ALL_VERSIONS_PATH = _candidate
else:
    ALL_VERSIONS_PATH = os.path.join(HERE, "versions", "bible_all_versions.json")

# Canonical 66-book order, exactly as used in amplified.json / nkjv.json /
# bible_all_versions.json so direct lookups line up with the FAISS metadata.
CANONICAL_BOOKS = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua",
    "Judges", "Ruth", "1 Samuel", "2 Samuel", "1 Kings", "2 Kings",
    "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah", "Esther", "Job",
    "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon", "Isaiah",
    "Jeremiah", "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos",
    "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah", "Haggai",
    "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John", "Acts",
    "Romans", "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians",
    "Philippians", "Colossians", "1 Thessalonians", "2 Thessalonians",
    "1 Timothy", "2 Timothy", "Titus", "Philemon", "Hebrews", "James",
    "1 Peter", "2 Peter", "1 John", "2 John", "3 John", "Jude", "Revelation",
]

# Normalised short codes. "AMPLIFIED" -> "AMP" to match corpus.py.
_VERSION_CODE = {
    "KJV": "KJV", "NIV": "NIV", "NKJV": "NKJV", "NLT": "NLT",
    "AMPLIFIED": "AMP", "AMP": "AMP", "ESV": "ESV", "MSB": "MSB",
}

# Spoken / written translation aliases -> short code. Used by the router to
# resolve NAV translation-swap commands ("give me that in the Amplified").
VERSION_ALIASES = {
    "amp": "AMP", "amplified": "AMP", "amplified bible": "AMP",
    "amplified version": "AMP", "the amplified": "AMP",
    "nkjv": "NKJV", "new king james": "NKJV",
    "new king james version": "NKJV", "new king james bible": "NKJV",
    "kjv": "KJV", "king james": "KJV", "king james version": "KJV",
    "king james bible": "KJV", "authorized version": "KJV", "authorized": "KJV",
    "niv": "NIV", "new international version": "NIV",
    "new international": "NIV",
    "nlt": "NLT", "new living translation": "NLT", "new living": "NLT",
    "esv": "ESV", "english standard version": "ESV",
    "english standard": "ESV",
    "msb": "MSB", "message": "MSB", "the message": "MSB",
    "the message bible": "MSB",
}

# Spoken / written book-name aliases -> canonical name. Keys are lowercased
# and stripped of non-alphanumerics so matching is forgiving on the mic.
_BOOK_ALIASES = {
    "psalm": "Psalms", "psalms": "Psalms",
    "song of solomon": "Song of Solomon", "song of songs": "Song of Solomon",
    "songs": "Song of Solomon", "song": "Song of Solomon",
    "ecclesiastes": "Ecclesiastes", "eccl": "Ecclesiastes",
    "1samuel": "1 Samuel", "firstsamuel": "1 Samuel", "isamuel": "1 Samuel",
    "2samuel": "2 Samuel", "secondsamuel": "2 Samuel", "iisamuel": "2 Samuel",
    "1kings": "1 Kings", "firstkings": "1 Kings", "ikings": "1 Kings",
    "2kings": "2 Kings", "secondkings": "2 Kings", "iikings": "2 Kings",
    "1chronicles": "1 Chronicles", "firstchronicles": "1 Chronicles",
    "ichronicles": "1 Chronicles",
    "2chronicles": "2 Chronicles", "secondchronicles": "2 Chronicles",
    "iichronicles": "2 Chronicles",
    "1corinthians": "1 Corinthians", "firstcorinthians": "1 Corinthians",
    "icorinthians": "1 Corinthians",
    "2corinthians": "2 Corinthians", "secondcorinthians": "2 Corinthians",
    "iicorinthians": "2 Corinthians",
    "1thessalonians": "1 Thessalonians", "firstthessalonians": "1 Thessalonians",
    "ithessalonians": "1 Thessalonians",
    "2thessalonians": "2 Thessalonians", "secondthessalonians": "2 Thessalonians",
    "iithessalonians": "2 Thessalonians",
    "1timothy": "1 Timothy", "firsttimothy": "1 Timothy", "itimothy": "1 Timothy",
    "2timothy": "2 Timothy", "secondtimothy": "2 Timothy", "iitimothy": "2 Timothy",
    "1peter": "1 Peter", "firstpeter": "1 Peter", "ipeter": "1 Peter",
    "2peter": "2 Peter", "secondpeter": "2 Peter", "iipeter": "2 Peter",
    "1john": "1 John", "firstjohn": "1 John", "ijohn": "1 John",
    "2john": "2 John", "secondjohn": "2 John", "iijohn": "2 John",
    "3john": "3 John", "thirdjohn": "3 John", "iiijohn": "3 John",
    "genesis": "Genesis", "gen": "Genesis",
    "exodus": "Exodus", "exod": "Exodus", "ex": "Exodus",
    "leviticus": "Leviticus", "lev": "Leviticus",
    "numbers": "Numbers", "num": "Numbers",
    "deuteronomy": "Deuteronomy", "deut": "Deuteronomy",
    "joshua": "Joshua", "josh": "Joshua",
    "judges": "Judges", "judg": "Judges",
    "ruth": "Ruth",
    "ezra": "Ezra",
    "nehemiah": "Nehemiah", "neh": "Nehemiah",
    "esther": "Esther", "esth": "Esther",
    "job": "Job",
    "proverbs": "Proverbs", "prov": "Proverbs",
    "isaiah": "Isaiah", "isa": "Isaiah",
    "jeremiah": "Jeremiah", "jer": "Jeremiah",
    "lamentations": "Lamentations", "lam": "Lamentations",
    "ezekiel": "Ezekiel", "ezek": "Ezekiel",
    "daniel": "Daniel", "dan": "Daniel",
    "hosea": "Hosea", "hos": "Hosea",
    "joel": "Joel",
    "amos": "Amos",
    "obadiah": "Obadiah", "obad": "Obadiah",
    "jonah": "Jonah",
    "micah": "Micah", "mic": "Micah",
    "nahum": "Nahum", "nah": "Nahum",
    "habakkuk": "Habakkuk", "hab": "Habakkuk",
    "zephaniah": "Zephaniah", "zeph": "Zephaniah",
    "haggai": "Haggai", "hag": "Haggai",
    "zechariah": "Zechariah", "zech": "Zechariah",
    "malachi": "Malachi", "mal": "Malachi",
    "matthew": "Matthew", "matt": "Matthew",
    "mark": "Mark",
    "luke": "Luke",
    "john": "John",
    "acts": "Acts",
    "romans": "Romans", "rom": "Romans",
    "galatians": "Galatians", "gal": "Galatians",
    "ephesians": "Ephesians", "eph": "Ephesians",
    "philippians": "Philippians", "phil": "Philippians",
    "colossians": "Colossians", "col": "Colossians",
    "titus": "Titus",
    "philemon": "Philemon", "phlm": "Philemon",
    "hebrews": "Hebrews", "heb": "Hebrews",
    "james": "James", "jas": "James",
    "jude": "Jude",
    "revelation": "Revelation", "rev": "Revelation",
    "revelations": "Revelation",
}
# Auto-add explicit "1 samuel", "i samuel", "first samuel" (with spaces) maps.
_PREFIX_WORDS = {
    "1": "1", "2": "2", "3": "3",
    "i": "1", "ii": "2", "iii": "3",
    "first": "1", "second": "2", "third": "3",
}
# Map each numbered book to only the prefix words that match its number.
for _canon in CANONICAL_BOOKS:
    if _canon[0] in "123" and " " in _canon:
        _num = _canon[0]
        _base = _canon.split(" ", 1)[1]
        _bl = _base.lower()
        for _pw, _pnum in _PREFIX_WORDS.items():
            if _pnum == _num:
                _BOOK_ALIASES.setdefault(f"{_pw} {_bl}", _canon)
                _BOOK_ALIASES.setdefault(f"{_pw}{_bl}", _canon)


@dataclass(frozen=True)
class VerseRef:
    """A resolved reference + its text in one translation."""
    translation: str
    book: str
    chapter: int
    verse: int
    text: str

    @property
    def reference(self) -> str:
        return f"{self.book} {self.chapter}:{self.verse}"

    @property
    def key(self) -> str:
        return f"{self.book} {self.chapter}:{self.verse}"


def _norm_code(raw: str) -> str:
    v = (raw or "").strip().upper()
    return _VERSION_CODE.get(v, v or "UNK")


def resolve_translation(name: str) -> Optional[str]:
    """Map a spoken/written translation name to a short code, or None."""
    if not name:
        return None
    key = name.strip().lower()
    if key in VERSION_ALIASES:
        return VERSION_ALIASES[key]
    up = name.strip().upper()
    if up in _VERSION_CODE:
        return _VERSION_CODE[up]
    return None


def resolve_book(name: str) -> Optional[str]:
    """Map a spoken/written book name to a canonical book, or None.

    Matching is forgiving: lowercase, strip punctuation, try the full alias
    table including ordinal prefixes ("first samuel" / "1 samuel" / "ii samuel").
    """
    if not name:
        return None
    key = name.strip().lower()
    # fast canonical hits
    for b in CANONICAL_BOOKS:
        if key == b.lower():
            return b
    if key in _BOOK_ALIASES:
        return _BOOK_ALIASES[key]
    # punctuation-scrubbed fallback
    scrub = "".join(c for c in key if c.isalnum() or c.isspace()).strip()
    if scrub in _BOOK_ALIASES:
        return _BOOK_ALIASES[scrub]
    # collapse internal whitespace and retry
    squashed = "".join(c for c in key if c.isalnum())
    if squashed in _BOOK_ALIASES:
        return _BOOK_ALIASES[squashed]
    return None


class BibleDB:
    """In-memory direct-lookup table over all versions in one JSON file."""

    def __init__(self, by_tv: dict, by_ref: dict, versions: list[str]):
        # by_tv: {(translation, book, chapter, verse): text}
        # by_ref: {(book, chapter, verse): {translation: text}}
        self._by_tv = by_tv
        self._by_ref = by_ref
        self.versions = versions

    def lookup(self, translation: str, book: str, chapter: int, verse: int) -> Optional[VerseRef]:
        code = _norm_code(translation)
        canon = book if book in CANONICAL_BOOKS else resolve_book(book)
        if canon is None:
            return None
        text = self._by_tv.get((code, canon, int(chapter), int(verse)))
        if text is None:
            return None
        return VerseRef(code, canon, int(chapter), int(verse), text)

    def has(self, translation: str, book: str, chapter: int, verse: int) -> bool:
        return self.lookup(translation, book, chapter, verse) is not None

    def translations_for(self, book: str, chapter: int, verse: int) -> list[str]:
        canon = book if book in CANONICAL_BOOKS else resolve_book(book)
        if canon is None:
            return []
        return sorted(self._by_ref.get((canon, int(chapter), int(verse)), {}).keys())

    def all_versions_for(self, book: str, chapter: int, verse: int) -> dict[str, str]:
        canon = book if book in CANONICAL_BOOKS else resolve_book(book)
        if canon is None:
            return {}
        return dict(self._by_ref.get((canon, int(chapter), int(verse)), {}))

    def max_verse(self, book: str, chapter: int) -> int:
        """Highest verse number that exists for (book, chapter) across versions."""
        canon = book if book in CANONICAL_BOOKS else resolve_book(book)
        if canon is None:
            return 0
        hi = 0
        for (_b, _c, vnum) in self._by_ref.keys():
            if _b == canon and _c == int(chapter) and vnum > hi:
                hi = vnum
        return hi

    def max_chapter(self, book: str) -> int:
        """Highest chapter number that exists for a book across versions."""
        canon = book if book in CANONICAL_BOOKS else resolve_book(book)
        if canon is None:
            return 0
        hi = 0
        for (_b, _c, _v) in self._by_ref.keys():
            if _b == canon and _c > hi:
                hi = _c
        return hi

    def chapter_verses(self, translation: str, book: str, chapter: int) -> list[VerseRef]:
        """Return every verse in a chapter for one translation, sorted by verse number.
        Used by the GET /bible endpoint to render the Bible reader panel.
        """
        code = _norm_code(translation)
        canon = book if book in CANONICAL_BOOKS else resolve_book(book)
        if canon is None:
            return []
        cnum = int(chapter)
        out: list[VerseRef] = []
        for (_b, _c, vnum), texts in self._by_ref.items():
            if _b == canon and _c == cnum and code in texts:
                out.append(VerseRef(code, canon, cnum, int(vnum), texts[code]))
        out.sort(key=lambda r: r.verse)
        return out


_LOCK = threading.Lock()
_DB: Optional[BibleDB] = None


def load_bible_db(path: str = ALL_VERSIONS_PATH) -> BibleDB:
    """Load (and cache) the all-versions Bible JSON into a BibleDB."""
    global _DB
    with _LOCK:
        if _DB is not None and _path_matches(path):
            return _DB
        if not os.path.exists(path):
            raise FileNotFoundError(f"Bible versions file not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        by_tv: dict = {}
        by_ref: dict = {}
        versions: list[str] = []
        raw_versions = data.get("versions", [])
        for v in raw_versions:
            versions.append(_norm_code(v))
        for book in data.get("books", []):
            bname = book.get("book") or book.get("name") or book.get("bookName")
            if bname not in CANONICAL_BOOKS:
                # be lenient: try to resolve, else keep raw
                r = resolve_book(bname)
                bname = r or bname
            for chap in book.get("chapters", []):
                cnum = int(chap.get("chapter") or chap.get("chapter_number") or 0)
                for v in chap.get("verses", []):
                    vnum = int(v.get("verse") or v.get("verse_number") or 0)
                    txt = v.get("text")
                    if isinstance(txt, dict):
                        for raw_t, body in txt.items():
                            code = _norm_code(raw_t)
                            body = (body or "").strip()
                            if not body:
                                continue
                            by_tv[(code, bname, cnum, vnum)] = body
                            by_ref.setdefault((bname, cnum, vnum), {})[code] = body
                    elif isinstance(txt, str) and txt.strip():
                        # single-version file shape; tag with whatever version
                        # the file declares, if any.
                        code = _norm_code(raw_versions[0]) if raw_versions else "UNK"
                        body = txt.strip()
                        by_tv[(code, bname, cnum, vnum)] = body
                        by_ref.setdefault((bname, cnum, vnum), {})[code] = body
        _DB = BibleDB(by_tv, by_ref, versions)
        _LAST_PATH = path
        return _DB


_LAST_PATH: str = ALL_VERSIONS_PATH


def _path_matches(path: str) -> bool:
    return os.path.abspath(path) == os.path.abspath(_LAST_PATH)


def get_bible_db(path: str = ALL_VERSIONS_PATH) -> BibleDB:
    """Process-wide singleton accessor (offline, local file only)."""
    return load_bible_db(path)


if __name__ == "__main__":
    db = load_bible_db()
    print(f"versions: {db.versions}")
    print(f"total (translation,ref) rows: {len(db._by_tv)}")
    r = db.lookup("NKJV", "Romans", 1, 16)
    print("Romans 1:16 NKJV ->", (r.text[:80] if r else None))
    r = db.lookup("AMP", "Ezekiel", 36, 27)
    print("Ezekiel 36:27 AMP ->", (r.text[:80] if r else None))
    r = db.lookup("MSB", "John", 3, 16)
    print("John 3:16 MSB ->", (r.text[:80] if r else None))
    print("resolve_book('first samuel') =", resolve_book("first samuel"))
    print("resolve_book('2 KINGS') =", resolve_book("2 KINGS"))
    print("resolve_book('iii john') =", resolve_book("iii john"))
    print("resolve_translation('amplified') =", resolve_translation("amplified"))
    print("translations_for Ezekiel 36:27 =", db.translations_for("Ezekiel", 36, 27))