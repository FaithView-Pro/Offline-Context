# FaithView Pro vs Pewbeam — Comparison & Improvement Plan

Companion to `PEWBEAM_ARCHITECTURE.md` (Pewbeam writeup) and
`faithview pro/TECHNICAL_ARCHITECTURE.md` (FaithView Pro writeup).

**Scope of evidence.** FaithView's side is taken from its full
`TECHNICAL_ARCHITECTURE.md`. Pewbeam's side is taken **only** from legible
installed files (config, logs, vendored NDI SDK headers, Bible data, the
onboarding/account/output/theme screenshots) — **not** from decompilation.
Where Pewbeam internals are only knowable from inside `Pewbeam.exe`, they are
flagged **inferred** or **unconfirmed**. Be aware of the asymmetry: FaithView
is documented in full by its author; Pewbeam is partly a black box, so any
"FaithView is more sophisticated than Pewbeam at X" claim is really
"FaithView is more sophisticated than *what we can see of* Pewbeam at X."

---

## 1. Side-by-side

| Area | FaithView Pro | Pewbeam (observed) |
|---|---|---|
| **App shell** | Python (`server.py` FastAPI + uvicorn) + static HTML/JS console served on `localhost:8000`. Browser-based operator UI. | Tauri app: Rust native shell + web frontend + **embedded Python sidecar** for ML. Native window. (Tauri confirmed by `pnpm tauri:build` line in `alpha.env.template`.) |
| **Entry modes** | Two: **batch** (`run.py` on an audio file → `results.json`) and **live** (`server.py` WS console). | **Live only** as a product. No batch entry point in the UI. (**Inferred** — no operator surface for batch.) |
| **STT (default)** | `faster-whisper small.en` int8, **fully offline**, ~4.2× realtime on CPU. Optional Deepgram cloud path. | **Deepgram Nova-3** cloud on Windows/Intel; `mlx-whisper` offline **on Apple Silicon only**. Offline STT on Windows is a **paid** feature. |
| **STT log shape** | `Segment{text, start_time, end_time, words}` (in-memory only; no on-disk log shipped by default). | One JSON event per `transcription_final`: `provider, session_id, segment_id, confidence, text, **normalized_text** (references digitized), message`. On-disk newline-delimited log under `logs/`. |
| **Reference parsing** | `intent_router.resolve_book` + regex → canonical `(T, B, C, V)`, runs at routing time (after buffering). | Runs **at/just after ASR finalization** — `normalized_text` already contains `"Genesis 1:1"`, `"Matthew 7:24"` before the event is logged. Stage placement differs. |
| **Intent taxonomy** | `EXPLICIT_REF`, `NAV_COMMAND`, `CLEAR_COMMAND`, `CUE_PHRASE` (4), with documented precedence. | Visible intents match: explicit-ref, verse-step (next/prev), stop, cue-phrase. **CLEAR not seen in the log session** — presence unconfirmed. |
| **Cue accumulator** | Explicit cross-segment accumulator with min-words open, sentence/punctuation/close-on-non-CUE, 35-word cap, multi-variant retrieval (full / tail-20 / clause-split) + cross-variant agreement bonus. | **Not observable.** Log shows cue-then-quote spanning two final segments with same `segment_id` (Deepgram continuation), but whether Pewbeam accumulates *itself* or relies on Deepgram's `is_final` aggregation is **unconfirmed**. |
| **Quote detection** | 3 detectors behind `QuoteDetector.score`: `HeuristicQuoteDetector` (10 weighted signals, 48 cue patterns, archaic-vocab, divine-speech, negative markers), `SemanticQuoteDetector` (heuristic + FAISS dual-translation concordance check, the strongest offline signal), `OnnxQuoteDetector` (trained MiniLM/DistilBERT, in-house, 20k synthetic rows). | **None observable on disk.** No model file, no cue-wordlist, no thresholds in config. If present, inside the compiled exe / Python sidecar bytecode. |
| **Retrieval** | `bge-small-en-v1.5` (384-dim, ~130 MB) + `faiss.IndexFlatIP` exact cosine over **AMP+NKJV** (~62k verses). `TOP_K=10`. | `google/embeddinggemma-300m` + FAISS, `FAISS_SIMILARITY_THRESHOLD=0.7` (template 0.65). Which translations are indexed: **unconfirmed** (all 6 on disk; free tier restricts usable set). |
| **Rerank** | **5-signal hybrid**: 45% semantic, 20% lexical, 15% context (book-freq + keyword tracker), 10% history, 10% quote_prob. Optional **cross-encoder** `ms-marco-MiniLM-L-6-v2` 30% blend. `select_representative` dedups AMP/NKJV. | **Not observable.** Single FAISS threshold in config. Rerank, if any, is compiled in. **Speculation:** simpler retrieve-and-accept than FaithView. |
| **Confidence banding** | 3 bands (`autopilot-eligible` ≥0.60, `review queue` 0.60–0.95, `ignored` <0.60) + separate live floor `0.45`. Modes: `autopilot` / `semi_autopilot` / `manual`. | **Not observable.** No thresholds in config; no band labels visible in OCR. **Unconfirmed.** |
| **Bible data** | `bible_all_versions.json` single file (7 translations, KJV/NIV/NKJV/NLT/AMP/ESV/MSB + African languages) → in-memory `BibleDB` hash by `(T,B,C,V)`. FAISS corpus = 2 (AMP+NKJV) of those 7. | **6 translations**, each shipped in **two formats** in parallel: JSON `book→chapter→verse` map with `Info` metadata, **and** a SQL dump `create table ESV(book_id, book, chapter, verse, text)`. Direct-lookup uses the SQL; semantic path uses the JSON. |
| **Translation aliases** | 80+ spoken book aliases, 25+ translation aliases (`resolve_book`, `resolve_translation`). | Not in config; likely compiled in. The log's `normalized_text` resolving `"Matthew seven twenty four"` → `"Matthew 7:24"` shows robust spoken-number parsing at minimum. |
| **NDI output** | **Not present.** Output is browser HTML + REST `/present` + WebSocket `display_update`. No NDI, no native multi-screen, no theme system. | **Fully integrated.** Ships the NDI SDK (~29 MB DLL + 29 MB dylib + headers on mac). Per-output enable toggle, `Output format` 1920×1080/60Hz, multi-output (paid). Welcome screen markets "NDI virtual camera video output." |
| **Outputs (general)** | 1 logical "live display" (the web console's display panel + whatever the operator's browser tab is on). | Multiple **output slots** (Main, Output 2, more paid). Each slot: theme + translation + monitor + NDI toggle + format. Separate **HDMI Preview** window. **Black** (blackout) rail toggle. **Scroll alert** marquee overlay with font/speed/color. |
| **Theme / rendering** | None — frontend is functional, not themed. | **Theme Designer**: layered (Text/Scripture/Shape/Image), per-layer font, position, dimension, reference gap, drop shadow, outline; bundled themes "Selah," "Lower Third Sample 2"; built-in backgrounds. WYSIWYG. |
| **Account / licensing** | None. Local, offline-first, no auth, no phone-home. `OFFLINE=True` default. | Required: account + email 6-digit code, Keygen Ed25519 device-bound license, **1 machine on free**, plan tiers (free 60 min / Plus / Core), offline STT is a **paid** perk. Session encrypted (`session.enc`). |
| **Config knobs (operator-visible)** | ~30 in `config.py` (quote threshold, top-k, rerank weights, cross-encoder flag, VAD silence, live floor, autopilot threshold, etc.) + CLI flags. | ~6 in `alpha.env` (Deepgram model/lang/proxy URL, STT auto-detect, embedding model, FAISS threshold, license key). Most behavior is compiled-in, not operator-tunable. |
| **Operator console layout** | 4-panel web grid: live transcript / detections / live display / queue. Browser tab. | 3-column native window: left rail (Slides/Songs/Outputs/Themes/Black), center stacked (Recent detections / Context search / Live display), right (Output selector / Lexicon / Queue / Book-search chapter reader). |
| **Workflow shortcuts** | Browser-driven (no global hotkeys). | Native: `Ctrl+Shift+L` start recognition, `Ctrl+Shift+S` stop, `Ctrl+T` translation, `Ctrl+N`/`Ctrl+W` presentation windows, Alt+F4 exit. |
| **Cold-start cost** | ~5.1 s (embedder 4.8 s + FAISS 315 ms). | Not measured; includes Tauri shell + Python sidecar boot + embedding model load. **Unconfirmed.** |
| **Diagnostics surface** | `/health` REST returns `{ok, audio, floor, source, mode}`. WebSocket events for every stage. | `transcription_final` log only — **no detection-stage events are logged** in the shipped log file (either off or absent). Operator has *less* introspection into scoring than FaithView. |
| **Offline guarantee** | Three enforced flags (`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `HF_DATASETS_OFFLINE`) + all models cached. Zero runtime cloud except opt-in Deepgram. | **Online by default on Windows** (Deepgram cloud). Offline is Apple-Silicon-only and, commercially, a paid tier. Account/auth is always online. |

---

## 2. Where Pewbeam looks genuinely better / more mature

> Stated honestly, not as flattery. These are the gaps worth closing in
> FaithView Pro.

### 2.1 NDI output — the biggest gap (and your stated immediate priority)

Pewbeam ships the **canonical NDI SDK** and exposes it as a first-class
output sink with a per-output enable checkbox, resolution/frame-rate
selector, and per-output theme + translation binding. It is wired for the
**`NDIlib_send_send_video_async_v2`** async path with synthesized
timecodes and (almost certainly) connection-aware rendering via
`NDIlib_send_get_no_connections` — the textbook-efficient integration.

FaithView has **no NDI at all**. Its "live display" is a DOM panel in the
operator's browser; getting that onto a projector or into OBS/vMix Resi
is left entirely to the operator. For a *church presentation* product, this
is the single largest feature deficit.

### 2.2 Multi-output + per-output theme/translation binding

Pewbeam's concept of an **Output** — an independently configured sink with
its own **theme, translation, monitor, NDI toggle, and format** — is
genuinely more mature than FaithView's single display. A church running a
front projection screen in NIV + a stage confidence monitor in NKJV +
an NDI feed to thebroadcast booth in ESV can do all three concurrently
(paid tier; Main + Output 2 + …). FaithView has one display, one
translation at a time, no per-output configuration.

### 2.3 A real theme / rendering engine

The Theme Designer (Text/Scripture/Shape/Image layers, per-layer font,
position, dimension, reference gap, drop shadow, outline, WYSIWYG preview)
is a **product-class** feature that FaithView's functional frontend lacks.
For volunteer operators, being able to match the church's visual identity
is a primary purchase driver, and it directly affects what an NDI output
would actually *look like* — NDI without a theme engine just ships ugly
text.

### 2.4 Native shell + global hotkeys + HDMI preview

Tauri gives Pewbeam `Ctrl+Shift+L` to start recognition from anywhere, a
**Black** blackout rail toggle (critical fail-safe in live church ops),
**Open HDMI Preview** (so the operator can verify what's going to the
projector before it goes live), and proper multi-window presentation
windows. FaithView is constrained to a single browser tab — no global
hotkeys, no blackout switch, no separate preview. These are operator-safety
features that matter in a live service.

### 2.5 Early reference normalization in the transcription log

Pewbeam logs **`normalized_text`** with references digitized
(`"Genesis 1:1"`) at ASR-finalization time. This is a small but elegant
design choice: the parser lives next to ASR, so every downstream consumer
(routing, retrieval, logging, future analytics) gets a canonical
reference for free. FaithView parses references at `intent_router` *after*
buffering, so the transcription stage itself doesn't carry a canonical
reference.

### 2.6 Scroll alert / operator message overlay

The marquee editor (`210039`) — font, size, color, scroll speed, scroll
count, background, "All displays" routing — is a standard church-ops
feature (announcements, "please silence phones") that FaithView lacks.

### 2.7 Simpler operator mental model (where it's simpler by design)

Pewbeam exposes **one** semantic threshold (`FAISS_SIMILARITY_THRESHOLD`)
and no rerank weights, band thresholds, or detector-type choices in
operator-visible config. This is *less powerful* than FaithView but **less
to misconfigure** — a volunteer running a service does not need to
understand 5-signal rerank. FaithView's richness is an asset for a power
user and a liability for a church volunteer.

---

## 3. Where FaithView Pro looks better / more sophisticated

> Stated with evidence, not pride.

### 3.1 The 5-signal hybrid re-rank (+ optional cross-encoder)

FaithView's rerank is genuinely more sophisticated than **anything
observable** in Pewbeam. Pewbeam ships a single FAISS threshold and no
rerank weights in config; if a reranker exists it is fully compiled-in and
operator-invisible. FaithView's published blend — 45% semantic / 20%
lexical / 15% context (book-frequency + keyword tracker) / 10% history
(accepted refs in this transcript) / 10% quote-prob, plus optional
ms-marco-MiniLM-L-6-v2 at 30% — is a documented, tunable, *tested* design.
FaithView's semantic + direct-lookup split (FAISS for unknown quotes;
`BibleDB` hash for explicit refs and navigation) is also openly documented;
Pewbeam's split is inferred from the two storage formats but unconfirmed.

### 3.2 Confidence banding + three operator modes

FaithView's `autopilot-eligible` / `review queue` / `ignored` bands with a
separate live floor (0.45) and three modes (`autopilot` /
`semi_autopilot` / `manual`) directly address the **trust gradient** that a
live verse-detection product needs. Pewbeam shows a "Recent detections"
panel but no banding is observable. **FaithView is more honest with the
operator about uncertainty** — banded color coding says "trust this /
verify this / ignore this." That is a real operator-trust advantage.

### 3.3 Semantic + direct-lookup split, with FAISS over 2 of 7

FaithView explicitly indexes only AMP+NKJV in FAISS (cost: one index
build, dual-translation concordance verification) while covering the
remaining 5 (KJV/NIV/NLT/ESV/MSB + African languages) via direct hash
lookup. This is a deliberate, documented trade-off. Pewbeam ships 6
translations but **which are FAISS-indexed and whether there is a
direct-lookup split is unconfirmed**. If Pewbeam embeds all 6, that's a
larger index and slower build with no documented concordance-verification
benefit.

### 3.4 Dual-translation concordance in `SemanticQuoteDetector`

FaithView's `SemanticQuoteDetector` requires the **same reference** to
appear from **both AMP and NKJV** with cosine ≥0.70 to give a strong boost
— the strongest offline signal that the speaker is verbatim quoting
scripture, not teaching about a verse. Pewbeam's threshold (single 0.7
cutoff) does not, from observable evidence, do cross-translation
concordance. This is a faithfulness win for FaithView.

### 3.5 The CUE accumulator + multi-variant retrieval

FaithView explicitly accumulates cue-introduced quotes across segment
boundaries, runs retrieval on **three query variants** (full / tail-20 /
clause-split), and gives a **cross-variant agreement bonus** (+0.03 per
agreeing variant, max +0.12). Pewbeam's cross-segment handling cannot be
confirmed from logs. This is a real recall advantage for the
"the-Bible-says… [long pause] …in-the-beginning… [pause] …God-created"
case that is common in real sermons.

### 3.6 Live rescue for verbatim quotes

FaithView's live-rescue path (ONNX score < threshold AND heuristic ≥0.15
AND ≥4 words → run retrieval anyway) is a deliberate fallback for
**bare verbatim quotes** that the ONNX cue-classifier is bad at. This is a
specifically engineered recall fix. Not observable in Pewbeam.

### 3.7 Fully offline, no account, no phone-home

FaithView's offline guarantee (3 enforced env flags, all models cached,
zero runtime cloud except opt-in Deepgram, no auth) is a **privacy and
deployability** advantage that matters for churches in low-connectivity
contexts (very relevant in Uganda, given the locale). Pewbeam requires
account + email verification, a Deepgram token proxy, a device-bound
license, and is online-by-default on Windows. FaithView can run on a
machine with no internet after the one-time model download; Pewbeam
cannot (on Windows) without paying for offline STT.

### 3.8 Batch mode

FaithView's `run.py` batch pipeline (process a recorded sermon →
`results.json`) is a **post-hoc analysis** tool Pewbeam doesn't expose.
Useful for sermon review, archival, and training data generation
(FaithView's `generate_dataset.py` + `train_quote_classifier.py`
pipeline depends on having batch + synthetic data generation).

### 3.9 Operator introspection via WebSocket events

FaithView exposes nine event types (`transcript_update`, `detection`,
`display_update`, `queue_update`, `session_update`, `source_update`,
`mode_update`, `transcript_clear`, `detections_clear`) plus `/health` and
`/search` REST. An operator (or a future telemetry consumer) can see
*every* stage. Pewbeam logs only `transcription_final` — no detection
events, no display events, no queue mutations, no source/mode changes.
FaithView is materially more diagnosable.

### 3.10 Translation coverage including African languages

FaithView ships Luganda, Runyankole, Swahili in `versions/` for the
direct-lookup path — directly relevant to your locale. Pewbeam ships 6
English translations only.

---

## 4. Prioritized improvements for FaithView Pro

Ordered by your stated priority (**NDI first**), with implementation notes
matching FaithView's existing **Python + strategy-pattern** architecture.
Each item states: gap → target design → implementation notes.

### P0 — Add NDI output (immediate gap)

**Target.** A new `NDIOutputStrategy` that renders the current display
verse to an NDI source on the LAN, with per-output config (enable,
resolution, frame-rate, source name, theme binding).

**Implementation.**

1. **Pick the binding.** The official NDI SDK is a C ABI; the cleanest
   Python route is [`ndi-python`](https://github.com/bcurio/ndi-python) (or
   the maintained `pymndi`/`pyndi` ctypes wrapper). It dynamically loads
   `Processing.NDI.Lib.x64.dll` (Windows) / `libndi.dylib` (mac) at
   runtime — exactly the pattern Pewbeam itself uses (Pewbeam ships the
   same DLL, and on Windows ships **no headers** precisely because the
   dynamic-load path doesn't need them; see
   `Processing.NDI.DynamicLoad.h`). Vendor the DLL/dylib into
   `faithview pro/vendor/ndi/{windows,macos}/lib/` the way Pewbeam does,
   so FaithView stays self-contained offline.
2. **New output strategy interface.** FaithView already uses the strategy
   pattern (`QuoteDetector`, `TranscriptionSource`, `AudioSource`). Add:
   ```python
   class OutputStrategy(ABC):
       def start(self) -> None: ...
       def stop(self) -> None: ...
       def render(self, verse: VerseRef, theme: Theme | None) -> None: ...
   ```
   Implementations: `WebDisplayOutput` (the existing `/present` path,
   refactored behind the interface), `NDIOutput`, (future)
   `HDMIOutput`, `ObsBrowserOutput`.
3. **Frame composition.** NDI video frames want BGRA bitmaps (the SDK's
   recommended async path; the header comment in `Processing.NDI.Send.h`
   is explicit that BGRA lets compression/network run on SDK threads).
   Use **Pillow** to render the verse text + reference + (future) theme
   background into a BGRA `ndarray` sized to the configured resolution
   (default 1920×1080), then hand it to
   `NDIlib_send_send_video_async_v2` with
   `timecode = NDIlib_send_timecode_synthesize` (the simplest correct
   path; matches Pewbeam's inferred choice).
4. **Frame-rate / clocking.** Set `clock_video=true` in
   `NDIlib_send_create_t` and let the SDK rate-limit. Submit at the
   configured Output format (start with 1920×1080@60Hz, matching the
   Pewbeam screenshot default). Only re-render when the verse changes
   (FaithView's `display_update` event is the natural trigger); the SDK
   will keep streaming the last frame at the target fps.
5. **Connection-aware rendering.** Call
   `NDIlib_send_get_no_connections(timeout=0)` each frame and **skip
   rendering when 0 receivers** — a free render-saving that Pewbeam almost
   certainly uses (it's the canonical NDI power pattern).
6. **Per-output config.** Extend `config.py` with a small
   `OutputConfig` dataclass list (translation, resolution_w/h, fps_n/d,
   ndi_enable, ndi_source_name, theme_id). Start with one Main output and
   leave the list extensible to multi-output (P1 below).
7. **Threading.** NDI send must happen on **its own thread**, never the
   asyncio event loop. Use `asyncio.run_coroutine_threadsafe` (already
   the FaithView pattern for the audio thread) or a dedicated
   `OutputRenderThread` fed by an immutable `DisplayState`.
8. **Graceful offline.** If the NDI DLL fails to load, log a warning and
   fall back to `WebDisplayOutput` only. Never hard-crash on a missing
   optional output.
9. **REST/WS surface.** Add `POST /output/configure`,
   `GET /outputs`, and a WS `output_update` event. The operator console
   needs an "Output settings" page modeled on Pewbeam's
   (`Select theme / Select translation / NDI Output / Output format`).
10. **No audio over NDI** (matches Pewbeam's inferred design — FaithView
    sends mic audio to Whisper, not to NDI).

### P1 — Multi-output slots + per-output translation/theme binding

**Gap.** FaithView's single display can't feed a front projector in NIV
while feeding a stage monitor in NKJV.

**Target.** N output slots, each an `OutputConfig` driving its own
`OutputStrategy`.

**Implementation.** The P0 `OutputStrategy` interface + `OutputConfig`
list *is* the multi-output design — just generalize the loop. Keep Main
free; gate >1 outputs behind a flag (FaithView has no account/paywall, so
use a soft cap, e.g. `--max-outputs`, defaulting to 4). Each output keeps
its own `NDIlib_send_instance_t` and its own render thread. The display
event becomes "verse X is live" and every enabled output renders it in its
own translation/theme.

### P1 — A minimal theme engine

**Gap.** NDI output is useless to a church without a themed verse
renderer. FaithView's frontend is functional, not themed.

**Target.** A `Theme` dataclass (background image/color, scripture font,
font size, color, position, dimension, reference gap, drop shadow,
outline) + a JSON theme file + a **Pillow-based renderer** shared by both
the NDI output and the web console.

**Implementation.**

1. `Theme` dataclass in `theme.py` with `from_json(path)`.
2. `RenderVerse(verse, theme, size) -> BGRA ndarray` — Pillow `Image` +
   `ImageDraw` + `ImageFont`, supporting: background image or color,
   verse text with line-wrap, citation `Reference (Translation)` with a
   configurable reference gap, drop shadow (draw text twice, offset +
   blur), outline (stroke).
3. Ship 2 bundled themes (analog to Pewbeam's "Selah" + "Lower Third
   Sample 2") with bundled background images in `static/themes/`.
4. (Stretch) a mini **Theme Designer** web page in the operator console:
   sliders for position/dimension/gap, font picker (reuse the Pewbeam
   pattern of caching installed-font enumeration), color pickers,
   live `<canvas>` preview. Full Pewbeam-parity theme editing is a real
   build; ship a minimal "load + tweak one theme" first.

### P1 — Blackout + HDMI preview safety controls

**Gap.** Live church ops need a one-click **Black** (kill output) and a
**Preview** (verify before going live). FaithView has neither.

**Implementation.** `POST /output/blackout` (sets a `blackout=True` flag
that every `OutputStrategy.render` short-circuits on, sending a black
frame), `POST /output/preview` producing a one-off PNG frame for the
console to display without touching live outputs. Add `Black` and
`Preview` buttons to the web console's output panel. This is cheap and
matters disproportionately in live failure modes.

### P2 — Native shell + global hotkeys (consider Tauri)

**Gap.** Browser-based means no global `Ctrl+Shift+L` to start
recognition from anywhere, no multi-window, no `Alt+F4` semantics.

**Implementation.** Two options:
- **Light**: ship FaithView as a **PWA** with `chrome --app` shortcutting
  and the `KeyboardEvent` listener as good as a browser permits. Cheapest,
  gets you **nothing** for global hotkeys.
- **Right**: wrap FaithView in **Tauri** (matching Pewbeam's confirmed
  shell). Keep the FastAPI server `localhost:8000` as the model server,
  make Tauri's Rust shell a thin webview pointing at it; this gives you
  real global hotkeys, multi-window presentation windows, HDMI preview
  windows, and a `Black` rail with proper fullscreen semantics. Tauri is
  the path Pewbeam itself took.

Recommendation: do P0/P1 first (NDI + themes + multi-output), then evaluate
whether browser constraints actually hurt your users before investing in
Tauri. A church volunteer on one laptop with one projector can be served
fine by PWA + the new NDI output.

### P2 — Early reference normalization in the transcription stream

**Gap.** FaithView parses spoken references at `intent_router`, *after*
buffering. Pewbeam digitizes them at ASR-finalization, so every downstream
consumer (log, routing, retrieval, future analytics) gets a canonical
reference for free.

**Implementation.** Add a `normalize_references(text)` step at the end
of `LiveTranscriber._on_new_segment` (and in `transcribe.segments_from_dicts`)
that runs the **same** regex family as `intent_router.resolve_book` but
*emits a `normalized_text` field on `Segment`*. The router keeps its full
intent classification; downstream logging + retrieval can use the
pre-normalized form. Low cost, high consistency. Mirror this into a
`logs/transcription-YYYY-MM-DD.ndjson` file matching Pewbeam's log shape —
useful for debugging and for regenerating synthetic training data via
`generate_dataset.py`.

### P2 — Operator alert / scroll message overlay

**Gap.** Pewbeam's marquee editor (`210039`) is a standard church-ops
feature (announcements, "silence phones"). FaithView has nothing.

**Implementation.** Add an `AlertState` to `live_pipeline.py`, an
`AlertStrategy.render(text, font, color, speed)` that produces an animated
frame (or, for the NDI output, a moving text layer). `POST /alert` with
`{message, font, color, scroll_speed, scroll_count, background, target}`.
Wire the existing `OutputStrategy` interface so `render` accepts either a
`VerseRef` or an `Alert`. Reuses the theme engine's text rendering.

### P3 — On-disk logs include detection + display events (parity, observability win)

**Gap.** Pewbeam logs only ASR finals. FaithView has richer WS events but
writes nothing to disk by default.

**Implementation.** Add a `logdir/` and write newline-delimited JSON for
`detection`, `display_update`, `queue_update`, `mode_update`,
`source_update` in addition to `transcript`. This makes FaithView strictly
**more diagnosable than Pewbeam** (Pewbeam's log is ASR-only) and gives you
real training/eval data for the next ONNX classifier iteration.

### P3 — Reconsider the embedder choice

**Gap.** FaithView uses `bge-small-en-v1.5` (384-dim, ~130 MB). Pewbeam
uses `google/embeddinggemma-300m` (larger, different lineage). Gemma-300m
is a stronger general-purpose embedder on net; whether it's stronger on
*Bible verse concordance* is an empirical question.

**Implementation.** Add `embeddinggemma-300m` as a **second**
`Embedder` implementation behind the existing `Embedder` strategy
interface (don't replace `bge-small`). Rebuild a second FAISS index
(`build_index.py --embedder gemma`) and run the existing
`benchmark.py` + `evaluate_quote_classifier.py` over both. Decide on
empirical F1/latency; do not switch cold. This is exactly what the
strategy pattern is for.

---

## 5. UI / UX recommendations from the screenshots review

FaithView's operator console is currently a 2×2 grid of stacked panels in
a browser tab. Drawing from Pewbeam's screens:

1. **Three-column native layout, not 2×2 grid.** Pewbeam's left icon rail
   (Slides/Songs/Outputs/Themes/Black) + center stacked panels
   (detections/context-search/live-display) + right rail (output
   selector/lexicon/queue/book-search) is a better fit for a wide operator
   monitor and keeps the **Live Display** (the one thing that matters)
   visually centered and large. FaithView's 2×2 grid shrinks the live
   display to a quarter of the screen.
2. **First-class "Output" panel, not an afterthought.** Once NDI lands,
   surface it the way Pewbeam does: a per-output card with theme +
   translation + monitor + NDI toggle + format, all in one dialog. Do
   not hide NDI under a settings menu — it's a primary output sink.
3. **Always-visible Live / countdown / status chrome.** Pewbeam shows
   `LIVE` + a time-remaining badge persistently. FaithView should show
   `LIVE` (or `PAUSED`/`MANUAL`) + current transcription source
   (`whisper`/`deepgram`) + operator mode (`autopilot`/`semi`/`manual`)
   in a persistent top bar, updated via the existing `source_update` /
   `mode_update` events.
4. **Per-row confidence band color coding, explicit.** Pewbeam's banding
   is unconfirmed; FaithView's banding is documented but the console
   should make it visually unambiguous (green/yellow/grey for
   autopilot/review/ignored) with the numeric confidence + band label
   visible per detection row. This is an area where FaithView can be
   *better than Pewbeam*.
5. **Chapter reader as a side panel.** Pewbeam's right-rail Book search
   with the chapter laid out verse-by-verse (`Genesis 1:1 …`, `1:2 …`,
   `1:3 …`) is a useful "where are we in the chapter" anchor. FaithView
   already has `/bible/{T}/{B}/{C}` → render it as a fixed right-rail
   panel highlight-current-verse.
6. **A proper Theme Designer, not raw config.** Even a minimal one
   (position/dimension/font/color/reference-gap + live preview) closes
   the largest perceived-quality gap versus Pewbeam and feeds the NDI
   output directly.
7. **Black + Preview buttons in easy reach.** Live church failure modes
   are the operator's primary fear. A one-click `Black` (blackout) and a
   `Preview` (render-to-PNG, not to live) are disproportionate
   trust-builders. Pewbeam has both.
8. **Native global hotkeys.** Even in browser, FaithView can offer
   `Space` to start/stop recognition, `Esc` to blackout — but only a
   Tauri wrap gives true global hotkeys. Defer to P2 but track it.
9. **Scroll/alert overlay UI** matching Pewbeam's: a small "Send alert"
   dialog with font/size/color/speed/background + "All displays"
   routing. Cheap to build once the theme/renderer exists.
10. **Do NOT adopt Pewbeam's always-online account + 60-minute trial
    meter.** FaithView's offline-first, no-auth posture is a genuine
    advantage for low-connectivity settings (and for privacy). Keep it.
    The "free 60 minutes then pay" + persistent countdown badge is a
    commercial choice Pewbeam makes that FaithView should not mimic.

---

## 6. Bottom line

- **Pewbeam wins decisively on output:** native Tauri shell, integrated
  NDI, multi-output slots, a real theme engine, blackout/preview safety,
  and a scroll-alert overlay. These are the features a church actually
  sees and pays for.
- **FaithView wins decisively on the detection engine:** documented
  5-signal rerank, optional cross-encoder, confidence banding, three
  operator modes, semantic+direct-lookup split, dual-translation
  concordance, CUE accumulator with multi-variant retrieval, live
  rescue, fully-offline-by-default, batch mode, full WebSocket
  introspection, and African-language coverage.
- **Highest-leverage moves** (in order): ship NDI output → ship
  multi-output + a minimal theme engine → ship blackout/preview safety
  → ship early reference normalization + on-disk detection logging →
  evaluate `embeddinggemma-300m` as a second embedder behind the
  strategy interface → consider a Tauri wrap only if browser constraints
  start hurting.

The detection-engine sophistication is *wasted on a church volunteer if
the output layer can't reach the projector.* Closing the NDI gap is the
single change that makes FaithView's superior detection *matter in a
Sunday service*.