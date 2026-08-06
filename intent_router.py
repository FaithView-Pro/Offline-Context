"""Milestone 1 -- LIVE INTENT ROUTER.

Not every spoken window needs a full semantic search. This router classifies a
chunk of transcript text into one of four intents and resolves each one *without*
FAISS/retrieve.py whenever the intent is explicit:

  1. EXPLICIT_REF  -- an explicit reference was spoken ("Romans 1:16",
     "Ezekiel 36 27", "2 Kings 6:2"). Parse with a reference regex, then do a
     DIRECT lookup against the all-versions Bible database (bible_db.py) by
     (translation, book, chapter, verse). Exact match only -- no search.
  2. NAV_COMMAND   -- "next verse" / "previous verse" (+/-1 on chapter:verse of
     the LAST displayed reference), or a translation swap ("give me that in the
     Amplified" / "in the NKJV" / "read it in the ESV" -- same reference, swap
     translation). Resolved via direct corpus lookup. Requires session state
     `current_reference`, updated every time ANY verse is displayed, from any
     path. CLEAR does NOT reset it.
  3. CLEAR_COMMAND -- "remove it", "not that one", "clear it", "take it down".
     Emits a hide-display signal. Must NOT reset current_reference.
  4. CUE_PHRASE / unknown -- anything else. Passed through unchanged to the
     existing Stage 3 -> 6 pipeline (quote_detect -> retrieve -> rerank -> score).

Everything here is a PURE function: input is a plain text string plus a small
session-state dataclass; output is a RouteResult. So the router is unit-testable
with plain strings before any audio is involved.

The nav/clear command pattern lists are deliberately SEPARATE from the
scripture-quote trigger library in build_labeling_csv.py / quote_detect.py --
those find scripture_QUOTES in sermon speech, which is a different job from
recognizing operator navigation commands. Do not merge them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import bible_db


# ===========================================================================
# Intent constants
# ===========================================================================
EXPLICIT_REF = "EXPLICIT_REF"
NAV_COMMAND = "NAV_COMMAND"
CLEAR_COMMAND = "CLEAR_COMMAND"
CUE_PHRASE = "CUE_PHRASE"


# ===========================================================================
# Session state -- the only mutable thing the router touches
# ===========================================================================
@dataclass
class SessionState:
    """`current_reference` is updated every time ANY verse is displayed, from
    ANY path (explicit ref, nav, or a presented detection). It is the anchor
    for "next verse" / "previous verse" / translation swap. CLEAR_COMMAND does
    NOT reset it -- a later "next verse" still resolves off the last real verse
    shown, not fail just because the display was cleared.
    """
    current_reference: Optional[tuple[str, str, int, int]] = None
    # (translation_code, book, chapter, verse)


# ===========================================================================
# Routed result
# ===========================================================================
@dataclass
class RouteResult:
    intent: str
    text: str                       # the original input text (passthrough / debug)

    # EXPLICIT_REF / NAV_COMMAND resolved target
    translation: Optional[str] = None
    book: Optional[str] = None
    chapter: Optional[int] = None
    verse: Optional[int] = None
    reference: Optional[str] = None     # "Romans 8:28"
    verse_text: Optional[str] = None    # resolved text from direct lookup
    available_translations: list[str] = field(default_factory=list)

    # NAV specifics
    nav_action: Optional[str] = None    # "next_verse"|"prev_verse"|"swap_translation"
    target_translation: Optional[str] = None

    # CLEAR specifics
    clear: bool = False

    # True when the intent matched but the reference could not resolve
    # (e.g. out-of-range chapter/verse, or nav with no current_reference).
    unresolved: bool = False
    reason: Optional[str] = None


# ===========================================================================
# CLEAR & NAV command vocabularies (kept SEPARATE from quote-detection triggers)
# ===========================================================================
# Lines an operator/preacher says to clear the live display.
CLEAR_PHRASES = [
    "remove it", "not that one", "clear it", "take it down", "clear that",
    "remove that", "take that down", "not this one", "clear the display",
    "clear screen", "hide that", "hide it", "wrong one", "that's wrong",
    "thats wrong", "that is wrong", "not right", "clear this", "remove this",
    "blank it", "blank the screen", "clear the verse",
]

# Next / previous verse navigation.
NAV_NEXT_VERSE = [
    "next verse", "next one", "the next verse", "go to the next verse",
    "following verse", "move to the next verse", "advance one verse",
    "advance a verse", "one more verse", "next please", "read the next verse",
    "the next one", "skip ahead one", "go forward one verse", "forward a verse",
]
NAV_PREV_VERSE = [
    "previous verse", "previous one", "prev verse", "last verse",
    "the previous verse", "go back a verse", "one verse back", "back one",
    "the verse before", "prior verse", "go back one verse", "backward a verse",
    "one verse before", "the one before",
]
NAV_NEXT_CHAPTER = ["next chapter", "following chapter", "go to the next chapter"]
NAV_PREV_CHAPTER = ["previous chapter", "prior chapter", "last chapter",
                    "chapter before", "go back a chapter"]

# Translation swap: structured patterns that name a translation as the target
# of a swap for the current reference. Built in _build_swap_regex() below so the
# alias list lives in one place (bible_db.VERSION_ALIASES).
_SWAP_ALIASES = None
_SWAP_REGEXES = None


def _build_swap_regex():
    global _SWAP_ALIASES, _SWAP_REGEXES
    aliases = sorted(bible_db.VERSION_ALIASES.keys(), key=lambda a: (-len(a.split()), -len(a)))
    alt = "|".join(re.escape(a) for a in aliases)
    _SWAP_ALIASES = aliases
    # Order matters: most specific / least ambiguous first.
    _SWAP_REGEXES = [
        re.compile(rf"\bgive me (?:that|it|this)\s+(?:in|from|on)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bread (?:it|that|this)\s+(?:in|from|on)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bshow (?:me\s+)?(?:that|it|this)\s+(?:in|from|on)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bsame (?:verse|one|reference|text)\s+(?:in|from|on)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bthat (?:one\s+)?(?:in|from)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bit (?:in|from)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bswitch (?:to|over to)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bchange (?:to|over to)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\bnow (?:let'?s see it |let us see it |see it )?(?:in|from)\s+(?:the\s+)?({alt})\b", re.I),
        re.compile(rf"\blet'?s see (?:it|that)\s+(?:in|from)\s+(?:the\s+)?({alt})\b", re.I),
        # Broad, used only when ref present + short utterance (applied gated below).
        re.compile(rf"\b(?:in|from|on)\s+(?:the\s+)?({alt})\b", re.I),
    ]


def _ensure_swap_regex():
    if _SWAP_REGEXES is None:
        _build_swap_regex()


# ===========================================================================
# Reference parser (separate from quote_detect._REF_RE -- different job:
# operator/pastor speaking an explicit reference to resolve directly)
# ===========================================================================
# ordinal prefix: "1 ", "2 ", "3 ", "I ", "II ", "III ", "first ", "second ",
# "third ", "1st ", "2nd ", "3rd ". The `\s+` after is required.
_ORD = rf"(?:(?:[1-3]|I{{1,3}}|first|second|third|1st|2nd|3rd)\s+)?"
# One title-case word, optionally followed by " of <TitleWord>" (Song of Solomon).
# Case-insensitive so mic transcripts of varying capitalisation still parse.
_BOOK = rf"(?:[A-Za-z][a-z]+(?:\s+of\s+[A-Za-z][a-z]+)?)"

# <book> [chapter] <chap> ( : | space [verse] ) <verse>
_REF_RE = re.compile(
    rf"\b{_ORD}(?P<book>{_BOOK})\s+(?:chapter\s+)?(?P<chap>\d{{1,3}})(?:\s*:\s*|\s+(?:verse\s+)?)(?P<verse>\d{{1,3}})\b",
    re.IGNORECASE,
)


def parse_reference(text: str) -> Optional[re.Match]:
    """Return the first reference regex match in ``text``, or None."""
    if not text:
        return None
    return _REF_RE.search(text)


def _detect_translation_in(text: str, gated_broad: bool = False) -> Optional[str]:
    """Find a named translation in the text via structured swap cues.

    Returns the resolved short code (e.g. "AMP") or None. ``gated_broad``
    enables the least-specific "in the {t}" fallback (only safe when the
    utterance is short and a reference is otherwise absent).
    """
    _ensure_swap_regex()
    low = text.lower()
    limit = len(_SWAP_REGEXES) if not gated_broad else len(_SWAP_REGEXES)
    # Non-broad runs patterns 0..-2; broad adds the final fallback.
    end = len(_SWAP_REGEXES) if gated_broad else len(_SWAP_REGEXES) - 1
    for rx in _SWAP_REGEXES[:end]:
        m = rx.search(low)
        if m:
            alias = m.group(1).strip()
            code = bible_db.resolve_translation(alias)
            if code:
                return code
    return None


# ===========================================================================
# Pure routing function
# ===========================================================================
def route(
    text: str,
    state: SessionState,
    db: Optional[bible_db.BibleDB] = None,
    default_translation: str = "NKJV",
) -> RouteResult:
    """Classify ``text`` and resolve the intent. Pure: no I/O beyond the DB.

    Precedence (chosen so an explicit reference always wins, since it is the
    most unambiguous signal):
        1. EXPLICIT_REF if the reference regex matches.
        2. CLEAR_COMMAND if a clear phrase is present.
        3. NAV_COMMAND if a next/prev or swap cue is present.
        4. CUE_PHRASE otherwise.
    """
    if db is None:
        db = bible_db.get_bible_db()

    result = RouteResult(intent=CUE_PHRASE, text=text or "")

    # ---- 1. EXPLICIT_REF --------------------------------------------------
    m = parse_reference(text)
    if m:
        return _resolve_explicit(m, text, db, default_translation)

    # ---- 2. CLEAR_COMMAND -------------------------------------------------
    low = (text or "").lower().strip()
    if any(p in low for p in CLEAR_PHRASES):
        # Guard: a long teaching sentence containing "not that one" as a
        # fragment is unlikely, but require the clear phrase to be most of a
        # short utterance (<= 6 words) for safety.
        words = low.split()
        if len(words) <= 8:
            return RouteResult(intent=CLEAR_COMMAND, text=text, clear=True)

    # ---- 3. NAV_COMMAND ---------------------------------------------------
    nav = _resolve_nav(text, state, db, default_translation)
    if nav is not None:
        return nav

    # ---- 4. CUE_PHRASE (fallback) -----------------------------------------
    return result


# ---- helpers --------------------------------------------------------------
def _resolve_explicit(m: re.Match, text: str, db, default_translation: str) -> RouteResult:
    book_raw = m.group(0).split(None, 1)  # not reliable for ordinal+book; rebuild
    chap = int(m.group("chap"))
    verse = int(m.group("verse"))
    # Reconstruct the book token (everything from match start up to the chap number).
    full = m.group(0)
    chap_str = m.group("chap")
    book_token = full[: m.start("chap") - m.start(0)].strip()
    # strip a trailing "chapter" word if present
    book_token = re.sub(r"\s+chapter\s*$", "", book_token, flags=re.I).strip()
    canon = bible_db.resolve_book(book_token)
    if canon is None:
        return RouteResult(intent=EXPLICIT_REF, text=text,
                           book=book_token, chapter=chap, verse=verse,
                           unresolved=True, reason=f"unknown book: {book_token!r}")

    # Detect an explicit translation request in the same utterance.
    trans = _detect_translation_in(text, gated_broad=False)
    if trans is None:
        trans = default_translation

    ref = db.lookup(trans, canon, chap, verse)
    if ref is None:
        avail = db.translations_for(canon, chap, verse)
        if avail:
            # translation not present for this ref -> fall back to any available
            pick = default_translation if default_translation in avail else avail[0]
            ref = db.lookup(pick, canon, chap, verse)
            trans = pick
    if ref is None:
        return RouteResult(intent=EXPLICIT_REF, text=text,
                           translation=trans, book=canon, chapter=chap, verse=verse,
                           reference=f"{canon} {chap}:{verse}",
                           unresolved=True, reason="reference out of range")
    return RouteResult(
        intent=EXPLICIT_REF, text=text,
        translation=ref.translation, book=ref.book, chapter=ref.chapter,
        verse=ref.verse, reference=ref.reference, verse_text=ref.text,
        available_translations=db.translations_for(ref.book, ref.chapter, ref.verse),
    )


def _resolve_nav(text: str, state: SessionState, db, default_translation: str) -> Optional[RouteResult]:
    low = (text or "").lower().strip()

    # --- next / previous VERSE / CHAPTER ---
    is_next_v = any(p in low for p in NAV_NEXT_VERSE)
    is_prev_v = any(p in low for p in NAV_PREV_VERSE)
    is_next_c = any(p in low for p in NAV_NEXT_CHAPTER)
    is_prev_c = any(p in low for p in NAV_PREV_CHAPTER)

    # --- translation swap ---
    # Only attempt the broad fallback when the utterance is short (<= 6 words)
    # and no explicit ref matched (we never reach here if a ref matched).
    gated_broad = (len(low.split()) <= 6)
    swap_trans = _detect_translation_in(text, gated_broad=gated_broad)

    has_nav = is_next_v or is_prev_v or is_next_c or is_prev_c or swap_trans is not None
    if not has_nav:
        return None

    if state.current_reference is None:
        # We know the intent is nav but cannot resolve without an anchor.
        return RouteResult(intent=NAV_COMMAND, text=text,
                           nav_action=("swap_translation" if swap_trans else
                                      ("next_verse" if is_next_v else
                                       "prev_verse" if is_prev_v else
                                       "next_chapter" if is_next_c else "prev_chapter")),
                           target_translation=swap_trans,
                           unresolved=True, reason="no current_reference to navigate from")

    _, book, chapter, verse = state.current_reference

    # Translation swap takes priority over next/prev when both are present
    # (rare in practice; swap is the more specific instruction).
    if swap_trans is not None and not (is_next_v or is_prev_v or is_next_c or is_prev_c):
        ref = db.lookup(swap_trans, book, chapter, verse)
        if ref is None:
            avail = db.translations_for(book, chapter, verse)
            return RouteResult(intent=NAV_COMMAND, text=text,
                               nav_action="swap_translation",
                               target_translation=swap_trans,
                               unresolved=True,
                               reason=f"translation {swap_trans} not available for {book} {chapter}:{verse}",
                               available_translations=avail)
        return RouteResult(intent=NAV_COMMAND, text=text,
                           nav_action="swap_translation",
                           target_translation=ref.translation,
                           translation=ref.translation, book=ref.book,
                           chapter=ref.chapter, verse=ref.verse,
                           reference=ref.reference, verse_text=ref.text,
                           available_translations=db.translations_for(ref.book, ref.chapter, ref.verse))

    # Next verse
    if is_next_v:
        ref = _step_verse(db, book, chapter, verse, +1, prefer=state.current_reference[0])
        return _nav_result(text, db, ref, "next_verse", None,
                           reason="next verse out of range" if ref is None else None)

    # Prev verse
    if is_prev_v:
        ref = _step_verse(db, book, chapter, verse, -1, prefer=state.current_reference[0])
        return _nav_result(text, db, ref, "prev_verse", None,
                           reason="previous verse out of range" if ref is None else None)

    # Next chapter -> first verse
    if is_next_c:
        new_c = chapter + 1
        if new_c > db.max_chapter(book):
            return _nav_result(text, db, None, "next_chapter", None,
                               reason=f"no chapter after {book} {chapter}")
        ref = db.lookup(state.current_reference[0], book, new_c, 1) \
            or _first_available(db, book, new_c)
        return _nav_result(text, db, ref, "next_chapter", None,
                           reason="next chapter has no verse 1" if ref is None else None)

    # Prev chapter -> last verse
    if is_prev_c:
        new_c = chapter - 1
        if new_c < 1:
            return _nav_result(text, db, None, "prev_chapter", None,
                               reason=f"no chapter before {book} {chapter}")
        last_v = db.max_verse(book, new_c)
        if last_v == 0:
            return _nav_result(text, db, None, "prev_chapter", None,
                               reason=f"chapter {new_c} of {book} not found")
        ref = db.lookup(state.current_reference[0], book, new_c, last_v) \
            or _first_available(db, book, new_c, last_v)
        return _nav_result(text, db, ref, "prev_chapter", None)

    return None


def _step_verse(db, book: str, chapter: int, verse: int, delta: int,
                prefer: Optional[str] = None) -> Optional[bible_db.VerseRef]:
    """Advance +1 / -1 verse, wrapping chapter boundaries. Prefers the anchor
    translation (``prefer``), then any available."""
    if delta > 0:
        v = verse + 1
        c = chapter
        if v > db.max_verse(book, c):
            # wrap to next chapter verse 1
            c = chapter + 1
            v = 1
            if c > db.max_chapter(book):
                return None
        return _first_available(db, book, c, v, prefer=prefer)
    else:
        v = verse - 1
        c = chapter
        if v < 1:
            c = chapter - 1
            if c < 1:
                return None
            v = db.max_verse(book, c)
            if v == 0:
                return None
        return _first_available(db, book, c, v, prefer=prefer)


def _first_available(db, book: str, chapter: int, verse: int,
                     prefer: Optional[str] = None) -> Optional[bible_db.VerseRef]:
    """Return the verse in the preferred translation, else any available."""
    if prefer is not None:
        r = db.lookup(prefer, book, chapter, verse)
        if r is not None:
            return r
    avail = db.translations_for(book, chapter, verse)
    if not avail:
        return None
    pick = prefer if (prefer and prefer in avail) else avail[0]
    return db.lookup(pick, book, chapter, verse)


def _nav_result(text, db, ref, action, target_translation,
                reason=None):
    if ref is None:
        return RouteResult(intent=NAV_COMMAND, text=text, nav_action=action,
                           target_translation=target_translation,
                           unresolved=True, reason=reason)
    return RouteResult(intent=NAV_COMMAND, text=text, nav_action=action,
                       target_translation=target_translation,
                       translation=ref.translation, book=ref.book,
                       chapter=ref.chapter, verse=ref.verse,
                       reference=ref.reference, verse_text=ref.text,
                       available_translations=db.translations_for(ref.book, ref.chapter, ref.verse))


# ===========================================================================
# Helper the pipeline uses to keep session state in sync with every display
# ===========================================================================
def note_display(state: SessionState, translation: str, book: str,
                 chapter: int, verse: int) -> None:
    """Call this whenever ANY verse is shown on the live display (from explicit
    ref, nav, or a presented detection). Keeps `current_reference` as the
    anchor for next/prev/swap. CLEAR must NOT call this."""
    state.current_reference = (translation, book, int(chapter), int(verse))


def clear_display_keeps_anchor(state: SessionState) -> None:
    """CLEAR only hides; it intentionally leaves current_reference intact."""
    return None


# ===========================================================================
# Self-test -- run with plain strings, no audio needed
# ===========================================================================
if __name__ == "__main__":
    db = bible_db.get_bible_db()
    st = SessionState()
    print(f"{'INTENT':14} {'ref':<16} {'trans':<5} {'text':<20}  input")
    print("-" * 100)
    cases = [
        # (input, expected_intent, note)
        ("turn to Romans 8:28", EXPLICIT_REF, ""),
        ("Romans 1:16", EXPLICIT_REF, ""),
        ("Ezekiel 36 27", EXPLICIT_REF, "space-sep"),
        ("2 Kings 6:2", EXPLICIT_REF, "ordinal digit"),
        ("second kings 6 2", EXPLICIT_REF, "ordword + spaces"),
        ("III John 1 4", EXPLICIT_REF, "roman + spaces"),
        ("song of solomon 1:1", EXPLICIT_REF, "of-word book"),
        ("Genesis chapter 1 verse 1", EXPLICIT_REF, "chapter/verse words"),
        ("Let's turn to John 3:16 in the Amplified", EXPLICIT_REF, "with translation"),
        ("next verse", NAV_COMMAND, "needs anchor"),
        ("give me that in the Amplified", NAV_COMMAND, "swap, needs anchor"),
        ("previous verse", NAV_COMMAND, "needs anchor"),
        ("remove it", CLEAR_COMMAND, ""),
        ("not that one", CLEAR_COMMAND, ""),
        ("clear it", CLEAR_COMMAND, ""),
        ("take it down", CLEAR_COMMAND, ""),
        ("the Bible says", CUE_PHRASE, "cue passthrough"),
        ("I went to the store yesterday", CUE_PHRASE, "chitchat"),
        ("for God so loved the world", CUE_PHRASE, "verbatim no cue"),
    ]
    ok = True
    for text_in, expect, note in cases:
        r = route(text_in, st, db)
        flag = "OK" if r.intent == expect else "XX"
        if r.intent != expect:
            ok = False
        ref = r.reference or ""
        disp = (r.verse_text[:18] + "...") if r.verse_text else ""
        print(f"{flag} {r.intent:14} {ref:<16} {(r.translation or ''):<5} {disp:<20}  {text_in!r}  {note}")

    # Now set an anchor and test nav resolves.
    print("\n-- with anchor = Ezekiel 36:27 NKJV --")
    st.current_reference = ("NKJV", "Ezekiel", 36, 27)
    for text_in, action in [
        ("next verse", "next_verse"),
        ("previous verse", "prev_verse"),
        ("give me that in the Amplified", "swap_translation"),
        ("now in the ESV", "swap_translation"),
        ("read it in the message", "swap_translation"),
    ]:
        r = route(text_in, st, db)
        flag = "OK" if r.intent == NAV_COMMAND and r.nav_action == action else "XX"
        if not (r.intent == NAV_COMMAND and r.nav_action == action):
            ok = False
        print(f"{flag} {r.intent:14} {r.nav_action:18} -> {r.reference or '<unresolved>':<16} {r.translation or ''}  {text_in!r}")

    # CLEAR must not reset the anchor: next verse after clear still resolves.
    print("\n-- CLEAR then next verse (anchor survives) --")
    r_clear = route("remove it", st, db)
    r_next = route("next verse", st, db)
    survived = (r_clear.intent == CLEAR_COMMAND and r_next.intent == NAV_COMMAND
                and r_next.reference == "Ezekiel 36:28")
    flag = "OK" if survived else "XX"
    if not survived:
        ok = False
    print(f"{flag} clear={r_clear.intent} then next -> {r_next.reference or '<unresolved>'}")

    # Swap to ESV then next verse should anchor at Ezekiel 36:27 ESV and move to 28.
    print("\n-- swap translation then next verse --")
    st.current_reference = ("ESV", "Ezekiel", 36, 27)
    r_swap = route("now in the Amplified", st, db)
    r_next2 = route("next verse", st, db)
    flag = "OK" if r_swap.reference == "Ezekiel 36:27" and r_swap.translation == "AMP" \
        and r_next2.reference == "Ezekiel 36:28" else "XX"
    if not (r_swap.reference == "Ezekiel 36:27" and r_swap.translation == "AMP"
            and r_next2.reference == "Ezekiel 36:28"):
        ok = False
    print(f"{flag} swap->{r_swap.translation} {r_swap.reference}; next->{r_next2.reference} {r_next2.translation}")

    # Chapter wrap: Romans 16:27 -> next verse wraps to Romans 1 of next? Romans has 16 chapters
    # so 16:27 next verse should hit end and fail. Use John 21:25 (last verse of John) -> next fails.
    print("\n-- end-of-book wrap --")
    st.current_reference = ("NKJV", "Jude", 1, 25)
    r_end = route("next verse", st, db)
    r_prev = route("previous verse", st, db)
    flag = "OK" if r_end.unresolved and r_prev.reference == "Jude 1:24" else "XX"
    if not (r_end.unresolved and r_prev.reference == "Jude 1:24"):
        ok = False
    print(f"{flag} Jude 1:25 next={r_end.reference or '<unresolved>'} prev={r_prev.reference}")

    print("\nALL ROUTER TESTS PASSED" if ok else "\nSOME ROUTER TESTS FAILED (see XX above)")