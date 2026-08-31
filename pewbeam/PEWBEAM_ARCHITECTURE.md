# Pewbeam — Architecture (Reverse-Writeup from Installed Files)

This document is an **architectural account assembled from legible installed
files, configuration, logs, bundled assets, the shipped NDI SDK headers, and
OCR of the in-app screenshots**. Per the investigation brief, no compiled
binary (`.exe` / `.dll` / `.dylib`) was disassembled, decompiled, or
reverse-engineered. Anything only knowable from inside `Pewbeam.exe` or the
NDI runtime DLL is marked **inferred** or **unconfirmed** rather than stated
as fact.

The install was inspected at:

```
C:\Users\ianasasiratusiime\AppData\Local\Pewbeam        (program, _up_ payload, logs)
C:\Users\ianasasiratusiime\AppData\Roaming\Pewbeam      (per-user state: auth, license, font cache)
```

A parallel lowercase `AppData\Local\pewbeam` tree exists with the same
contents (Windows per-machine vs per-user install paths); only one was
analyzed in depth — they are identical.

---

## 1. Provenance & Overall Shape

### 1.1 Product identity

- The **installer welcome screen** (screenshot `2026-07-31 173039`) is titled
  **"EasyVerse Setup"** and markets the product as *"a powerful church
  presentation system for displaying Bible verses, song lyrics, and media
  during services."* Later installer screens (`2026-08-10 205204`, `205213`)
  are titled **"Pewbeam Setup."** So the product was renamed **EasyVerse →
  Pewbeam**. Both names refer to the same application lineage.
- Installed binary: `Pewbeam.exe`, **66,999,296 bytes** (~64 MB).
- Bundled uninstaller: `uninstall.exe`, 82,180 bytes.
- Disk requirement per installer: **205.4 MB** (`2026-08-10 205213`).

### 1.2 Application framework (inferred, high confidence)

The config template (`_up_/resources/config/alpha.env.template`) contains the
line:

> *"Copy this file to `resources/config/alpha.env` before running
> `pnpm run tauri:build` for alpha distributions. These values are baked into
> the bundled application and exposed to both the **Rust backend** and the
> **embedded Python server**."*

This is direct, legible evidence that Pewbeam is a **Tauri application**: a
**Rust** native shell (windowing, outputs, NDI, license) wrapping a **web
frontend**, with an **embedded Python sidecar** that handles the ML-heavy
work (transcription, semantic retrieval). The `pnpm` toolchain and the
"embedded Python server" phrasing place the ML stack in the Python sidecar,
not the Rust shell. **Everything beyond this split is inferred** from the
config keys and bundled assets.

### 1.3 Local vs cloud

| Subsystem | Local | Cloud | Evidence |
|---|---|---|---|
| Account / auth | session cached (`auth/session.enc`, 472 B encrypted blob; `auth/choice.json` = `{"choice":"signed_in"}`) | **Required at runtime** — email + 6-digit code verification (`2026-08-10 205605`) | screenshots + Roaming state |
| Licensing | device fingerprint (`license/device_fingerprint` 64-hex bytes), Ed25519 public key baked in (`KEYGEN_ED25519_PUBLIC_KEY_B64` in `alpha.env`) | Keygen-style cloud license issuance; "LICENSE KEY No license key on file yet" + "Pending" status (`205641`) | config + screenshots |
| Speech-to-text | `mlx-whisper` on **Apple Silicon only** (auto-detected) | **Deepgram Nova-3** on Windows / Intel Mac via a token proxy (`DEEPGRAM_TOKEN_PROXY_URL=https://pewbeam.vercel.app/api/deepgram-token`, `DEEPGRAM_MODEL=nova-3`, `DEEPGRAM_LANGUAGE=en-US`) | `alpha.env.template` |
| Verse matching / embeddings | `google/embeddinggemma-300m` embedded locally + FAISS (`FAISS_SIMILARITY_THRESHOLD=0.7`) | none observed | `alpha.env` |
| Bible text | 6 translations bundled on disk (ESV, KJV, NASB, NIV, NKJV, NLT) | none | `_up_/BibleTranslations/` tree |
| Outputs (NDI etc.) | fully local | none | screenshots + NDI SDK |

The onboarding screen (`205447`) is explicit about the commercial model:
*"Start free — 60 minutes on us. Create a free account for 60 minutes of live
verse detection — plus the lexicon, a second output, every translation, and
AI content."* The upgrade screen (`205641`, `205701`) lists Plus/Core perks
as **"unlimited transcription, more output screens, offline mode, and every
translation."** Notably, **offline STT mode is a paid feature** on Windows
(it implicitly means switching off the Deepgram cloud path; the only free
offline path is `mlx-whisper`, which is Apple-Silicon-only).

### 1.4 Entry points / modes

From the menu and tab chrome (screenshots `205803`, `205936`, `205957`,
`210001`, `210012`) the operator-visible entry surface is:

- **Top tabs**: `Scriptures`, `Live transcript`, `Start` — with the active
  translation + current book/chapter shown inline (`NIV · GENESIS 1`).
- **Audio menu**: `Start Recognition` (`Ctrl+Shift+L`) / `Stop Recognition`
  (`Ctrl+Shift+S`) — this is the **mic-on toggle** that starts live verse
  detection.
- **Window menu**: `New Presentation Window` (`Ctrl+N`), `Close
  Presentation Window` (`Ctrl+W`), `Open HDMI Preview`, `Close HDMI Preview`.
  → Multi-window **presentation output** with a dedicated **HDMI preview**.
- **View menu**: `Toggle Theme (Dark/Light)`, `Open Theme Designer`,
  `Fullscreen`, `Bible Translation` (`Ctrl+T`).
- **Help menu**: `Documentation`, `Report Issue`, `Check for Updates`,
  `Preferences`, `About Pewbeam`.

The application has a single primary mode: a live operator console with the
mic-detection engine keyed on/off via `Start Recognition`. There is no
separate "batch transcribe an audio file" entry point visible in the UI
(unlike FaithView Pro's `run.py`). Batch processing, if present, is not
operator-exposed. **Inferred:** Pewbeam is **live-only** as a product.

---

## 2. NDI Integration

This is the most directly observable subsystem because Pewbeam ships the
**complete NDI SDK** on disk.

### 2.1 What is shipped

NDI SDK is vendored at `_up_/vendor/ndi/` with separate per-OS subtrees:

```
vendor/ndi/
├── macos/
│   ├── include/   ← 16 Processing.NDI.*.h headers (full C API)
│   └── lib/
│       └── libndi.dylib          (29,411,216 B)
└── windows/
    ├── include/   ← only .gitkeep (no headers shipped)
    └── lib/
        ├── Processing.NDI.Lib.x64.dll   (29,863,120 B)
        ├── Processing.NDI.Lib.Licenses.txt
        └── .gitkeep
```

**Key asymmetry**: on **macOS** Pewbeam ships both the headers and the
`libndi.dylib`. On **Windows** it ships only the runtime DLL — no headers.
This matches the canonical **dynamic-load** pattern documented in
`Processing.NDI.DynamicLoad.h` (shipped on the mac side, 34 KB): the
application calls `NDIlib_initialize()` and resolves symbols at runtime via
the platform's dynamic loader, so headers are not required to ship for the
Windows production build. The mac build keeping the headers is consistent
with a development/source layout where the SDK is vendored whole for the mac
target.

### 2.2 What the SDK exposes (from the shipped headers)

`Processing.NDI.Send.h` documents the sender API Pewbeam would use to push
the live display out as an NDI source:

- `NDIlib_send_create(NDIlib_send_create_t*)` — create a sender with a
  `p_ndi_name` (the source name receivers see), a `p_groups` filter, and
  `clock_video` / `clock_audio` rate-limiting flags.
- `NDIlib_send_send_video_v2(...)` — push a video frame synchronously.
- `NDIlib_send_send_video_async_v2(...)` — push a frame asynchronously
  (returned immediately; SDK owns the buffer until a sync event). This is the
  recommended path for BGRA frames because compression/network happen on the
  SDK's threads.
- `NDIlib_send_send_audio_v3(...)` — push audio.
- `NDIlib_send_send_metadata(...)` — push XML metadata per frame.
- `NDIlib_send_get_no_connections(...)` — how many receivers are currently
  subscribed; used to **skip rendering when nobody is connected**.
- `NDIlib_send_get_tally(...)` — on-program / on-preview tally.
- `NDIlib_send_add_connection_metadata` / `clear_connection_metadata` —
  metadata auto-sent on each new receiver connection.

`Processing.NDI.structs.h` documents the frame formats the SDK accepts:

- **Video FourCCs**: `UYVY`, `UYVA` (alpha), `P216`, `PA16`, `YV12`, `I420`,
  `NV12`, `BGRA`, `BGRX`, `RGBA`, `RGBX`. Default constructor uses `UYVY`
  and `30000/1001` (29.97 fps).
- **Audio**: `FLTP` planar 32-bit float, default 48 kHz / 2 channels.
- **Metadata**: UTF-8 XML string with a timecode.
- **Timecodes**: 100-ns intervals; `NDIlib_send_timecode_synthesize`
  (`INT64_MAX`) tells the SDK to synthesize timecodes from system clock —
  the simplest integration path, and the most likely default for a
  presentation tool.

The NDI SDK's bundled third-party list (`Processing.NDI.Lib.Licenses.txt`)
calls out **RapidJSON, SpeexDSP (resampling only), RapidXML, CxxUrl, ASIO,
MsQuic, Opus, SHA-2**. So the shipped NDI runtime does its own JSON, XML,
networking (MsQuic/ASIO), resampling, and audio coding (Opus) internally —
application code only has to feed it frames.

### 2.3 How NDI is surfaced to the operator

From the `Output settings` screenshots (`210021`, `225858`):

```
Output settings
├── Main Output
│   └── Configure Main Output
│       ├── Select theme
│       ├── Select translation        ("Same as Scripture panel" default)
│       ├── Output monitor            ← direct-attach display
│       ├── NDI Output                ← checkbox: "Enable this output"
│       └── Output format             ← "1920 x 1080 / 60Hz"
└── Output 2
    └── Configure Output 2
        └── (same fields)
└── "Unlock more output screens" / "Upgrade plan"
```

So NDI is wired as **one sink among several** for each "output" slot:

- Each output slot independently binds: **a theme** (visual template), **a
  translation** (which Bible text to render, defaulting to follow the
  Scripture panel), **an output monitor** (physical screen), and an **NDI
  Output** toggle.
- Each output also has an **Output format** selector — the screenshot shows
  **1920×1080 / 60 Hz** — i.e., resolution and frame-rate are configurable.
  (`Processing.NDI.structs.h` defaults to 29.97; Pewbeam configures 60 Hz,
  matching the `frame_rate_N/D` fields on `NDIlib_video_frame_v2_t`.)
- **Multi-output is a paid feature**: "Output 2" is present in the UI but
  gated behind "Unlock more output screens … Upgrade plan." On the free tier
  only **Main Output** is usable, but that one output can itself be NDI.

The onboarding welcome screen (`2026-07-31 173039`, EasyVerse) explicitly
lists **"NDI virtual camera video output"** as a headline feature, so NDI is
a first-class output path, not an afterthought.

### 2.4 Inferred NDI runtime behavior

Nothing in the config files sets an explicit NDI source name, group, or
discovery scope; those values are not stored in `alpha.env` or any legible
JSON. **Inferred from the SDK design + UI**:

1. On enabling an output's "NDI Output" checkbox, the Rust shell creates a
   `NDIlib_send_create_t` with a source name (probably derived from the
   machine name + output label, following the SDK's `MACHINE (SOURCE)`
   convention in `NDIlib_source_t.p_ndi_name`).
2. The web frontend composes each frame (theme + scripture text + background
   image) into a **BGRA** bitmap (the most ergonomic format for the async
   send path per the SDK header comment), then hands it to
   `NDIlib_send_send_video_async_v2` with `timecode =
   NDIlib_send_timecode_synthesize`.
3. The frame-rate of submission matches the selected **Output format**
   (60 Hz in the screenshot). `clock_video = true` lets the SDK rate-limit.
4. **Tally / connection count** (the `get_tally` / `get_no_connections`
   calls in `Send.h`) are almost certainly used to **short-circuit rendering
   when no receiver is connected** — a standard NDI power-optimization.
5. Audio is almost certainly **not** sent over NDI from Pewbeam (the product
   is a *verse display*, not an audio router); the mic audio goes to Deepgram
   independently. This is **unconfirmed** — only the absence of any audio-NDI
   knob in the screenshot is observable.

### 2.5 What is NOT observable

- The actual NDI source name string Pewbeam registers. Not in config.
- Whether Pewbeam also *receives* NDI (the `Recv.h` / `FrameSync.h` headers
  are shipped on mac, but no UI surface for NDI input exists in the
  screenshots). **Inferred: send-only.**
- Whether multiple NDI outputs can run concurrently per output slot. The UI
  ties NDI to an "output" which is independently themed, so plausibly yes,
  but **unconfirmed**.

---

## 3. Live Audio → Verse Detection Pipeline

### 3.1 Transcription stage (directly confirmed by logs)

The log file `logs/transcription-2026-08-10.log` is the single most
revealing legible artifact. It is **newline-delimited JSON**, one event per
line, all with `"event":"transcription_final"`. Representative entries:

```json
{"timestamp":"2026-08-10T18:02:05.715834900+00:00","level":"info",
 "event":"transcription_final","provider":"deepgram",
 "session_id":"683e1cbd-…","session_label":"session-001","session_index":1,
 "segment_id":3,"confidence":0.9970703125,"duration_ms":1786384925688,
 "text":"Go to Matthew seven twenty four.",
 "normalized_text":"Go toMatthew 7:24",
 "message":"Transcript (final, session-001): Go to Matthew seven twenty four.",
 "source":"transcription"}
```

Confirmed facts about the transcription stage:

- **Provider**: `deepgram` (matches the `DEEPGRAM_MODEL=nova-3` config).
- **Confidence** is a Deepgram word/segment confidence (0.87–0.999 in the
  sample), stored as a float.
- **Session-scoped**: each run has a `session_id` (UUID), a `session_label`
  (`"session-001"`), and a 1-indexed `segment_id` that increments per final
  utterance.
- **Two text fields per event**: `text` = raw ASR, `normalized_text` = a
  **post-processed string in which spoken verse references are parsed into
  canonical form** (e.g., `"Go to Matthew seven twenty four."` →
  `"Go toMatthew 7:24"`, `"Genesis one one"` → `"Genesis 1:1"`, `"verse one"`
  → `"verse 1"`). This is the same Role-expansion + digit-normalization that
  FaithView's `intent_router.resolve_book` does — but Pewbeam does it
  **inside the transcription logging layer**, which strongly implies the
  reference parser runs **immediately after ASR finalization**, before any
  downstream routing/retrieval.
- **Segment revision**: the same `segment_id` (e.g. `22`) can appear twice
  in a row with different `text` (`"Ask and it will be given to you. Seek
  and you'll find"` and then `"knock and the door will be open to you."`).
  This is consistent with Deepgram `is_final` utterance continuation /
  endpointing producing revised or appended hypotheses for one logical
  utterance.

### 3.2 Voice-command / intent layer (inferred from log utterances)

The log utterances fall into clearly distinct buckets — and since the
**normalized_text** already digitizes references, these utterances are
almost certainly being classified at or right after transcription, not
later:

| Utterance in log | Likely intent |
|---|---|
| `"Go to Matthew seven twenty four."` → `"Go toMatthew 7:24"` | **Explicit reference navigation** |
| `"Go to Genesis one one."` → `"Go toGenesis 1:1"` | **Explicit reference navigation** |
| `"Next verse."` / `"Go to verse one."` | **Verse stepping** |
| `"Previous verse."` / `"Previous was."` | **Verse stepping (reverse)** |
| `"Next bus."` (ASR mishears "verse" as "bus") | System **accepts** it — recorded as `normalized_text: "Next bus."`, but the intent survives in the operator UI (per the running session). So either a fuzzy command matcher, or the operator tapped through. **Unconfirmed which.** |
| `"Stop there."` | **Stop / freeze** command |
| `"The bible says"` → then `"in the beginning, God created the heavens and the"` | **Cue-phrase trigger** opening a quote |
| `"Ask and it will be given to you. Seek and you'll find"` / `"knock and the door will be open to you."` | **Verbatim scripture quote** (Matthew 7:7–8) flowing through the semantic-match path |

So the **intent taxonomy** is essentially the same shape as FaithView's:
**EXPLICIT_REF, NAV_COMMAND (step/stop), CUE_PHRASE**. A **CLEAR** command is
not visible in this log session.

### 3.3 Semantic verse matching (confirmed from config, internals inferred)

Two legible config keys pin the retrieval stack:

```env
EMBEDDING_MODEL=google/embeddinggemma-300m
FAISS_SIMILARITY_THRESHOLD=0.7          # alpha.env (template ships 0.65)
```

- **Embeddings**: `google/embeddinggemma-300m` — a Gemma-family 300M-param
  embedding model. This is a different, larger lineage than FaithView's
  `BAAI/bge-small-en-v1.5` (384-dim, ~130 MB).
- **Vector store**: FAISS, with an explicit **similarity threshold of 0.7**
  for accepting a match (vs. FaithView's QUOTE_THRESHOLD=0.70 on the
  *detector* and a separate `live_confidence_floor=0.45`).
- No FAISS index file is shipped on disk — the embeddings + FAISS index are
  presumably **built at runtime on first use** from the bundled translation
  JSON/SQL. **Inferred:** the embedded Python sidecar builds and caches the
  FAISS index much like FaithView's `build_index.py`, but on demand.

What is **NOT visible** anywhere in the install:

- Anything resembling a **rerank** stage, **confidence banding**, an
  **autopilot / review / manual** mode split, a **cue accumulator**, a
  **multi-variant query** strategy, or a **history / sermon-context**
  signal. If any of these exist they are compiled into `Pewbeam.exe` /
  inside the Python sidecar's bundled bytecode and are not legible. The
  config ships no rerank weights, no banding thresholds, and no mode flags.
  The only "threshold" exposed is the single FAISS similarity cutoff.
- Any **ONNX quote classifier** or **heuristic cue-phrase detector** model
  on disk. Neither a model file nor a cue-phrase wordlist is bundled
  anywhere legible. **Inferred:** if a cue-phrase / scripture-vs-nonscripture
  classifier exists, it is either inside the compiled exe or fetched on
  demand. The log shows cue phrases being followed by quote text and
  reaching the live display, so *some* detection gating exists, but its
  mechanism is unobservable here.

The UI does show a **"Recent detections"** panel (`205803`, `205936`), a
**"Context search"** panel, and a **"Live display"** panel — so a detection
feed and a separate context-search feature (operator-typed free text →
semantic verse search, analog of FaithView's `/search` endpoint) are
operator-visible. The **scoring internals behind "Recent detections" are
not legible**.

### 3.4 Bible data layer (directly confirmed)

Six translations are bundled, each in **two parallel formats**:

```
BibleTranslations/
├── ESV/  ESV_bible.json   (4,781,143 B)   ESV_bible.sql   (4,789,629 B)
├── KJV/  KJV_bible.json   (4,846,087 B)   KJV_bible.sql   (4,963,047 B)
├── NASB/ NASB_bible.json  (4,919,293 B)   NASB_bible.sql  (4,962,801 B)
├── NIV/  NIV_bible.json   (4,731,550 B)   NIV_bible.sql   (4,708,015 B)
├── NKJV/ NKJV_bible.json  (4,856,039 B)   NKJV_bible.sql  (4,892,374 B)
└── NLT/  NLT_bible.json   (4,916,679 B)   NLT_bible.sql   (4,884,910 B)
```

Plus per-book JSON shards (`ESV_books/Psalm.json`, etc.) — ~66 files per
translation.

**Two schemas, for two consumers:**

1. **The `.sql` file is a plain SQL dump**, not a binary SQLite db. First
   line of `ESV_bible.sql`:
   ```sql
   create table esv(book_id int not null, book varchar(255) not null,
                    chapter int not null, verse int not null,
                    text varchar(1000) not null,
                    primary key (book_id, chapter, verse));
   INSERT INTO esv(book_id, book, chapter, verse, text) VALUES
   (1,'Genesis',1,1,'In the beginning, God created the heavens and the earth.'),
   ...
   ```
   → a relational `book_id`-keyed table per translation (table named after
   the translation code). This is the **direct-lookup** path (explicit
   reference resolution, voice navigation). Primary key is
   `(book_id, chapter, verse)`.

2. **The `.json` file is a nested book→chapter→verse map** with an `Info`
   header:
   ```json
   { "Info": { "Language":"English", "Meaningless":"1.2.0",
               "Timestamp":"2025-04-21T21:36:47.240186-04:00",
               "Translation":"ESV" },
     "Psalm": { "1": { "1": "...", "2": "..." } } }
   ```
   The peculiar `"Meaningless": "1.2.0"` key (a 1.2.0 version tag under a
   stray name) and the per-translation build timestamp are app-internal
   metadata. This JSON shape is the likely input the Python sidecar turns
   into the FAISS index (one row per verse, embedded with Gemma-300m).

**Confirmed translation set**: ESV, KJV, NASB, NIV, NKJV, NLT (6).
The onboarding screen advertises *"every translation"* as a paid perk, so the
free tier restricts which of these six are usable even though all six are
on disk. The screenshot chrome shows **NIV** as the active translation and a
**"TRADITIONAL"** toggle (possibly a contemporary/traditional-language
 rendition switch — **unconfirmed**).

### 3.5 License / account runtime (partially confirmed)

- `Roaming/Pewbeam/license/device_fingerprint` — 64 hex chars (32 bytes),
  an Ed25519-style device fingerprint used by Keygen offline licensing.
- `Roaming/Pewbeam/auth/session.enc` — 472 B **encrypted** blob (cipher
  unstated). Carries the signed-in session. **Unconfirmed:** whether this is
  encrypted with a key derived from the device fingerprint or with a
  machine-bound key.
- `Roaming/Pewbeam/auth/choice.json` — `{"choice":"signed_in"}` records the
  selected onboarding path (in contrast to "Continue without an account"
  shown on the start screen `205447`).
- `Roaming/Pewbeam/font-cache.json` — 9,784 B enumerating all locally
  installed font families + weights (Adobe, Segoe, Bahnschrift, etc.). Read
  once at startup to populate the Theme Designer font pickers
  (`General Sans`, `Figtree`, `Inter` bundled in `_up_/resources/fonts/`).
- `.setup_complete` — `{}`, a 2-byte marker that onboarding has run.

`alpha.env.template` ships the public half of a Keygen Ed25519
verification key (`KEYGEN_ED25519_PUBLIC_KEY_B64=...`), confirming
**offline license verification** with the Keygen.sh pattern; the private
signing side lives on Pewbeam's license server. The upgrade screen
(`205701`) shows license states ("Pending", "No license key on file yet",
"Manual billing", "1 machine included"), so the offline path is
device-bound (1 machine on free).

---

## 4. Operator Workflow & UI (from screenshots)

Compiled from the OCR text of all 21 screenshots. Where I could not see
the pixels directly, this is grounded purely in the OCR'd text + spatial
order in the OCR result; **visual styling (exact colors, density, status
coding) is inferred from the text labels and standard Tauri/web UI
conventions, not from pixel inspection.**

### 4.1 First-run flow

1. **Installer** (`2026-07-31 173039` EasyVerse / `2026-08-10 205204`,
   `205213` Pewbeam): welcome → choose folder (205.4 MB needed) → install.
2. **Start screen** (`205447`): three CTAs — `Create a free account` /
   `Log in` / `Continue without an account`. Free tier = 60 min live verse
   detection + lexicon + a second output + every translation + "AI content."
   A **trial countdown** surfaces later in the main window chrome as
   **"eo:00 left"** (almost certainly "00:00 left"-style minutes-remaining
   badge next to the `LIVE` indicator — `205803`, `205936`).
3. **Email verification** (`205605`): 6-digit code sent to email.
4. **Post-login account** (`205641`, `205701`): License / Plan / Profile /
   Downloads (Windows / Mac Apple-Silicon / Mac Intel as separate
   downloads) / Devices (1 max) / Support. Exit point: `Sign out`, `Edit`.

### 4.2 Main operator console

Frame (`205803`, `205936`):

```
┌─ Pewbeam ──── File Edit View Audio Window Help ────────────────── [LIVE] [time-left]
│  Scriptures  Live transcript  Start          NIV  GENESIS 1
├─[left rail icons]──┬─[center stack]────────────────┬─[right stack]───┐
│  Slides            │  Recent detections            │  Main Output ▾  │
│  Songs             │  Context search               │  Lexicon | Queue│
│  O Black           │  Live display                 │  Book search    │
│  Outputs           │   ─ GENESIS (NIV)             │   Genesis 1:1 …│
│  Themes            │     In the beginning God …    │   2 And God …  │
│                    │                               │   3 …          │
└────────────────────┴───────────────────────────────┴─────────────────┘
                                            status bar:  8:59 PM  8/10/2026
```

- **Top tab row**: `Scriptures` (Bible reader), `Live transcript` (the ASR
  captions), `Start` (onboarding/account). The active translation + book +
  chapter are shown inline (`NIV · GENESIS 1`), and updated by voice
  navigation.
- **Left icon rail**: `Slides`, `Songs`, `Outputs`, `Themes`, plus a `Black`
  toggle (blank-output / "blackout" control — standard in church
  presentation tools).
- **Center**: three stacked panels — **Recent detections** (the live
  scripture-detection feed), **Context search** (free-text semantic verse
  search), **Live display** (the rendered current verse — exactly what the
  congregation sees: `GENESIS (NIV) — In the beginning God created the
  heavens and the earth.`).
- **Right**: output selector (`Main Output ▾`, with an "Output 2" available
  but plan-gated), **Lexicon**/**Queue** tab pair, and a **Book search**
  pane showing the chapter as a vertical reader (Genesis 1:1, 1:2, 1:3 …).
  A **Clear all** button empties the queue.
- **Status bar** at the bottom shows system time — operator-visible.

### 4.3 Output & alert configuration

- **Output settings** (`210021`, `225858`): per-output ("Main Output" /
  "Output 2") theme, translation, output monitor, **NDI Output toggle**,
  and **Output format** (`1920×1080/60Hz`). Extra output screens gated to
  paid plans.
- **Scroll alert dialog** (`210039`): an operator **message overlay** with
  marquee config — `Enter message here`, font picker (`Figtree v`, bold,
  size 40, color `#FFFFFF`), "Turn this off to show a static message instead
  of a marquee", scroll speed, scroll count, background color
  (`#E1D2F`, 100% opacity), output target (`All displays`), `Send alert`.
  This is the **operator alert / lower-third** capability, fully
  style-driven.

### 4.4 Theme Designer (`210051`, `210107`)

A full **pixel-precise theme editor** layered on top of the verse renderer:

- Theme library sidebar: `ALL THEMES (3)`, search, `+ New`, `Import`,
  `Export`, `Save changes`, `Close`.
- Built-in shipped themes named in the install:
  - **"Selah"** (referenced in `backgrounds/README.md`)
  - **"Lower Third Sample 2"** (background `b2ba5783-…-…-…-….jpg` shipped
    at `resources/backgrounds/`)
  - A third theme inferred from "ALL THEMES (3)" — name not legible.
- Content model: themes are composed of layers — **Text / Scripture / Shape
  / Image** (radio add-content buttons in `210051`).
- **Scripture layer controls** (`210051`): Alignment, Position (x=91),
  Dimension (w=1738), Reference gap (48), Lock aspect ratio, **Scripture
  Font** picker (`General Sans`, Bold, line-height 0, Size Auto, letter
  spacing), Reference gap knob (the gap between the verse body and the
  citation line `Genesis (NKJV)`).
- **Effects panel** (`210107`): per-layer **Drop Shadow** (position, blur,
  color `#000000`) and **Outline** (position Outside, solid, color picker)
  — both live-rendered in the inline preview (`In the beginning God
  created the heaven and the earth — Genesis (NKJV)`).
- Preview area shows the rendered verse with the theme applied, so the
  designer is WYSIWYG.

### 4.5 Information density / workflow notes

- The operator works **one console** with three live panels (detections,
  context-search, live-display) plus a queue — comparable information
  density to FaithView's four-panel console but laid out in a three-column
  vertical stack rather than FaithView's 2×2 grid.
- **Status coding** (color/labels for confidence) is **not legibly visible
  in OCR** — the screenshots show no obvious confidence-band color text,
  but Pewbeam's config does not expose band thresholds at all, so whether
  banded coding exists is **unconfirmed**. The "Recent detections"
  panel's per-row formatting cannot be assessed without pixel view.
- The **trial countdown badge** ("eo:00 left" next to LIVE) is an unusually
  prominent always-visible meter — operators of the free tier are
  constantly aware of remaining minutes. This is a commercial, not
  technical, design choice, but it shapes operator workflow on the free
  tier.
- **NDI/output is one settings dialog away** — not buried deeply, but also
  not on the main console. Operator workflow: configure outputs once,
  then leave them; day-to-day work is voice + detections + queue.
- The **"Black"** toggle on the left rail is the standard church-ops
  "kill the projector output immediately" safety control.
- **Keyboard shortcuts** are conventional (`Ctrl+T` translation, `Ctrl+N`
  new presentation window, `Ctrl+Shift+L` start recognition, `Ctrl+W`
  close, `Alt+F4` exit) — consistent with a Tauri/Electron-style native
  shell but **differ from FaithView** which is browser-based (no
  `Ctrl+Shift+L` global shortcut for start, since FaithView uses a
  WebSocket UI).

---

## 5. Genuinely Uncertain / Unconfirmed

Flagged honestly — stated as **speculation**, not fact:

- **Rerank stage**: FaithView ships a 5-signal rerank + optional
  cross-encoder. Pewbeam ships **only** an embedding model name and one
  FAISS threshold. Whether Pewbeam has *any* rerank, or relies solely on
  FAISS cosine + the cue trigger, is not visible. **Speculation:** the
  single threshold suggests a simpler retrieve-and-accept pipeline than
  FaithView's, but a compiled-in reranker cannot be ruled out.
- **Confidence banding / autopilot**: no config keys, no UI labels observed.
  The "Recent detections" panel exists, so *some* gating happens, but
  whether Pewbeam has FaithView-style `autopilot` / `review` / `manual`
  bands is **unconfirmed**.
- **Cue accumulator**: the log shows cue phrases (`"The bible says"`)
  followed by quote text spanning multiple final segments
  (`"Ask and it will be given…"`, then `"knock and the door will be
  open to you."` with the same `segment_id`). Whether Pewbeam explicitly
  *accumulates* across segments the way FaithView's CUE accumulator does,
  or whether Deepgram's own `is_final` aggregations handle it, is
  **unconfirmed**.
- **NDI source name, audio-over-NDI, tally usage**: inferred from SDK
  defaults; not configurable in any legible file.
- **ONNX quote classifier / cue-phrase wordlist**: no model or wordlist
  bundle is present anywhere legible. If a classifier exists it is inside
  the compiled exe or fetched at runtime.
- **Translation-restriction mechanism** ("every translation" as a paid
  perk): all six translations are on disk; whether the gate is enforced
  client-side (license-flag check) or server-side (and the bundled files
  are simply inert until unlocked) is **unconfirmed**.
- **"TRADITIONAL" toggle** on the console: name is legible; whether it is
  a language-register switch (e.g., NKJV ↔ modernized text of the same
  verse) or a theme/style flag is **unconfirmed**.
- The **"Meaningless": "1.2.0"** metadata key in the translation JSON is
  unexplained — likely a deliberately obfuscated version tag, an
  anti-tamper marker, or an inside name. Purpose **unconfirmed**.