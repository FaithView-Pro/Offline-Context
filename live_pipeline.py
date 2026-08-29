"""Milestone 3 -- LIVE PIPELINE ORCHESTRATOR (no server yet).

Wires the live transcription path to the existing offline detection stages:

    live_transcribe  ->  buffer (UNCHANGED)  ->  intent_router
        | EXPLICIT_REF / NAV_COMMAND / CLEAR_COMMAND -> direct lookup or display action
        | CUE_PHRASE -> quote_detect (ONNX) -> retrieve -> rerank -> score

Emits three kinds of things to a callback, so a later server (Milestone 4) or a
terminal listener (this file's ``__main__``) can react:

  * ``on_transcript(Segment)`` -- every transcribed sentence (rolling caption)
  * ``on_detection(DetectionEvent)`` -- a resolved scripture detection (CUE path)
  * ``on_display(DisplayEvent)`` -- whatever should be shown / cleared on the live
    display, driven by EXPLICIT_REF / NAV_COMMAND / CLEAR_COMMAND

``DetectionEvent`` matches the dataclass shape the brief specifies exactly. The
session state ``current_reference`` lives in the router's :class:`SessionState`
and is updated every time ANY verse is displayed (explicit ref, nav result, or
an OPERATOR-CONFIRMED presented detection) -- never silently on a low-confidence
auto-detection, because a guessed verse is not a verse the operator chose to
show.

Fully offline: model loaders honor ``config.OFFLINE`` and the HF_HUB_OFFLINE
conventions already used elsewhere in this repo. No network call in the live
path.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, asdict, field
from typing import Optional, Callable, Any

import config
import score as score_mod
import rerank as rerank_mod
import quote_detect
import intent_router
import bible_db
from context import SermonContext
from transcribe import Segment
from buffer import buffer_segments, Window
from retrieve import Retriever
from live_transcribe import AudioSource, WhisperTranscriptionSource
from transcription_source import TranscriptionSource


@dataclass
class DetectionEvent:
    reference: str
    translation: str
    text: str
    confidence: float          # 0-1
    confidence_band: str       # from score.py
    transcript_snippet: str
    timestamp: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DisplayEvent:
    """What (the pipeline believes) should be on the live display right now."""
    clear: bool = False
    reference: Optional[str] = None
    translation: Optional[str] = None
    text: Optional[str] = None
    # provenance so the UI / server can label the source
    source: str = "cue"        # "explicit_ref" | "nav" | "clear" | "queue_present" | "cue"

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


class LivePipeline:
    """The end-to-end live scripture-detection pipeline (standalone, no server).

    Pure orchestration: consumes the unmodified Stage modules for the CUE path,
    uses :mod:`intent_router` for direct-ref/nav/clear, and keeps the single
    source of truth for "what verse is currently shown" in
    :attr:`session.current_reference`.
    """

    def __init__(
        self,
        on_transcript: Optional[Callable[[Segment], None]] = None,
        on_detection: Optional[Callable[[DetectionEvent], None]] = None,
        on_display: Optional[Callable[[DisplayEvent], None]] = None,
        *,
        whisper_model: str = config.WHISPER_MODEL,      # small.en default (see Task 1)
        # small.en beam=1 on 10s tile = ~3.65s Whisper time measured, leaving ~4.35s
        # for buffer → ONNX → FAISS → rerank → score. The 8s window was explicitly
        # widened from 4s to create that margin (benchmark_live_chunk.py).
        chunk_seconds: float = 8.0,
        overlap_seconds: float = 2.0,
        quote_detector_kind: str = "heuristic",     # "hybrid" | "onnx" | "heuristic" | "semantic"
        quote_threshold: float = config.QUOTE_THRESHOLD,
        top_k: int = config.TOP_K,
        live_confidence_floor: float = 0.45,        # surface >= this in live mode
        default_translation: str = "NKJV",
        offline: bool = config.OFFLINE,
        settings: Optional[config.Settings] = None,
        whisper_initial_prompt: Optional[str] = None,
        condition_on_previous_text: bool = False,
        preprocess: bool = True,
        transcription_source_type: str = "whisper",
        mode: str = "semi_autopilot",
    ):
        self.on_transcript = on_transcript or (lambda s: None)
        self.on_detection = on_detection or (lambda e: None)
        self.on_display = on_display or (lambda e: None)
        self.s = settings or config.Settings()
        self.s.whisper_model = whisper_model
        self.s.quote_threshold = quote_threshold
        self.s.top_k = top_k
        self.s.offline = offline

        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds
        self.quote_threshold = quote_threshold
        self.top_k = top_k
        self.live_confidence_floor = live_confidence_floor
        self.default_translation = default_translation
        self.offline = offline
        # Whisper live-accuracy knobs (see live_transcribe.LiveTranscriber):
        #   whisper_initial_prompt -- None => fall back to config.WHISPER_INITIAL_PROMPT
        #     at use-time so the CLI default is consistent with the unit-test path.
        #   condition_on_previous_text -- False by default for live chunked ASR.
        #   preprocess -- high-pass mic cleanup on by default.
        self.whisper_initial_prompt = whisper_initial_prompt
        self.condition_on_previous_text = condition_on_previous_text
        self.preprocess = preprocess

        # --- Live-mode recall rescue -------------------------------------------------
        # The trained ONNX quote classifier is calibrated for *cue-introduced*
        # scripture ("the bible says..."), so a bare verbatim quote read aloud
        # ("I will put my spirit within you...") scores ~0 even though it IS
        # scripture. The batch engine accepts that precision tradeoff, but a
        # LIVE operator console benefits from also surfacing verbatim reads.
        # So: when ONNX < threshold, if a cheap heuristic says "plausibly
        # scripture" AND retrieval's top-1 cosine is strong, still emit a
        # DetectionEvent. This does NOT touch quote_detect/retrieve/rerank --
        # it's live-mode orchestration only, and it's configurable/offable.
        self.live_rescue = True
        self.rescue_heuristic_floor = 0.15
        self.rescue_min_words = 3
        self._rescue_heur = None

        self.session = intent_router.SessionState()
        self.ctx = SermonContext()
        self.accepted_keys: set[str] = set()
        self.accepted_books: dict = {}

        self.quote_detector_kind = quote_detector_kind
        self._detector = None
        self._retriever = None
        self._db = None
        self._transcription_source: Optional[TranscriptionSource] = None
        self._transcription_source_type = transcription_source_type
        self.mode = mode  # "autopilot" | "semi_autopilot" | "manual"
        self._running_windows: list[Window] = []   # accumulating live windows
        self._processed_windows: set[int] = set()  # window.index already routed
        self._running_windows_accum: list[Segment] = []
        self._cue_transcribing: bool = True  # False when manual mode pauses

        # --- Minimal CUE accumulator: joins text across segment boundaries ---
        # When a cue-phrase window opens, subsequent CUE windows (even after
        # a transcription pause) extend the candidate text so retrieval sees
        # the full spoken quote, not just the first fragment.
        self._cue_accum = None  # {"full": str, "candidate": str, "win": Window} or None
        # trigger-starter patterns: if a new CUE window starts with one of
        # these while accumulator is open, it signals a fresh quote
        self._cue_new_trigger_re: re.Pattern = re.compile(
            r"^\s*(the bible says|scripture says|it is written|"
            r"paul writes|paul says|the word says|as it is written|"
            r"jesus said|the lord says|thus saith the lord|"
            r"the scripture says|the psalmist says|david writes|"
            r"moses writes|the prophet says|god says|and it says|"
            r"and paul writes|for it is written|in the book of)\b",
            re.IGNORECASE,
        )
        self._cue_accum_word_cap: int = 35  # generous safety valve

    # ---- lazy resource setup (shared by live mic + file dry-run) ----
    def _ensure_resources(self):
        import os
        if self.offline:
            for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
                os.environ.setdefault(k, "1")
        if self._db is None:
            self._db = bible_db.get_bible_db()
        if self._retriever is None:
            import models
            embedder = models.get_embedder(offline=self.offline)
            self._retriever = Retriever.from_disk(
                embedder,
                index_path=os.path.join(self.s.index_dir, "verses.faiss"),
                meta_path=os.path.join(self.s.index_dir, "verses_meta.json"),
            )
        if self._detector is None:
            self._build_detector(self.quote_detector_kind)

    def _build_detector(self, kind: str):
        kind = kind or "hybrid"
        if kind == "onnx":
            try:
                self._detector = quote_detect.get_detector(
                    "onnx",
                    model_path=config.ONNX_MODEL_PATH,
                    tokenizer_path=config.ONNX_TOKENIZER_PATH,
                    positive_index=config.ONNX_POSITIVE_INDEX,
                )
                print("[pipeline] ONNX quote detector loaded", flush=True)
            except Exception as exc:
                print(f"[pipeline] ONNX load failed ({exc}); falling back to heuristic", flush=True)
                self._detector = quote_detect.get_detector("heuristic")
        elif kind == "hybrid":
            # Strong rules first (cue/reference/attribution), ONNX as fallback;
            # degrades to strong rules only if ONNX is unavailable.
            self._detector = quote_detect.get_detector("hybrid")
            print("[pipeline] hybrid quote detector loaded (strong rules + ONNX fallback)", flush=True)
        elif kind == "semantic":
            self._detector = quote_detect.get_detector("semantic", retriever=self._retriever)
        else:
            self._detector = quote_detect.get_detector("heuristic")

    # =================================================================
    # Main live control
    # =================================================================
    def start(self, source: AudioSource, quote_detector_kind: str = "hybrid"):
        self.quote_detector_kind = quote_detector_kind
        self._ensure_resources()
        self._start_transcription_source(source)

    def _start_transcription_source(self, audio_source: AudioSource):
        if self._transcription_source is not None:
            try:
                self._transcription_source.stop()
            except Exception:
                pass

        prompt = self.whisper_initial_prompt if self.whisper_initial_prompt is not None \
            else self.s.whisper_initial_prompt

        if self._transcription_source_type == "deepgram":
            self._transcription_source = self._build_deepgram_source()
        else:
            self._transcription_source = self._build_whisper_source(prompt)

        self._transcription_source.start(audio_source, self._on_new_segment,
                                          on_interim=self._on_interim_update)

    def _build_whisper_source(self, prompt: Optional[str] = None) -> WhisperTranscriptionSource:
        return WhisperTranscriptionSource(
            model_name=self.s.whisper_model,
            chunk_seconds=self.chunk_seconds,
            overlap_seconds=self.overlap_seconds,
            language=self.s.whisper_language,
            beam_size=self.s.whisper_beam_size,
            vad_filter=self.s.whisper_vad_filter,
            offline=self.offline,
            initial_prompt=prompt,
            condition_on_previous_text=self.condition_on_previous_text,
            preprocess=self.preprocess,
        )

    def _build_deepgram_source(self):
        from deepgram_transcribe import DeepgramSource
        return DeepgramSource()

    def set_transcription_source(self, source_type: str, audio_source: AudioSource) -> str:
        """Hot-swap the active transcription source live.

        Stops the current source, creates a new one of ``source_type``
        ("whisper" | "deepgram"), wires it into the same segment callback, and
        starts it against ``audio_source``.

        Returns the name of the now-active source.
        """
        self._transcription_source_type = source_type
        self._start_transcription_source(audio_source)
        return self._transcription_source.name if self._transcription_source else source_type

    def set_mode(self, mode: str) -> None:
        """Set operator mode: "autopilot", "semi_autopilot", or "manual"."""
        self.mode = mode

    def pause_transcription(self) -> None:
        """Pause the active transcription source (used by Manual mode)."""
        self._cue_transcribing = False
        if self._transcription_source:
            try:
                self._transcription_source.stop()
            except Exception:
                pass

    def resume_transcription(self, audio_source: AudioSource) -> None:
        """Resume transcription after a pause."""
        self._cue_transcribing = True
        self._start_transcription_source(audio_source)

    def stop(self):
        if self._transcription_source:
            self._transcription_source.stop()

    # Async feed: each freshly transcribed Segment arrives here
    def _on_new_segment(self, seg: Segment):
        if not self._cue_transcribing:
            return
        # rolling transcript callback
        try:
            self.on_transcript(seg)
        except Exception as exc:
            print(f"[pipeline] on_transcript error: {exc}")

        # buffer.feed works on a LIST of segments; we rebuild windows from the
        # running buffer. Buffer.py is unchanged and stateless per call, so we
        # just accumulate segments and re-buffer each tick (cheap: it's just
        # sentence splitting). We only route NEW candidate windows.
        self._running_windows_accum.append(seg)
        windows = buffer_segments(self._running_windows_accum)
        for w in windows:
            if w.index in self._processed_windows:
                continue
            self._processed_windows.add(w.index)
            self._route_window(w)

    def _on_interim_update(self, seg: Segment):
        """Handle interim (non-final) transcript from Deepgram.

        Interims are progressive refinements of the SAME utterance, not new
        utterances.  We update the display and the CUE accumulator in place
        so that Scripture detection sees the latest/best text.

        FAISS search is NOT run on interims — only complete sentences
        (ending with . ! ?) trigger retrieval.
        """
        if not self._cue_transcribing:
            return
        # 1. Update the live caption display
        try:
            self.on_transcript(seg)
        except Exception as exc:
            print(f"[pipeline] on_transcript error: {exc}")

        # 2. Update the CUE accumulator with the latest interim text.
        #    The accumulator holds the evolving utterance; interims REPLACE
        #    the candidate text rather than appending.
        #    No FAISS search here — wait for a complete sentence.
        candidate_text = (seg.text or "").strip()
        if not candidate_text:
            return
        word_count = len(candidate_text.split())
        if word_count < self.rescue_min_words:
            return

        if self._cue_accum is not None:
            # Check if this interim starts with a new trigger phrase
            # (different utterance) — if so, close the old one and start fresh
            if self._cue_new_trigger_re.match(candidate_text.lower()):
                self._close_cue_accum()
            else:
                # Same utterance — replace the accumulator text in place
                self._cue_accum["candidate"] = candidate_text
                self._cue_accum["full"] = candidate_text
                return

        # Open a fresh accumulator for this evolving utterance
        self._cue_accum = {
            "full": candidate_text,
            "candidate": candidate_text,
            "win": None,  # interims don't carry a Window
        }

    # =================================================================
    # Routing
    # =================================================================
    def _route_window(self, w: Window) -> None:
        result = intent_router.route(
            w.candidate_text, self.session, self._db,
            default_translation=self.default_translation,
        )

        if result.intent == intent_router.EXPLICIT_REF:
            self._close_cue_accum()
            self._handle_explicit(result)
            return
        if result.intent == intent_router.NAV_COMMAND:
            self._close_cue_accum()
            self._handle_nav(result)
            return
        if result.intent == intent_router.CLEAR_COMMAND:
            self._close_cue_accum()
            self._handle_clear(result)
            return
        # CUE_PHRASE -> feed into accumulator so text joins across pauses
        self._extend_cue_accum(w)

    # ---- EXPLICIT_REF / NAV resolve via direct lookup, no FAISS ----
    def _emit_resolve(self, result, source_tag: str) -> None:
        if result.unresolved or not result.reference:
            print(f"[pipeline] {source_tag} unresolved: {result.reason} "
                  f"(candidate: {result.text[:50]!r})")
            return
        # anchor session on every display (next/prev/swap chain off this)
        intent_router.note_display(
            self.session, result.translation, result.book,
            result.chapter, result.verse,
        )
        # keep the re-ranker's running context/history in sync too
        self.accepted_keys.add(f"{result.book} {result.chapter}:{result.verse}")
        self.accepted_books[result.book] = self.accepted_books.get(result.book, 0) + 1
        self.ctx.add(result.book, result.verse_text or "")
        self.on_display(DisplayEvent(
            clear=False, reference=result.reference, translation=result.translation,
            text=result.verse_text, source=source_tag,
        ))

    def _handle_explicit(self, result):
        self._emit_resolve(result, "explicit_ref")

    def _handle_nav(self, result):
        self._emit_resolve(result, "nav")

    def _handle_clear(self, result):
        # Important: do NOT reset current_reference -- a later "next verse"
        # must still resolve off the last real verse shown.
        intent_router.clear_display_keeps_anchor(self.session)
        self.on_display(DisplayEvent(clear=True, source="clear"))

    # ---- CUE accumulator: joins text across pauses for full-quote retrieval ----
    _CUE_END_RE = re.compile(r"[.!?]['\"\']?\s*$")

    _CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
    _TAIL_WINDOW_WORDS = 20

    def _extend_cue_accum(self, w: Window) -> None:
        """Append window text to the live CUE accumulator.

        FAISS search only runs when the text ends with sentence punctuation
        (. ! ?) — incomplete fragments are accumulated but not searched.
        """
        text = w.candidate_text or ""
        if len(text.split()) < self.rescue_min_words:
            return

        has_end = bool(self._CUE_END_RE.search(text))

        if self._cue_accum is not None:
            # New trigger phrase starting while accumulator is open →
            # close the old quote and start fresh
            if self._cue_new_trigger_re.match(text.lower()):
                self._close_cue_accum()
                # fall through to open new accumulator below
            else:
                existing = self._cue_accum["candidate"]
                # If the new text contains the existing text (or vice versa),
                # this is the same utterance arriving as a final after interims.
                # Replace rather than append to avoid duplication.
                if text in existing or existing in text:
                    self._cue_accum["full"] = w.text or text
                    self._cue_accum["candidate"] = text
                else:
                    # Genuine extension (Whisper chunking or new pause window)
                    self._cue_accum["full"] += " " + (w.text or text)
                    self._cue_accum["candidate"] += " " + text
                # Only search on complete sentences
                if has_end:
                    self._run_accumulated_search(is_final=True)
                    self._cue_accum = None
                return

        # Open a fresh accumulator
        self._cue_accum = {
            "full": w.text or text,
            "candidate": text,
            "win": w,
        }
        if has_end:
            self._run_accumulated_search(is_final=True)
            self._cue_accum = None

    def _close_cue_accum(self) -> None:
        """Close the accumulator when a non-CUE intent arrives."""
        if self._cue_accum is not None:
            # Run one final search before closing (in case the last chunk
            # added context that produces a better match)
            self._run_accumulated_search(is_final=True)
            self._cue_accum = None

    def _run_accumulated_search(self, is_final: bool = False) -> None:
        """Run the full CUE pipeline on whatever text the accumulator holds.

        For finalized text, runs retrieval on multiple sub-windows (full text,
        tail window, clause-split windows) and picks the variant with the
        highest confidence. For intermediate (non-final) text, runs a single
        full-text retrieval for speed.
        """
        acc = self._cue_accum
        if acc is None:
            return
        candidate_text = acc["candidate"]
        word_count = len(candidate_text.split())
        if word_count < self.rescue_min_words:
            return
        if word_count >= self._cue_accum_word_cap:
            is_final = True  # force-finalize at word cap
            self._cue_accum = None

        try:
            qp = self._detector.score(candidate_text)
        except Exception as exc:
            print(f"[pipeline] quote detector error: {exc}")
            return

        run_retrieval = qp >= self.quote_threshold
        if not run_retrieval and self.live_rescue:
            if self._rescue_heur is None:
                self._rescue_heur = quote_detect.get_detector("heuristic")
            try:
                heur = self._rescue_heur.score(candidate_text)
            except Exception:
                heur = 0.0
            if heur >= self.rescue_heuristic_floor:
                run_retrieval = True
        if not run_retrieval:
            return

        query_variants = self._build_query_variants(candidate_text, is_final)
        print(f"[pipeline] detection search: is_final={is_final} qp={qp:.3f} "
              f"variants={len(query_variants)} text=\"{candidate_text[:60]}...\"", flush=True)

        vrefs: dict[str, int] = {}
        best_conf = -1.0
        best_ev: Optional[DetectionEvent] = None
        best_selected: Optional[Any] = None

        for qtext in query_variants:
            try:
                cands = self._retriever.search_one(qtext, top_k=self.top_k)
            except Exception as exc:
                print(f"[pipeline] retrieval error: {exc}")
                continue
            if not cands:
                continue

            ranked = rerank_mod.rerank_candidates(
                cands, candidate_text, qp, self.ctx,
                self.accepted_keys, self.accepted_books,
                weights=self.s.weights, cross_encoder=None,
            )
            selected, _ = rerank_mod.select_representative(ranked)
            if selected is None:
                continue
            conf = score_mod.confidence_for(selected)

            vrefs[selected.candidate.key] = vrefs.get(selected.candidate.key, 0) + 1

            if conf > best_conf:
                best_conf = conf
                best_selected = selected
                best_ev = DetectionEvent(
                    reference=selected.candidate.reference,
                    translation=selected.candidate.translation,
                    text=selected.candidate.text,
                    confidence=conf,
                    confidence_band=score_mod.band(conf),
                    transcript_snippet=candidate_text,
                    timestamp=time.time(),
                )

        if best_ev is None:
            return

        if best_selected is not None:
            agree_count = vrefs.get(best_selected.candidate.key, 1)
            if agree_count >= 2:
                boost = min(0.12, 0.03 * (agree_count - 1))
                best_conf = min(1.0, best_conf + boost)
                best_ev = DetectionEvent(
                    reference=best_selected.candidate.reference,
                    translation=best_selected.candidate.translation,
                    text=best_selected.candidate.text,
                    confidence=best_conf,
                    confidence_band=score_mod.band(best_conf),
                    transcript_snippet=candidate_text,
                    timestamp=time.time(),
                )

        if best_conf < self.live_confidence_floor:
            return

        print(f"[pipeline] detection: {best_ev.reference} ({best_ev.translation}) "
              f"conf={best_conf:.3f} band={best_ev.confidence_band}", flush=True)

        self.on_detection(best_ev)

        if is_final and best_selected is not None and acc is not None:
            self.ctx.add_candidate(best_selected.candidate, acc["full"])

        if self.mode == "autopilot" and is_final and best_ev.confidence_band == "autopilot-eligible":
            self.operator_present(
                reference=best_ev.reference,
                translation=best_ev.translation,
                text=best_ev.text,
                source="autopilot",
            )

    def _build_query_variants(self, text: str, is_final: bool) -> list[str]:
        variants: list[str] = [text]
        if not is_final:
            return variants
        words = text.split()
        if len(words) > self._TAIL_WINDOW_WORDS:
            tail = " ".join(words[-self._TAIL_WINDOW_WORDS:]).strip()
            if tail and tail != text and len(tail.split()) >= self.rescue_min_words:
                variants.append(tail)
        clauses = self._CLAUSE_SPLIT_RE.split(text)
        for clause in clauses:
            clause = clause.strip().rstrip(".,; ")
            wc = len(clause.split())
            if wc >= self.rescue_min_words and clause != text:
                variants.append(clause)
        seen: set[str] = set()
        uniq: list[str] = []
        for v in variants:
            if v not in seen:
                seen.add(v)
                uniq.append(v)
        return uniq

    # =================================================================
    # Operator actions (called by the server in Milestone 4)
    # =================================================================
    def operator_present(self, reference: str, translation: Optional[str] = None,
                         text: Optional[str] = None, source: str = "queue_present") -> Optional[DisplayEvent]:
        """Operator chose to show a verse (from the queue or a detection).
        Resolves the reference via the direct DB (any of the 7 versions) and
        anchors ``current_reference`` so next/prev/swap work afterwards."""
        if self._db is None:
            self._db = bible_db.get_bible_db()
        ref = _parse_ref_string(reference)
        if ref is None:
            return None
        book, chapter, verse = ref
        # Pick translation: requested, else the one named in the reference's
        # detection, else default, else any available.
        code = translation or self.default_translation
        vr = self._db.lookup(code, book, chapter, verse)
        if vr is None:
            avail = self._db.translations_for(book, chapter, verse)
            if avail:
                pick = code if code in avail else avail[0]
                vr = self._db.lookup(pick, book, chapter, verse)
        if vr is None:
            return None
        intent_router.note_display(self.session, vr.translation, vr.book, vr.chapter, vr.verse)
        self.accepted_keys.add(vr.key)
        self.accepted_books[vr.book] = self.accepted_books.get(vr.book, 0) + 1
        ev = DisplayEvent(clear=False, reference=vr.reference, translation=vr.translation,
                          text=(text or vr.text), source=source)
        self.on_display(ev)
        return ev


# ===========================================================================
# Small reference-string parser ("Romans 8:28" / "2 Kings 6:2") -> tuple
# ===========================================================================

_REF_STR_RE = re.compile(
    rf"\b(?P<book>(?:[1-3]\s+|[IV]{{1,3}}\s+|first\s+|second\s+|third\s+)?"
    rf"(?:[A-Za-z][a-z]+(?:\s+of\s+[A-Za-z][a-z]+)?))\s+"
    rf"(?P<chap>\d{{1,3}})\s*:\s*(?P<verse>\d{{1,3}})\b",
    re.IGNORECASE,
)


def _parse_ref_string(ref: str) -> Optional[tuple[str, int, int]]:
    if not ref:
        return None
    m = _REF_STR_RE.search(ref)
    if not m:
        return None
    book_token = m.group("book").strip()
    canon = bible_db.resolve_book(book_token)
    if canon is None:
        return None
    return canon, int(m.group("chap")), int(m.group("verse"))


# ===========================================================================
# Standalone console runner (Milestone 3 test). With a real mic this prints
# live captions + detections; with --file it does the same on a sermon file so
# the CUE path is verifiable without a microphone.
# ===========================================================================
def _console_transcript(seg: Segment):
    print(f"[t] {seg.start_time:6.2f}-{seg.end_time:6.2f}  {seg.text}")


def _console_detection(ev: DisplayEvent):
    pass


def _console_display(ev: DisplayEvent):
    if ev.clear:
        print("[DISPLAY] <cleared>")
    else:
        print(f"[DISPLAY] {ev.reference} ({ev.translation})  src={ev.source} :: {ev.text}")


def _console_cue_detection(ev: DetectionEvent):
    pct = round(ev.confidence * 100)
    print(f"[DETECT] {pct:3d}% [{ev.confidence_band}] {ev.reference} ({ev.translation}) "
          f"<-- {ev.transcript_snippet[:50]!r}")
    print(f"          {ev.text[:90]}")


def _build_console_pipeline(args):
    return LivePipeline(
        on_transcript=_console_transcript,
        on_detection=_console_cue_detection,
        on_display=_console_display,
        whisper_model=args.model,
        chunk_seconds=args.chunk,
        overlap_seconds=args.overlap,
        quote_detector_kind=args.detector,
        quote_threshold=args.quote_threshold,
        top_k=args.top_k,
        live_confidence_floor=args.floor,
        default_translation=args.default_translation,
        whisper_initial_prompt=args.initial_prompt,
        condition_on_previous_text=bool(args.condition_previous_text),
        preprocess=not args.no_preprocess,
        transcription_source_type=getattr(args, "source", "whisper"),
        mode=getattr(args, "mode", "semi_autopilot"),
    )


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="FaithView Pro live pipeline (offline).")
    ap.add_argument("--file", help="audio file to feed instead of the mic (offline test)")
    ap.add_argument("--model", default=config.WHISPER_MODEL, help="faster-whisper model id")
    ap.add_argument("--chunk", type=float, default=8.0,
                    help="chunk window seconds (was 4.0; widened for small.en per benchmark)")
    ap.add_argument("--overlap", type=float, default=2.0)
    ap.add_argument("--detector", default="hybrid",
                    choices=["hybrid", "onnx", "heuristic", "semantic"],
                    help="quote detector: 'hybrid' = strong rules (cue/reference/attribution) "
                         "first, trained ONNX model as fallback (default)")
    ap.add_argument("--quote-threshold", type=float, default=config.QUOTE_THRESHOLD)
    ap.add_argument("--top-k", type=int, default=config.TOP_K)
    ap.add_argument("--floor", type=float, default=0.45,
                    help="live confidence floor to surface detections (NOT the batch 0.60; "
                         "live speech runs lower, so ~0.45 surfaces real <0.60 candidates)")
    ap.add_argument("--default-translation", default="NKJV")
    ap.add_argument("--max-segments", type=int, default=None,
                    help="(file mode) stop after N transcript segments")
    ap.add_argument("--device", type=int, default=None, help="mic input device index")
    ap.add_argument("--initial-prompt", default=None,
                    help="Whisper initial_prompt to bias decoding toward vocabulary; "
                         "pass an empty string to DISABLE the config default")
    ap.add_argument("--condition-previous-text", action="store_true",
                    help="opt back IN to whisper's condition_on_previous_text "
                         "(default OFF for live -- avoids '...' continuation "
                         "hallucinations across rolling chunks)")
    ap.add_argument("--no-preprocess", action="store_true",
                    help="disable mic high-pass cleanup (a/b vs file path)")
    args = ap.parse_args()

    if args.file:
        # offline, synchronous drive over the file (same code path as live mic)
        from live_transcribe import FileSource, LiveTranscriber
        p = _build_console_pipeline(args)
        p._ensure_resources()
        p.quote_detector_kind = args.detector
        src = FileSource(args.file, realtime=False)
        print(f"[pipeline] file dry-run: {args.file} | model={args.model} "
              f"detector={args.detector} floor={args.floor} -> Ctrl-C to stop")
        lt = LiveTranscriber(
            on_segment=p._on_new_segment, model_name=args.model,
            chunk_seconds=args.chunk, overlap_seconds=args.overlap,
            language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE, vad_filter=config.WHISPER_VAD_FILTER,
            offline=config.OFFLINE,
            initial_prompt=args.initial_prompt,
            condition_on_previous_text=bool(args.condition_previous_text),
            preprocess=not args.no_preprocess,
        )
        # build windows via the pipeline's segment handler
        seen = 0
        try:
            for _ in lt.iter_segments(src, max_chunks=None):
                seen += 1
                if args.max_segments and seen >= args.max_segments:
                    break
        except KeyboardInterrupt:
            pass
        print(f"\n[pipeline] processed {seen} transcribed segments")
        return

    # real mic
    from live_transcribe import MicSource
    p = _build_console_pipeline(args)
    src = MicSource(device=args.device)
    print(f"[pipeline] LIVE mic | model={args.model} detector={args.detector} "
          f"floor={args.floor} -> Ctrl-C to stop")
    p.start(src, quote_detector_kind=args.detector)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[pipeline] stopping ...")
    finally:
        p.stop()


if __name__ == "__main__":
    _cli()