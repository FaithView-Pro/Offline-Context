"""Stage 3 -- QUOTE DETECTION (local classifier).

Decides whether each sentence window is *actually Scripture being quoted or
referenced*, versus story / joke / news / general teaching -- *before* any
Bible search runs, so retrieval is only spent on real candidates.

The detector is behind a small interface (``QuoteDetector.score``) so the
heuristic stand-in can be swapped for a trained MiniLM/DistilBERT/TinyBERT
ONNX classifier later without touching the rest of the pipeline:

    class MyOnnxDetector(QuoteDetector):
        def score(self, text: str) -> float: ...

Only windows with ``score >= QUOTE_THRESHOLD`` (default 0.70) proceed to
Stage 4 (semantic retrieval).

Heuristic architecture — four INDEPENDENT strong signals (OR, not additive):

                         Scripture Evidence
                               │
        ┌──────────────┬───────┴────────┬────────────────┐
        │              │                │                │
       CUE         REFERENCE      ATTRIBUTION       BOOK NAME
      STRONG        STRONG          STRONG            STRONG
        │              │                │                │
        └──────────────┴───────┬────────┴────────────────┘
                               │
                     INDEPENDENT DECISION
                               │
                  Scripture candidate = YES

If ANY ONE of the four strong signals fires, the sentence is independently
classified as a strong Scripture candidate (score >= 0.90) — the other signals
are NOT required. Weak signals (theological vocabulary, quotation marks,
archaic diction, base scores, penalties) are deliberately NOT part of the
primary decision: they cannot promote a sentence on their own.

Known limitation (documented): a verbatim quote in fully modern English with
no cue, no reference, and no biblical attribution may score below threshold
under the pure heuristic. The HybridQuoteDetector covers that gap: when no
strong rule fires, the trained ONNX classifier (onnx_model/) makes the
decision. In practice preachers usually introduce quotes with a cue ("the
Bible says", "Paul writes", "it is written"), read the reference first, or
attribute the quote to a biblical speaker ("Isaiah declares"), so recall is
reasonable; precision is the priority for the offline decision engine. The
SemanticQuoteDetector's dual-translation concordance check remains as an
independent rescue path.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

import config


# ==========================================================================
# STRONG SIGNAL 1 — CUE: explicit "I am quoting scripture" markers.
# A cue alone is sufficient for a strong Scripture prediction; it does NOT
# need a reference, quoting verb, quotation marks, or theological vocabulary.
# ==========================================================================
CUE_PHRASES = [
    "the bible says", "the bible teaches", "the bible declares", "the bible states",
    "the bible records", "the bible tells us", "the bible writes",
    "it is written", "as it is written", "the scripture says", "scripture says",
    "the scriptures say", "the word of the lord", "thus saith the lord",
    "thus says the lord", "the lord says", "the lord declares", "the lord spoke",
    "god says", "god said", "the word says", "the word of god says",
    "according to the scripture", "according to scripture", "the prophet says",
    "the prophet declared", "the psalmist says", "the psalmist writes",
    "david said", "david writes", "paul writes", "paul says", "paul tells us",
    "jesus said", "jesus says", "christ said", "the lord jesus said",
    "remember the word", "the bible assures us", "scripture tells us",
    "scripture declares", "scripture states", "the apostle paul says",
]

# ==========================================================================
# STRONG SIGNAL 2 — REFERENCE: an explicit chapter:verse citation.
# A reference alone is sufficient; no cue or quoting verb is required.
# ==========================================================================
# Matches "Romans 8:28", "1 John 4:8", "III John 1:4", "Psalm 23:1"
_REF_RE = re.compile(
    r"\b(?:[1-3]\s+|[IV]+\s+|First\s+|Second\s+|Third\s+)?"
    r"(?:[A-Z][a-z]+(?:\s+(?:of\s+)?[A-Z][a-z]+)?)\s+\d{1,3}:\d{1,3}\b"
)
# Matches a bare "chapter:verse" token like "8:28"
_BARE_REF_RE = re.compile(r"\b\d{1,3}\s*:\s*\d{1,3}\b")

# ==========================================================================
# STRONG SIGNAL 3 — QUOTING VERB / ATTRIBUTION.
#
# A quoting verb becomes a strong, INDEPENDENT signal only when the speaker
# is a biblical figure or biblical authority ("Paul writes", "Isaiah
# declares", "the prophet says"). A generic quoting verb alone ("my teacher
# says", "the news says", "John says") is NOT a Scripture signal.
# ==========================================================================
_QUOTING_VERB_PATTERN = (
    r"(?:says|said|saith|writes|wrote|declares|declared|states|stated|reads|"
    r"tells\s+us|told\s+us|reminds\s+us|teaches(?:\s+us)?|taught(?:\s+us)?|"
    r"records|recorded|proclaims|proclaimed|testifies|testified|"
    r"warns|warned|commands|commanded|promises|promised|speaks|spoke)"
)

# Biblical speakers/authorities. Bare ambiguous first names that are common in
# modern speech (john, mark, luke, james, joseph, stephen, timothy, ...) are
# deliberately EXCLUDED unless qualified by a role ("the apostle john"), so
# "John says he will arrive tomorrow" does not fire while "Peter says" does.
_BIBLICAL_SPEAKERS = [
    # --- Divine authority ---
    "the lord jesus christ", "the lord jesus", "lord jesus christ",
    "lord jesus", "jesus christ", "christ jesus", "jesus", "christ",
    "the lord god", "lord god", "the lord of hosts", "the lord", "god",
    "the holy spirit", "the holy ghost", "the father", "the almighty",
    "the most high", "the holy one", "the son of god", "the son of man",
    "jehovah", "yahweh",
    # --- Scripture itself as the speaker ---
    "the scriptures", "scripture", "the bible", "the word of god",
    "the word of the lord", "the word",
    # --- Biblical authors / figures (unambiguous names) ---
    "paul", "peter", "jude", "moses", "david", "solomon", "isaiah",
    "jeremiah", "ezekiel", "daniel", "hosea", "joel", "amos", "obadiah",
    "jonah", "micah", "nahum", "habakkuk", "zephaniah", "haggai",
    "zechariah", "malachi", "abraham", "isaac", "jacob", "job", "samuel",
    "elijah", "elisha", "ezra", "nehemiah", "noah",
    # --- Roles / titles ---
    "the apostle paul", "the apostle peter", "the apostle john",
    "the prophets", "the prophet", "the psalmist", "the apostles",
    "the apostle", "the evangelist", "the writer of hebrews",
    "the author of hebrews", "the angel of the lord", "the beloved disciple",
]


def _alternation(phrases) -> str:
    """Regex alternation of literal phrases, longest first (no backtracking)."""
    return "(?:" + "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True)) + ")"


# Speaker -> verb: "Paul writes", "Isaiah declares", "the prophet says".
_ATTRIBUTION_RE = re.compile(
    r"\b" + _alternation(_BIBLICAL_SPEAKERS) + r"\s+" + _QUOTING_VERB_PATTERN + r"\b"
)

# Verb -> speaker (quote-then-attribute): "'I will never leave you,' says the
# Lord." Restricted to unambiguous divine titles so that e.g. "the atheist
# says God is dead" cannot fire the signal.
_INVERTED_DIVINE_SPEAKERS = [
    "the lord of hosts", "the lord god", "lord god", "the lord",
    "the almighty", "the most high", "the holy one",
]
_INVERTED_ATTRIBUTION_RE = re.compile(
    r"\b(?:says|said|saith|declares|declared|speaks|spoke)\s+"
    + _alternation(_INVERTED_DIVINE_SPEAKERS) + r"\b"
)

# ==========================================================================
# STRONG SIGNAL 4 — BIBLE BOOK NAME.
#
# A valid Bible book name anywhere in the sentence ("According to Genesis",
# "In Romans we learn", "the book of Isaiah") is independently sufficient
# for a strong Scripture prediction -- no chapter:verse reference, cue, or
# quoting verb required. This is distinct from the REFERENCE signal, which
# detects chapter:verse structures ("Genesis 1:3"); "According to Genesis"
# has no reference but still identifies the biblical source. "book of X"
# constructions are covered by the same word-boundary match on X.
#
# All 66 canonical books are recognized. Books that exist ONLY in numbered
# form (Samuel/Kings/Chronicles/Corinthians/Thessalonians/Timothy/Peter and
# the Johannine epistles) require their number ("1 corinthians",
# "2nd timothy", "third john") -- the bare stems are NOT book names
# ("samuel" the person, "kings" of nations). Matching is a word-boundary
# regex on normalized lowercase text: "john" matches, "johnson" does not.
#
# Deliberately accepted ambiguity (spec requirement): Job/Mark/John/James/
# Acts/Ruth/Numbers are also common words or personal names, so e.g. "I lost
# my job" fires the signal. In the preaching domain a book mention is a
# high-value Scripture indicator, and Stage 4 retrieval scopes candidates to
# the named book -- precision there, recall here.
# ==========================================================================
_BOOKS_BARE = [
    # --- Old Testament (books with unnumbered names) ---
    "genesis", "exodus", "leviticus", "numbers", "deuteronomy", "joshua",
    "judges", "ruth", "ezra", "nehemiah", "esther", "job", "psalm", "psalms",
    "proverbs", "ecclesiastes", "song of solomon", "isaiah", "jeremiah",
    "lamentations", "ezekiel", "daniel", "hosea", "joel", "amos", "obadiah",
    "jonah", "micah", "nahum", "habakkuk", "zephaniah", "haggai",
    "zechariah", "malachi",
    # --- New Testament (books with unnumbered names) ---
    "matthew", "mark", "luke", "john", "acts", "romans", "galatians",
    "ephesians", "philippians", "colossians", "titus", "philemon", "hebrews",
    "james", "jude", "revelation",
]
# Books existing ONLY in numbered form: 1/2 Samuel ... 1/2 Peter.
_BOOKS_NUMBERED_12 = ["samuel", "kings", "chronicles", "corinthians",
                      "thessalonians", "timothy", "peter"]
# John additionally has three epistles: 1/2/3 John (bare "john" = the Gospel,
# already in _BOOKS_BARE).
_BOOK_NAME_RE = re.compile(
    r"\b(?:"
    # Numbered branches first so "1 john" matches whole, not just "john".
    r"(?:[12](?:st|nd)?|first|second)\s+(?:" + "|".join(_BOOKS_NUMBERED_12) + r")"
    r"|(?:[123](?:st|nd|rd)?|first|second|third)\s+john"
    r"|" + _alternation(_BOOKS_BARE)
    + r")\b"
)


class QuoteDetector(ABC):
    """Interface every quote detector implements. Swappable, no pipeline edits."""

    @abstractmethod
    def score(self, text: str) -> float:
        """Return P(text is Scripture being quoted/referenced) in [0, 1]."""

    def __call__(self, text: str) -> float:
        return self.score(text)


class HeuristicQuoteDetector(QuoteDetector):
    """Independent strong-signal classifier (OR architecture, not additive).

    A sentence is a strong Scripture candidate when ANY ONE of four
    independent strong signals fires:

        1. CUE         -- explicit "the Bible says", "it is written", ...
        2. REFERENCE   -- "Romans 8:28", "Psalm 23:1", ...
        3. ATTRIBUTION -- biblical speaker + quoting verb:
                          "Paul writes", "Jesus said", "the prophet says", ...
        4. BOOK NAME   -- any of the 66 canonical books: "According to
                          Genesis", "In Romans we learn", "the book of
                          Isaiah", "1 Corinthians", ...

    One strong signal alone is sufficient for a strong positive score; the
    other signals are NOT required. Multiple strong signals only raise
    confidence slightly. Weak signals (theological vocabulary, quotation
    marks, book mentions, archaic diction, base scores, penalties) are NOT
    part of the primary decision and cannot promote a sentence on their own.

    Tuned via the self-test at the bottom of this file; the whole class is
    replaceable by a trained ONNX classifier.
    """

    def __init__(
        self,
        strong_score: float = 0.92,
        extra_signal_bonus: float = 0.03,
        no_signal_score: float = 0.05,
    ):
        # Score returned when exactly one strong signal fires (>= QUOTE_THRESHOLD).
        self.strong_score = strong_score
        # Small confidence bump per additional strong signal (2 -> +0.03, 3 -> +0.06).
        self.extra_signal_bonus = extra_signal_bonus
        # Flat score when none of the three strong signals is present. Deliberately
        # below the live rescue floor (0.15) and the visibility floor (0.30) so
        # weak signals can never accumulate into a promotion.
        self.no_signal_score = no_signal_score

    def strong_signals(self, text: str) -> dict:
        """Which of the four independent strong signals fired.

        Returns ``{"cue", "reference", "attribution", "book_name"}`` bools.
        ANY one True is sufficient for a strong Scripture prediction; the
        signals never depend on each other.
        """
        t = text or ""
        low = t.lower()
        return {
            "cue": any(cue in low for cue in CUE_PHRASES),
            "reference": bool(_REF_RE.search(t) or _BARE_REF_RE.search(t)),
            "attribution": bool(
                _ATTRIBUTION_RE.search(low) or _INVERTED_ATTRIBUTION_RE.search(low)
            ),
            "book_name": bool(_BOOK_NAME_RE.search(low)),
        }

    def book_name_match(self, text: str) -> str | None:
        """The matched book-name surface form (normalized), for diagnostics."""
        m = _BOOK_NAME_RE.search((text or "").lower())
        return m.group(0) if m else None

    def score(self, text: str) -> float:
        # --- Four independent strong signals. ANY ONE is sufficient. -------
        n_strong = sum(self.strong_signals(text).values())
        if n_strong == 0:
            # No cue, no reference, no biblical attribution: do NOT promote
            # based on theological words, quotation marks, book names,
            # archaic vocabulary, or any other weak signal.
            return self.no_signal_score
        return min(0.99, self.strong_score + self.extra_signal_bonus * (n_strong - 1))


class OnnxQuoteDetector(QuoteDetector):
    """Trained DistilBERT quote classifier via ONNX Runtime.

    Used standalone (``--detector onnx``) or as the ML fallback inside
    HybridQuoteDetector (``--detector hybrid``), where it only sees sentences
    no strong rule fired on. Model/tokenizer paths default to
    ``config.ONNX_MODEL_PATH`` / ``config.ONNX_TOKENIZER_PATH``;
    ``positive_index`` defaults to ``config.ONNX_POSITIVE_INDEX`` (the model
    card declares id2label {0: negative, 1: positive}). The rest of the
    pipeline is unchanged because it only talks to ``QuoteDetector``.
    """

    def __init__(self, model_path: str, tokenizer_path: str, positive_index: int = 1):
        import onnxruntime as ort  # lazy import; not required for the heuristic
        from transformers import AutoTokenizer

        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        self.positive_index = positive_index

    def score(self, text: str) -> float:  # pragma: no cover - depends on a trained model
        import numpy as np
        enc = self.tokenizer(text, padding=True, truncation=True, max_length=128, return_tensors="np")
        inputs = {k: v for k, v in enc.items() if k in [i.name for i in self.session.get_inputs()]}
        logits = self.session.run(None, inputs)[0][0]
        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        pos = float(probs[self.positive_index]) if self.positive_index < len(probs) else float(probs[-1])
        return max(0.0, min(0.99, pos))


class HybridQuoteDetector(QuoteDetector):
    """Strong scripture rules first; trained ONNX classifier as fallback.

    Decision hierarchy (the OR rule architecture is preserved exactly; ONNX
    can never override an explicit strong signal, and no weak heuristic
    signal gates whether ONNX runs):

        PRIORITY 1  CUE         -> strong positive score (ONNX bypassed)
        PRIORITY 2  REFERENCE   -> strong positive score (ONNX bypassed)
        PRIORITY 3  ATTRIBUTION -> strong positive score (ONNX bypassed)
        PRIORITY 4  BOOK NAME   -> strong positive score (ONNX bypassed)
        PRIORITY 5  none fired  -> ONNX model probability decides

    ``fallback=None`` degrades gracefully to pure strong-rules behaviour, so
    the pipeline keeps working when ONNX Runtime or the model files are
    unavailable. The rest of the pipeline only sees ``score()`` -- the
    decision path stays transparent downstream.
    """

    #: Diagnostic ``source`` priority when several strong signals fire at once.
    SOURCE_PRIORITY = ("cue", "reference", "attribution", "book_name")

    def __init__(
        self,
        rules: HeuristicQuoteDetector | None = None,
        fallback: QuoteDetector | None = None,
    ):
        self.rules = rules if rules is not None else HeuristicQuoteDetector()
        self.fallback = fallback

    def score(self, text: str) -> float:
        return self.explain(text)["score"]

    def explain(self, text: str) -> dict:
        """Score + diagnostic: WHY the sentence was classified this way.

        Returns ``{"text", "score", "source", "signals", "matched_book",
        "onnx_used", "prediction"}`` where ``source`` is one of ``"cue"``,
        ``"reference"``, ``"attribution"``, ``"book_name"``, ``"onnx"`` or
        ``"none"`` (no strong signal and no fallback configured).
        ``matched_book`` holds the matched book surface form when the
        BOOK NAME signal fired, else ``None``.
        """
        signals = self.rules.strong_signals(text)
        if any(signals.values()):
            # Strong rule hit -> strong positive; ONNX is NOT consulted.
            source = next(s for s in self.SOURCE_PRIORITY if signals[s])
            score = self.rules.score(text)
            onnx_used = False
        elif self.fallback is not None:
            # No strong signal -> the trained classifier decides.
            source, score, onnx_used = "onnx", self.fallback.score(text), True
        else:
            # No strong signal and no fallback -> pure-rules behaviour.
            source, score, onnx_used = "none", self.rules.score(text), False
        return {
            "text": text,
            "score": score,
            "source": source,
            "signals": signals,
            "matched_book": self.rules.book_name_match(text) if signals["book_name"] else None,
            "onnx_used": onnx_used,
            "prediction": "scripture" if score >= config.QUOTE_THRESHOLD else "not_scripture",
        }


class SemanticQuoteDetector(QuoteDetector):
    """Heuristic + dual-translation concordance pre-check.

    The strongest offline signal that a sentence is *actually a verbatim
    scripture quote* (not just topically related) is **dual-translation
    concordance**: the same verse reference scores highly from BOTH
    translations (AMP + NKJV) in the top-K. A sentence about "the spirit" may
    match many spirit-verses, but only a real quote will match the *same*
    reference from both translations with high similarity.

    Fallback: a very high single-match (>= 0.80) paired with a decent heuristic
    score (>= 0.25) also gets a boost, catching quotes that only appear strongly
    in one translation.

    Still implements ``QuoteDetector``, still runs in Stage 3 before Stage 4
    produces re-rank candidates. The semantic check is a yes/no proximity signal,
    not the top-k candidate retrieval of Stage 4.
    """

    def __init__(
        self,
        base: QuoteDetector,
        retriever,
        search_k: int = 5,
        min_words: int = 3,
        dual_ref_threshold: float = 0.70,
        dual_ref_base: float = 0.40,
        dual_ref_sem_weight: float = 0.50,
        dual_ref_heur_weight: float = 0.10,
        top1_threshold: float = 0.80,
        top1_heuristic_min: float = 0.25,
        top1_base: float = 0.50,
        top1_sem_weight: float = 0.30,
        top1_heur_weight: float = 0.20,
    ):
        self.base = base
        self.retriever = retriever
        self.search_k = search_k
        self.min_words = min_words
        self.dual_ref_threshold = dual_ref_threshold
        self.dual_ref_base = dual_ref_base
        self.dual_ref_sem_weight = dual_ref_sem_weight
        self.dual_ref_heur_weight = dual_ref_heur_weight
        self.top1_threshold = top1_threshold
        self.top1_heuristic_min = top1_heuristic_min
        self.top1_base = top1_base
        self.top1_sem_weight = top1_sem_weight
        self.top1_heur_weight = top1_heur_weight

    def score(self, text: str) -> float:
        heuristic_score = self.base.score(text)
        if len(text.split()) < self.min_words:
            return heuristic_score
        try:
            cands = self.retriever.search_one(text, top_k=self.search_k)
        except Exception:
            return heuristic_score
        if not cands:
            return heuristic_score

        # --- Dual-translation concordance ---
        ref_scores: dict = {}
        for c in cands:
            ref_scores.setdefault(c.key, []).append((c.translation, c.score))
        best_dual = 0.0
        for pairs in ref_scores.values():
            translations = {t for t, _ in pairs}
            if len(translations) >= 2:  # same ref from AMP + NKJV
                avg = sum(s for _, s in pairs) / len(pairs)
                best_dual = max(best_dual, avg)
        if best_dual >= self.dual_ref_threshold:
            boosted = self.dual_ref_base + self.dual_ref_sem_weight * best_dual + self.dual_ref_heur_weight * heuristic_score
            return max(heuristic_score, min(0.99, boosted))

        # --- Fallback: very high single match + decent heuristic ---
        top1 = cands[0].score
        if top1 >= self.top1_threshold and heuristic_score >= self.top1_heuristic_min:
            boosted = self.top1_base + self.top1_sem_weight * top1 + self.top1_heur_weight * heuristic_score
            return max(heuristic_score, min(0.99, boosted))

        return heuristic_score


def get_detector(kind: str = "heuristic", **kwargs) -> QuoteDetector:
    """Factory: returns the requested detector. Default = heuristic stand-in.

    Use ``kind='semantic'`` with ``retriever=...`` to get the
    SemanticQuoteDetector (heuristic + FAISS proximity boost).
    Use ``kind='hybrid'`` to get strong rules with the trained ONNX
    classifier as fallback (ONNX loaded lazily; if unavailable the hybrid
    degrades to strong rules only). Pass ``fallback=<QuoteDetector>`` to
    inject a custom/mock fallback.
    """
    if kind == "heuristic":
        return HeuristicQuoteDetector(**kwargs)
    if kind == "semantic":
        base = kwargs.pop("base", HeuristicQuoteDetector())
        retriever = kwargs.pop("retriever", None)
        if retriever is None:
            raise ValueError("semantic detector requires retriever=...")
        return SemanticQuoteDetector(base, retriever, **kwargs)
    if kind == "hybrid":
        rules = kwargs.pop("rules", None)
        fallback = kwargs.pop("fallback", None)
        if fallback is None:
            # Lazy load: ONNX Runtime is only imported here, only when the
            # hybrid fallback is actually configured.
            model_path = kwargs.pop("model_path", config.ONNX_MODEL_PATH)
            tokenizer_path = kwargs.pop("tokenizer_path", config.ONNX_TOKENIZER_PATH)
            positive_index = kwargs.pop("positive_index", config.ONNX_POSITIVE_INDEX)
            try:
                fallback = OnnxQuoteDetector(
                    model_path=model_path,
                    tokenizer_path=tokenizer_path,
                    positive_index=positive_index,
                )
            except Exception as exc:
                print(f"[quote_detect] hybrid: ONNX fallback unavailable ({exc}); "
                      "running strong rules only", flush=True)
                fallback = None
        return HybridQuoteDetector(rules=rules, fallback=fallback)
    if kind == "onnx":
        return OnnxQuoteDetector(**kwargs)
    raise ValueError(f"unknown quote detector: {kind}")


def filter_quote_windows(windows, detector: QuoteDetector, threshold: float = config.QUOTE_THRESHOLD):
    """Return (passed, all_scored) where passed is [(window, prob)] >= threshold.

    Quote detection scores the *candidate sentence* (``window.candidate_text``),
    not the full prev+current+next window. Scoring the full window lets lookahead
    words bleed a cue phrase from a later sentence into a non-quote sentence
    (false positives) and lets a prior cue bleed into a teaching sentence. The
    full window context is still used downstream for retrieval and re-ranking,
    where extra context genuinely helps a similarity query.
    """
    scored = [(w, detector.score(w.candidate_text)) for w in windows]
    passed = [(w, p) for (w, p) in scored if p >= threshold]
    return passed, scored


if __name__ == "__main__":
    # Self-test / tuning harness. Run: python quote_detect.py
    det = HeuristicQuoteDetector()
    cases = [
        # --- Strong signal 1: CUE alone is sufficient (no reference needed) ---
        ("The Bible says we should love our enemies.", True),
        ("And the bible says, for God so loved the world that he gave his only begotten Son.", True),
        ("As it is written, I will put my Spirit within you and cause you to walk in my statutes.", True),
        ("The Lord says, I will give you a new heart and put a new spirit within you.", True),
        ("According to scripture we are saved by grace through faith.", True),
        # --- Strong signal 2: REFERENCE alone is sufficient (no cue needed) ---
        ("Romans 8:28 says all things work together for good.", True),
        ("Romans 8:28 says all things work together for good to them that love God.", True),
        ("John 3:16.", True),
        ("Turn to 1 Corinthians 13:4 and read it with me.", True),
        # --- Strong signal 3: ATTRIBUTION alone (no reference, no cue, no quotes) ---
        ("Paul writes that the just shall live by faith.", True),
        ("Jesus said forgive those who persecute you.", True),
        ("Isaiah declares that the Lord is our shepherd.", True),
        ("The prophet says the Lord will restore his people.", True),
        ("The psalmist writes that the Lord is my shepherd.", True),
        ("The apostle Paul says we are justified by faith.", True),
        ("I will never leave you nor forsake you, says the Lord.", True),
        # --- Strong signal 4: BOOK NAME alone is sufficient ---------------
        ("According to Genesis, God said let there be light.", True),
        ("In Romans, Paul teaches about justification by faith.", True),
        ("Matthew records the words of Jesus.", True),
        ("According to Revelation, there will be a new heaven and new earth.", True),
        ("The book of Isaiah says the Lord is holy.", True),
        ("According to 1 Corinthians, love is patient.", True),
        ("Paul writes in 2 Corinthians about the new creation.", True),
        ("We have been studying Second Timothy this month.", True),
        # book_name as the ONLY firing signal (no cue, no reference, no verb):
        ("In Romans we learn about faith.", True),
        ("Revelation describes a new heaven and a new earth.", True),
        ("The book of Genesis tells us about creation.", True),
        # --- Weak theological vocabulary must NOT promote a sentence ---
        ("We should have faith, hope and love.", False),
        ("Christians should pursue righteousness and holiness.", False),
        ("The sermon was about salvation and grace.", False),
        # --- Generic quoting verbs are NOT Scripture attribution ---
        ("My teacher says mathematics is difficult.", False),
        ("The news says prices are rising.", False),
        # --- Word-boundary safety: substrings of book names must NOT match --
        ("My neighbor Johnson came over for dinner.", False),
        ("He markedly improved his serve this season.", False),
        # --- Non-scripture stays negative ---
        ("So last week we went to the lake and the kids had a great time swimming.", False),
        ("Knock knock, who's there, a pastor with a long sermon.", False),
        ("Breaking news: the stock market closed higher today after the report.", False),
        ("I think we should all try to be kinder to each other this week.", False),
        # --- Documented ambiguity (spec: ambiguous book names STAY strong) --
        # These flipped from FALSE in the previous suite: a bare book-name
        # mention is now an independent strong signal. Accepted trade-off for
        # the preaching domain; Stage 4 retrieval scopes to the named book.
        ("John says he will arrive tomorrow.", True),        # book "john"
        ("Turn in your Bibles to Ezekiel chapter thirty six.", True),  # book "ezekiel"
        ("I lost my job last year.", True),                  # book "job"
        ("The acts of kindness moved us.", True),            # book "acts"
        # --- Documented recall gap: verbatim quotes with no cue, no reference
        #     and no attribution are intentionally NOT promoted by the
        #     heuristic (the semantic detector's dual-translation concordance
        #     or the trained ONNX classifier is the rescue path for these) ---
        ("Behold, the Lamb of God, which taketh away the sin of the world.", False),
        ("I will put my spirit within you and cause you to walk in my statutes.", False),
    ]
    print(f"{'PASS?':5} {'prob':>5}  sentence")
    ok = True
    for text, expected in cases:
        p = det.score(text)
        pred = p >= config.QUOTE_THRESHOLD
        flag = "OK" if pred == expected else "XX"
        if pred != expected:
            ok = False
        print(f"{flag:5} {p:5.2f}  {text[:70]}")
    print("\nALL OK" if ok else "\nSOME MISMATCHES (review above)")

    # --- Coverage: every one of the 66 canonical books must fire book_name --
    ALL_66_BOOKS = [
        "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua",
        "Judges", "Ruth", "1 Samuel", "2 Samuel", "1 Kings", "2 Kings",
        "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah", "Esther", "Job",
        "Psalms", "Proverbs", "Ecclesiastes", "Song of Solomon", "Isaiah",
        "Jeremiah", "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel",
        "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah",
        "Haggai", "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John",
        "Acts", "Romans", "1 Corinthians", "2 Corinthians", "Galatians",
        "Ephesians", "Philippians", "Colossians", "1 Thessalonians",
        "2 Thessalonians", "1 Timothy", "2 Timothy", "Titus", "Philemon",
        "Hebrews", "James", "1 Peter", "2 Peter", "1 John", "2 John",
        "3 John", "Jude", "Revelation",
    ]
    assert len(ALL_66_BOOKS) == 66
    missing = [b for b in ALL_66_BOOKS
               if not det.strong_signals(f"We read from {b} today.")["book_name"]]
    print(f"[coverage] canonical books detected: {len(ALL_66_BOOKS) - len(missing)}/66"
          + (f"  MISSING: {missing}" if missing else ""))
    ok = ok and not missing

    # =====================================================================
    # Hybrid detector tests: strong rules must BYPASS the ONNX fallback;
    # the ONNX model only decides when NO strong signal fires.
    # =====================================================================
    class MockOnnxDetector(QuoteDetector):
        """Test double for the ONNX fallback: fixed probability, records calls."""

        def __init__(self, prob: float):
            self.prob = prob
            self.calls = 0

        def score(self, text: str) -> float:
            self.calls += 1
            return self.prob

    def check(cond: bool, label: str):
        nonlocal_ok[0] = nonlocal_ok[0] and cond
        print(f"{'OK' if cond else 'XX':5} {label}")

    nonlocal_ok = [True]
    print("\n== hybrid: strong rules bypass a hostile ONNX (mock = 0.05) ==")
    mock = MockOnnxDetector(0.05)
    hyb = HybridQuoteDetector(fallback=mock)
    bypass_cases = [
        ("The Bible says we should forgive.", "cue"),
        ("Romans 8:28 says all things work together for good.", "reference"),
        # "paul writes" is BOTH a cue phrase and attribution; priority order
        # cue > reference > attribution reports "cue". What matters: the
        # sentence is Scripture and ONNX is never consulted.
        ("Paul writes that the just shall live by faith.", "cue"),
        # book_name as the ONLY firing signal:
        ("In Romans we learn about faith.", "book_name"),
        ("According to 1 Corinthians, love is patient.", "book_name"),
    ]
    for text, expected_source in bypass_cases:
        before = mock.calls
        info = hyb.explain(text)
        check(info["prediction"] == "scripture" and info["score"] >= config.QUOTE_THRESHOLD,
              f"scripture despite mock=0.05 (score={info['score']:.2f})  {text[:55]}")
        check(info["source"] == expected_source,
              f"source == {expected_source!r} (got {info['source']!r})  {text[:55]}")
        check(mock.calls == before and not info["onnx_used"],
              f"ONNX bypassed (calls unchanged)  {text[:55]}")

    # BOOK NAME independence + diagnostics (spec: source=book_name,
    # matched_book, ONNX must not override the strong rule).
    info_bk = hyb.explain("In Romans we learn about faith.")
    check(info_bk["signals"] == {"cue": False, "reference": False,
                                 "attribution": False, "book_name": True},
          "book_name fires independently (the only strong signal)")
    check(info_bk["matched_book"] == "romans",
          f"matched_book == 'romans' (got {info_bk['matched_book']!r})")
    info_gn = hyb.explain("According to Genesis, God said let there be light.")
    check(info_gn["prediction"] == "scripture" and not info_gn["onnx_used"]
          and info_gn["signals"]["book_name"] and info_gn["matched_book"] == "genesis",
          f"Genesis book_name strong despite mock=0.05 "
          f"(score={info_gn['score']:.2f}, source={info_gn['source']!r} -- "
          "'god said' is also a cue, and cue has diagnostic priority)")

    print("\n== hybrid: ONNX fallback decides when no strong signal fires ==")
    verbatim = "I will put my spirit within you and cause you to walk in my statutes."
    mock_hi = MockOnnxDetector(0.92)
    info_hi = HybridQuoteDetector(fallback=mock_hi).explain(verbatim)
    check(info_hi["prediction"] == "scripture" and abs(info_hi["score"] - 0.92) < 1e-9
          and info_hi["source"] == "onnx" and mock_hi.calls == 1,
          f"mock=0.92 -> scripture via onnx (score={info_hi['score']:.2f})  {verbatim[:55]}")
    mock_lo = MockOnnxDetector(0.20)
    info_lo = HybridQuoteDetector(fallback=mock_lo).explain(verbatim)
    check(info_lo["prediction"] == "not_scripture" and abs(info_lo["score"] - 0.20) < 1e-9
          and info_lo["source"] == "onnx" and mock_lo.calls == 1,
          f"mock=0.20 -> not scripture via onnx (score={info_lo['score']:.2f})  {verbatim[:55]}")
    # Weak theological vocabulary must NOT gate or replace the ONNX fallback.
    weak = "We should have faith, hope and love."
    mock_w = MockOnnxDetector(0.05)
    info_w = HybridQuoteDetector(fallback=mock_w).explain(weak)
    check(info_w["prediction"] == "not_scripture" and info_w["source"] == "onnx"
          and mock_w.calls == 1,
          f"weak vocab still routes to onnx (score={info_w['score']:.2f})  {weak[:55]}")

    print("\n== hybrid: no ONNX configured -> pure strong-rules behaviour ==")
    rules_only = HybridQuoteDetector(fallback=None)
    info_r1 = rules_only.explain("The Bible says we should love our enemies.")
    info_r2 = rules_only.explain(verbatim)
    check(info_r1["prediction"] == "scripture" and info_r1["source"] == "cue",
          f"strong rule still fires without onnx (score={info_r1['score']:.2f})")
    check(info_r2["prediction"] == "not_scripture" and info_r2["source"] == "none",
          f"no signal + no onnx -> not scripture (score={info_r2['score']:.2f})")

    mock_lo = MockOnnxDetector(0.20)
    info_lo = HybridQuoteDetector(fallback=mock_lo).explain(verbatim)
    check(info_lo["prediction"] == "not_scripture" and abs(info_lo["score"] - 0.20) < 1e-9
          and info_lo["source"] == "onnx" and mock_lo.calls == 1,
          f"mock=0.20 -> not scripture via onnx (score={info_lo['score']:.2f})  {verbatim[:55]}")
    # Weak-vocab sentence routes to ONNX too (no gating).
    weak = "We should have faith, hope and love."
    mock_w = MockOnnxDetector(0.05)
    info_w = HybridQuoteDetector(fallback=mock_w).explain(weak)
    check(info_w["prediction"] == "not_scripture" and info_w["source"] == "onnx"
          and mock_w.calls == 1,
          f"weak vocab still routes to onnx (score={info_w['score']:.2f})  {weak[:55]}")

    print("\n== hybrid: no ONNX configured -> pure strong-rules behaviour ==")
    rules_only = HybridQuoteDetector(fallback=None)
    info_r1 = rules_only.explain("The Bible says we should love our enemies.")
    info_r2 = rules_only.explain(verbatim)
    check(info_r1["prediction"] == "scripture" and info_r1["source"] == "cue",
          f"strong rule still fires without onnx (score={info_r1['score']:.2f})")
    check(info_r2["prediction"] == "not_scripture" and info_r2["source"] == "none",
          f"no signal + no onnx -> not scripture (score={info_r2['score']:.2f})")

    # Word-boundary safety: ambiguous names must not false-positive from
    # substrings.  Verify the regex rejects substrings and only matches whole
    # book names:
    check(not _BOOK_NAME_RE.search("johnson"), "johnson must not match 'john'")
    check(not _BOOK_NAME_RE.search("markedly"), "markedly must not match 'mark'")
    check(_BOOK_NAME_RE.search("john"), "john should match (standalone)")
    check(_BOOK_NAME_RE.search("1 corinthians"), "1 corinthians should match")
    check(_BOOK_NAME_RE.search("romans"), "romans should match")

    print("\nHYBRID ALL OK" if nonlocal_ok[0] else "\nHYBRID SOME MISMATCHES (review above)")
    ok = ok and nonlocal_ok[0]
