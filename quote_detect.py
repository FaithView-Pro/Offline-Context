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

Known limitation (documented): the heuristic relies on explicit cue phrases,
scripture reference patterns, quotation markers, and archaic/verbatim-scripture
vocabulary. A verbatim quote in fully modern English with no cue and no
reference may score below threshold until the trained ONNX classifier is wired
in. In practice preachers usually introduce quotes with a cue ("the Bible
says", "Paul writes", "it is written") or read the reference first, so recall
is reasonable; precision is the priority for the offline decision engine.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

import config


# --- Cue phrases: explicit "I am quoting scripture" markers ----------------
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

# --- Quoting verbs that, next to a reference/cue, reinforce a quote ---------
QUOTING_VERBS = ["says", "said", "writes", "wrote", "tells us", "told us",
                 "declares", "states", "reads", "reminds us", "promises"]

# --- Archaic / verbatim-scripture lexicon (strong signal of a direct quote) -
ARCHAIC_STRONG = {
    "thou", "thee", "thy", "thine", "ye", "shalt", "hath", "doth", "saith",
    "whosoever", "believeth", "heareth", "cometh", "goeth", "knoweth", "seeth",
    "giveth", "taketh", "bringeth", "sendeth", "saveth", "raiseth", "lovest",
    "beholdest", "behold", "lo", "verily", "lest", "wherefore", "unto",
    "thereon", "therein", "thereof", "whereby", "begotten", "whence",
    "hither", "thither",
}

# --- Theological lexicon (weaker signal; common in general teaching too) ----
THEOLOGICAL = {
    "righteousness", "iniquity", "transgression", "salvation", "covenant", "mercy",
    "grace", "glory", "atonement", "redemption", "sanctify", "sanctuary",
    "offering", "sacrifice", "propitiation", "reconciliation", "justification",
    "faithful", "wicked", "righteous", "holiness", "anointed", "blessed",
    "abomination", "shepherd", "altar", "tabernacle", "priest", "prophet",
    "apostle", "disciple", "gentiles", "messiah", "repent", "repentance",
    "forgiveness", "kingdom", "heaven", "resurrection", "eternal", "immortal",
    "commandments", "statutes", "precepts", "testimony", "tribulation",
    "persecution", "affliction", "consolation", "lovingkindness", "steadfast",
    "prophecy", "revelation", "exalt", "exalted", "humble", "meek",
    # added after real-audio validation: very common scripture terms
    "spirit", "soul", "flesh", "heart", "blood", "cross", "sin", "sins",
    "sinner", "judgment", "wrath", "light", "darkness", "life", "death",
    "fear", "love", "hope", "peace", "joy", "faith", "prayer", "pray",
    "angel", "angels", "devil", "demon", "evil", "good", "holy", "lord",
    "god", "christ", "jesus", "spirit", "breath", "walk", "ways", "path",
    "vineyard", "harvest", "seed", "sow", "reap", "vine", "branch", "root",
    "stone", "rock", "mountain", "river", "water", "fire", "wind", "storm",
    "captivity", "exile", "wilderness", "desert", "jordan", "jerusalem",
    "zion", "bethlehem", "nazareth", "galilee", "egypt", "babylon",
    "lawlessness", "lawless", "depart", "declare", "declares", "declared",
    "evildoers", "prophesy", "prophesied", "miracles", "cast", "demons",
    "mighty", "works", "enter", "heaven", "father", "will", "son",
}

# --- Non-scripture markers (joke / news) used as a down-weight --------------
NEG_MARKERS = [
    "knock knock", "breaking news", "according to reports", "reported that",
    "once upon a time", "did you hear the one", "a guy walks into",
    "stock market", "the president said today", "trending on",
]

# Distinctive Bible book names (subset that rarely appears in non-biblical text)
BOOK_NAMES = {
    "genesis", "exodus", "leviticus", "numbers", "deuteronomy", "joshua",
    "judges", "ruth", "samuel", "kings", "chronicles", "ezra", "nehemiah",
    "esther", "psalm", "psalms", "proverbs", "ecclesiastes", "song of solomon",
    "isaiah", "jeremiah", "lamentations", "ezekiel", "daniel", "hosea", "joel",
    "amos", "obadiah", "jonah", "micah", "nahum", "habakkuk", "zephaniah",
    "haggai", "zechariah", "malachi", "matthew", "mark", "luke", "john",
    "acts", "romans", "corinthians", "galatians", "ephesians", "philippians",
    "colossians", "thessalonians", "timothy", "titus", "philemon", "hebrews",
    "james", "peter", "jude", "revelation",
}

# --- Divine-speech / scripture-content patterns (no cue phrase needed) ----
DIVINE_PATTERNS = [
    "i will put my spirit", "i will give you a new heart", "i will write my laws",
    "i will be your god", "you shall be my people", "i am the lord",
    "i am the lord your god", "you shall have no other gods",
    "i will make you", "i will bring you", "i will lead you",
    "i will pour out my spirit", "i will remove your heart of stone",
    "i will give you a heart of flesh", "cause you to walk", "walk in my statutes",
    "keep my judgments", "keep my commandments", "keep my statutes",
    "you shall love the lord", "you shall love your neighbor",
    "the lord is my shepherd", "the lord is my rock", "the lord is my light",
    "in the beginning", "let there be", "thus says the lord",
    "behold i am", "verily i say", "truly i say", "i tell you the truth",
    "whoever believes", "whosoever believeth", "for god so loved",
    "the word became flesh", "in him was life", "the light shines in the darkness",
    "love your enemies", "pray for those", "turn the other cheek",
    "seek first the kingdom", "your kingdom come", "your will be done",
    "on earth as it is in heaven", "our father who art", "hallowed be your name",
    "forgive us our debts", "lead us not into temptation", "deliver us from evil",
    "i am the way", "i am the truth", "i am the life", "i am the door",
    "i am the good shepherd", "i am the resurrection", "i am the vine",
    "i am the bread of life", "i am the light of the world",
    "the spirit of the lord", "the spirit of god", "filled with the spirit",
    "baptized with the spirit", "gifts of the spirit", "fruit of the spirit",
    "the grace of our lord", "the communion of the holy spirit",
    "faith comes by hearing", "the just shall live by faith",
    "all have sinned", "the wages of sin", "the gift of god is eternal life",
    "nothing can separate us", "if god is for us", "more than conquerors",
    "i can do all things", "rejoice in the lord always",
    "the peace of god which surpasses", "whatever is true whatever is noble",
    "i have fought the good fight", "i have kept the faith",
    "there is therefore now no condemnation", "the law of the spirit of life",
    "depart from me", "you who practice", "you who work", "you workers of",
    "practice lawlessness", "i never knew you", "i will declare to them",
    "i will declare", "not everyone who says to me", "lord lord",
    "enter the kingdom of heaven", "on that day many will say",
    "did we not prophesy in your name", "cast out demons in your name",
    "i declare to you", "away from me",
]

# Matches "Romans 8:28", "1 John 4:8", "III John 1:4", "Psalm 23"
_REF_RE = re.compile(
    r"\b(?:[1-3]\s+|[IV]+\s+|First\s+|Second\s+|Third\s+)?"
    r"(?:[A-Z][a-z]+(?:\s+(?:of\s+)?[A-Z][a-z]+)?)\s+\d{1,3}:\d{1,3}\b"
)
# Matches a bare "chapter:verse" token like "8:28"
_BARE_REF_RE = re.compile(r"\b\d{1,3}\s*:\s*\d{1,3}\b")
_QUOTE_RE = re.compile(r'["\u201c\u201d].+?["\u201c\u201d]')


class QuoteDetector(ABC):
    """Interface every quote detector implements. Swappable, no pipeline edits."""

    @abstractmethod
    def score(self, text: str) -> float:
        """Return P(text is Scripture being quoted/referenced) in [0, 1]."""

    def __call__(self, text: str) -> float:
        return self.score(text)


class HeuristicQuoteDetector(QuoteDetector):
    """Rule-based stand-in for when no labeled training data exists yet.

    Combines explicit cue phrases, scripture reference patterns, quotation
    markers, archaic/verbatim-scripture vocabulary density, and theological-term
    density into a calibrated probability. Tuned via the self-test at the bottom
    of this file; the whole class is replaceable by a trained ONNX classifier.
    """

    def __init__(
        self,
        cue_weight: float = 0.55,
        ref_weight: float = 0.55,
        archaic_cap: float = 0.45,
        archaic_marker_bonus: float = 0.15,
        theo_cap: float = 0.25,
        quote_marker_weight: float = 0.08,
        book_mention_weight: float = 0.06,
        quoting_verb_bonus: float = 0.07,
        neg_penalty: float = 0.25,
        base: float = 0.10,
        divine_pattern_weight: float = 0.60,
    ):
        self.cue_weight = cue_weight
        self.ref_weight = ref_weight
        self.archaic_cap = archaic_cap
        self.archaic_marker_bonus = archaic_marker_bonus
        self.theo_cap = theo_cap
        self.quote_marker_weight = quote_marker_weight
        self.book_mention_weight = book_mention_weight
        self.quoting_verb_bonus = quoting_verb_bonus
        self.neg_penalty = neg_penalty
        self.base = base
        self.divine_pattern_weight = divine_pattern_weight

    def score(self, text: str) -> float:
        t = text or ""
        low = t.lower()
        toks = re.findall(r"[a-z']+", low)
        n = max(1, len(toks))

        has_cue = any(cue in low for cue in CUE_PHRASES)
        has_ref = bool(_REF_RE.search(t) or _BARE_REF_RE.search(t))
        has_quote = bool(_QUOTE_RE.search(t))
        has_book = any(b in low for b in BOOK_NAMES) and not has_ref
        has_neg = any(m in low for m in NEG_MARKERS)
        has_divine = any(p in low for p in DIVINE_PATTERNS)

        archaic_hits = sum(1 for w in toks if w in ARCHAIC_STRONG)
        theo_hits = sum(1 for w in toks if w in THEOLOGICAL)
        archaic_ratio = archaic_hits / n
        theo_ratio = theo_hits / n

        archaic_score = min(self.archaic_cap, archaic_ratio * 3.0)
        if archaic_hits > 0:
            archaic_score += self.archaic_marker_bonus
        theo_score = min(self.theo_cap, theo_ratio * 1.5)

        has_quoting_verb = any(v in low for v in QUOTING_VERBS)
        verb_bonus = self.quoting_verb_bonus if (has_quoting_verb and (has_ref or has_cue or has_quote)) else 0.0

        s = self.base
        if has_cue:
            s += self.cue_weight
        if has_ref:
            s += self.ref_weight
        if has_book:
            s += self.book_mention_weight
        if has_quote:
            s += self.quote_marker_weight
        if has_divine:
            s += self.divine_pattern_weight
        s += archaic_score + theo_score + verb_bonus
        if has_neg:
            s -= self.neg_penalty

        return max(0.0, min(0.99, s))


class OnnxQuoteDetector(QuoteDetector):
    """Slot for a trained MiniLM/DistilBERT/TinyBERT classifier via ONNX Runtime.

    Not active by default (no labeled data yet). To activate: train a model,
    export to ONNX, then construct this class with the model + tokenizer paths
    and pass it to the pipeline via ``run.py --quote-detector onnx``. The rest
    of the pipeline is unchanged because it only talks to ``QuoteDetector``.
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
    """
    if kind == "heuristic":
        return HeuristicQuoteDetector(**kwargs)
    if kind == "semantic":
        base = kwargs.pop("base", HeuristicQuoteDetector())
        retriever = kwargs.pop("retriever", None)
        if retriever is None:
            raise ValueError("semantic detector requires retriever=...")
        return SemanticQuoteDetector(base, retriever, **kwargs)
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
        ("And the bible says, for God so loved the world that he gave his only begotten Son.", True),
        ("As it is written, I will put my Spirit within you and cause you to walk in my statutes.", True),
        ("Romans 8:28 says all things work together for good to them that love God.", True),
        ("The Lord says, I will give you a new heart and put a new spirit within you.", True),
        ("Behold, the Lamb of God, which taketh away the sin of the world.", True),
        ("So last week we went to the lake and the kids had a great time swimming.", False),
        ("Knock knock, who's there, a pastor with a long sermon.", False),
        ("Breaking news: the stock market closed higher today after the report.", False),
        ("I think we should all try to be kinder to each other this week.", False),
        ("Turn in your Bibles to Ezekiel chapter thirty six.", False),
        ("I will put my spirit within you and cause you to walk in my statutes.", True),
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
