"""Synthetic sermon-ASR dataset generator for the Scripture-quote classifier.

Generates ~20k rows in the exact register of preacher_text_only.csv (raw
Whisper output: chopped segments, disfluency, mis-transcriptions, occasional
non-English ASR hallucination) by calling an LLM in small batches. Every
positive row's verse reference is resolved in code against the real local
corpus (bible_all_versions.json — KJV/NIV/NKJV/NLT/AMPLIFIED/ESV/MSB, with a
random version seeded per row; falls back to amplified.json + nkjv.json);
unresolvable references are dropped.

Resume-safe: rows are appended to the output JSONL after every batch, and on
startup the existing file is reloaded to rebuild per-subtype counts and the
global dedup set. Re-running simply continues until targets are met.

Usage:
    python generate_dataset.py                 # full run (20k rows)
    python generate_dataset.py --limit 120     # smoke test
    python generate_dataset.py --export-only   # just rebuild the CSV from JSONL

Requires .env with GLM_API_KEY / GLM_BASE_URL / GLM_MODEL (gitignored).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

# --- Config -------------------------------------------------------------------

BATCH_SIZE = 35
MAX_BATCHES = 3000            # safety guard against runaway loops
BATCH_DELAY_S = 0.6
MAX_RETRIES = 3
MAX_TEXT_CHARS = 500
TEMPERATURE = 0.95
MAX_TOKENS = 6000

TARGETS = {
    # positive (~35%)
    "direct_quote_chopped": 2800,   # verbatim wording, cut before/after full verse
    "explicit_reference": 2100,     # "turn to Romans chapter eight" etc.
    "paraphrase": 2100,             # reworded, still anchored to one real verse
    # negative (~65%)
    "narration_chop": 5450,         # ordinary sermon narration mid-flow (bulk)
    "bible_story_no_quote": 2200,   # retells a Bible event, no specific verse
    "hard_negative_name_only": 1800,  # Bible name/place, not a quote
    "testimony_or_personal": 1800,  # personal story / healing testimony
    "transition_or_filler": 1400,   # "listen carefully", "somebody say amen"
    "asr_noise_artifact": 350,      # ~2.5% of negatives: garbled ASR hallucination
}
POSITIVE_SUBTYPES = {"direct_quote_chopped", "explicit_reference", "paraphrase"}
NEGATIVE_SUBTYPES = set(TARGETS) - POSITIVE_SUBTYPES

# Preaching topics rotated across batches to fight repetition over ~570 calls.
TOPICS = [
    "the authority of the believer", "divine healing", "faith and confession",
    "the Holy Spirit's power", "prayer life", "worship and praise",
    "spiritual warfare", "grace vs works", "identity in Christ",
    "the name of Jesus", "speaking in tongues", "the blood of Jesus",
    "prosperity and provision", "the anointing", "deliverance from demons",
    "the Word of God as seed", "resurrection power", "kingdom dominion",
    "prophetic ministry", "the glory of God", "revival", " consecration",
    "the love of God", "forgiveness", "the second coming", "tithing and giving",
    "spiritual gifts", "the church as Christ's body", "overcoming fear",
    "walking in the Spirit", "the cross", "salvation", "hope in trials",
    "God's faithfulness", "the rapture", "angels and ministry of angels",
]

CHOP_HINTS = [
    "About half of the rows must start or end MID-CLAUSE, cut at arbitrary points.",
    "Make most rows end abruptly, mid-thought, some trailing with '...'.",
    "Several rows should begin mid-sentence as if the previous segment was cut off.",
    "Mix short (5-12 word) chops with longer run-on rows; at least a third must be chopped mid-clause.",
]

# --- Book-name aliases -> canonical corpus names --------------------------------

ALIASES = {
    "psalm": "Psalms", "psalms": "Psalms", "song of songs": "Song of Solomon",
    "song of solomon": "Song of Solomon", "gen": "Genesis", "genesis": "Genesis",
    "ex": "Exodus", "exod": "Exodus", "exodus": "Exodus", "lev": "Leviticus",
    "num": "Numbers", "deut": "Deuteronomy", "deuteronomy": "Deuteronomy",
    "josh": "Joshua", "judg": "Judges", "jdg": "Judges", "1 sam": "1 Samuel",
    "2 sam": "2 Samuel", "1 samuel": "1 Samuel", "2 samuel": "2 Samuel",
    "1 kgs": "1 Kings", "2 kgs": "2 Kings", "1 kings": "1 Kings", "2 kings": "2 Kings",
    "1 chr": "1 Chronicles", "2 chr": "2 Chronicles", "1 chronicles": "1 Chronicles",
    "2 chronicles": "2 Chronicles", "psa": "Psalms", "prov": "Proverbs",
    "ecc": "Ecclesiastes", "ecclesiastes": "Ecclesiastes", "isa": "Isaiah",
    "isaiah": "Isaiah", "jer": "Jeremiah", "jeremiah": "Jeremiah",
    "lam": "Lamentations", "ezek": "Ezekiel", "ezekiel": "Ezekiel", "dan": "Daniel",
    "daniel": "Daniel", "hos": "Hosea", "hosea": "Hosea", "obad": "Obadiah",
    "mic": "Micah", "micah": "Micah", "hab": "Habakkuk", "zeph": "Zephaniah",
    "hag": "Haggai", "zech": "Zechariah", "zechariah": "Zechariah", "mal": "Malachi",
    "malachi": "Malachi", "matt": "Matthew", "matthew": "Matthew", "math": "Matthew",
    "mk": "Mark", "mark": "Mark", "lk": "Luke", "luke": "Luke", "jn": "John",
    "john": "John", "acts": "Acts", "rom": "Romans", "romans": "Romans",
    "1 cor": "1 Corinthians", "2 cor": "2 Corinthians", "1 corinthians": "1 Corinthians",
    "2 corinthians": "2 Corinthians", "gal": "Galatians", "galatians": "Galatians",
    "eph": "Ephesians", "ephesians": "Ephesians", "phil": "Philippians",
    "philippians": "Philippians", "col": "Colossians", "colossians": "Colossians",
    "1 thess": "1 Thessalonians", "2 thess": "2 Thessalonians",
    "1 thessalonians": "1 Thessalonians", "2 thessalonians": "2 Thessalonians",
    "1 tim": "1 Timothy", "2 tim": "2 Timothy", "1 timothy": "1 Timothy",
    "2 timothy": "2 Timothy", "tit": "Titus", "titus": "Titus",
    "phlm": "Philemon", "philemon": "Philemon", "heb": "Hebrews", "hebrews": "Hebrews",
    "jas": "James", "james": "James", "1 pet": "1 Peter", "2 pet": "2 Peter",
    "1 peter": "1 Peter", "2 peter": "2 Peter", "1 john": "1 John", "2 john": "2 John",
    "3 john": "3 John", "jude": "Jude", "rev": "Revelation", "revelation": "Revelation",
}

_REF_RE = re.compile(
    r"^\s*((?:[1-3]\s*)?[A-Za-z]+(?:\s+(?:of\s+)?[A-Za-z]+)?)\s+(\d{1,3})\s*[:.]\s*(\d{1,3})\s*$"
)

# --- .env -----------------------------------------------------------------------

def load_env() -> None:
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# --- Corpus ----------------------------------------------------------------------

def load_corpus_lookup() -> tuple[dict, list, list]:
    """Load the multi-version corpus.

    Returns:
      lookup: {(book, chapter): {verse: {VERSION: text}}} for reference resolution
      pool:   [(reference, version, text)] seedable verses across ALL versions
      versions: version codes present (e.g. KJV, NIV, NKJV, NLT, AMPLIFIED, ESV, MSB)

    Falls back to amplified.json + nkjv.json if bible_all_versions.json is absent.
    """
    allv = HERE / "bible_all_versions.json"
    if allv.exists():
        data = json.loads(allv.read_text(encoding="utf-8"))
        versions = list(data.get("versions", []))
        lookup: dict = {}
        pool: list[tuple[str, str, str]] = []
        for book in data.get("books", []):
            bname = book.get("book")
            for ch in book.get("chapters", []):
                cnum = ch.get("chapter")
                for v in ch.get("verses", []):
                    vnum = v.get("verse")
                    texts = v.get("text", {})
                    if not isinstance(texts, dict):
                        continue
                    lookup.setdefault((bname, cnum), {})[vnum] = texts
                    for ver, txt in texts.items():
                        if txt and 8 <= len(txt.split()) <= 45:
                            pool.append((f"{bname} {cnum}:{vnum}", ver, txt))
        return lookup, pool, versions

    # fallback: two-translation corpus via corpus.py
    import corpus as corpus_mod
    verses = corpus_mod.load_corpus(str(HERE / "amplified.json"), str(HERE / "nkjv.json"))
    lookup = {}
    pool = []
    for v in verses:
        lookup.setdefault((v.book, v.chapter), {}).setdefault(v.verse, {})[v.translation] = v.text
        if 8 <= len(v.text.split()) <= 45:
            pool.append((v.reference, v.translation, v.text))
    return lookup, pool, ["AMP", "NKJV"]


def resolve_reference(ref: str, lookup: dict) -> str | None:
    """'Romans 8:28' / 'Psalm 23:1' / '1 John 4:8' -> canonical 'Book C:V' or None."""
    if not ref or not isinstance(ref, str):
        return None
    m = _REF_RE.match(ref.strip().rstrip(".,;"))
    if not m:
        return None
    book_raw = re.sub(r"\s+", " ", m.group(1).strip().lower())
    book = ALIASES.get(book_raw)
    if book is None and book_raw:
        book = book_raw.title() if book_raw.title() in {b for (b, _c) in lookup} else None
    if book is None:
        return None
    chapter, verse = int(m.group(2)), int(m.group(3))
    if verse in lookup.get((book, chapter), {}):
        return f"{book} {chapter}:{verse}"
    return None


def verse_texts(canonical_ref: str, lookup: dict) -> dict:
    """All available version texts for a canonical 'Book C:V' reference."""
    m = re.match(r"^(.+)\s+(\d+):(\d+)$", canonical_ref)
    book, ch, vs = m.group(1), int(m.group(2)), int(m.group(3))
    return lookup.get((book, ch), {}).get(vs, {})


def content_overlap(a: str, b: str) -> int:
    stop = {"the", "a", "an", "and", "or", "but", "of", "to", "in", "for", "on", "with",
            "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does",
            "you", "your", "i", "we", "they", "he", "she", "it", "his", "her", "their",
            "our", "my", "that", "this", "as", "at", "by", "from", "not", "no", "will",
            "shall", "would", "could", "should", "may", "all", "any", "into", "upon"}
    ta = {w for w in re.findall(r"[a-z']+", a.lower()) if w not in stop and len(w) > 2}
    tb = {w for w in re.findall(r"[a-z']+", b.lower()) if w not in stop and len(w) > 2}
    return len(ta & tb)

# --- LLM client -------------------------------------------------------------------

def llm_generate(messages: list[dict], api_key: str, base_url: str, model: str) -> str:
    r = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": TEMPERATURE,
              "max_tokens": MAX_TOKENS, "stream": False},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def extract_json_array(raw: str) -> list:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON array in response")
    return json.loads(raw[start:end + 1])

# --- Prompt ------------------------------------------------------------------------

SYSTEM = (
    "You are a synthetic-data engine producing training rows that mimic RAW ASR "
    "(Whisper) transcripts of live charismatic/Pentecostal sermons — African and "
    "American extemporaneous preaching mixed. The text is messy ASR output: chopped "
    "mid-thought at arbitrary segment boundaries, filler words, run-ons, repeated "
    "words, false starts, self-corrections, occasional mis-transcribed words, and "
    "trailing '...'. Output STRICT JSON only: one JSON array of objects. No "
    "markdown, no commentary, no code fences."
)

SUBTYPE_INSTRUCTIONS = {
    "direct_quote_chopped": (
        "Verbatim Bible wording from the PROVIDED seed verses, chopped so the segment "
        "cuts before or after the full verse — often mid-quote. 'verse' = the exact "
        "seed reference."),
    "explicit_reference": (
        "Names a reference in passing ('turn to Romans chapter eight', 'verse fifteen "
        "says', 'if you open Titus chapter two') — the quoted text may trail off, be "
        "absent, or continue in the next row. 'verse' = the exact seed reference."),
    "paraphrase": (
        "Same meaning as a PROVIDED seed verse but reworded in the preacher's own "
        "words; still clearly anchored to that one verse. 'verse' = the exact seed "
        "reference."),
    "narration_chop": (
        "Ordinary sermon narration mid-flow — teaching, exhortation, story-telling "
        "with NO scripture quoted and NO reference named. 'verse' = '0'."),
    "bible_story_no_quote": (
        "Retells a Bible event or person in the preacher's own words ('David faced "
        "Goliath and everyone was afraid') with NO specific verse quoted. 'verse' = '0'."),
    "hard_negative_name_only": (
        "Contains a Bible name/place ('Paul travelled a lot', 'in Ephesus there was "
        "a revival') but is NOT a quote and names NO reference. 'verse' = '0'."),
    "testimony_or_personal": (
        "Personal story, healing testimony, anecdote ('I was in a meeting and a lady "
        "got healed'). 'verse' = '0'."),
    "transition_or_filler": (
        "Pure connective tissue: 'listen carefully', 'can somebody say amen', 'are "
        "you following me', 'turn to your neighbor'. 'verse' = '0'."),
    "asr_noise_artifact": (
        "Short garbled ASR hallucination: partially non-English (Indonesian, Chinese, "
        "Korean, Portuguese, German fragments), nonsensical, like the model lost the "
        "thread on noisy audio. Keep these SHORT (3-15 words). 'verse' = '0'."),
}


def build_prompt(plan: dict[str, int], anchors: list[str], seed_verses: list[tuple[str, str, str]],
                 topic: str, chop_hint: str, split_seq: bool) -> str:
    lines = [
        f"Generate exactly {sum(plan.values())} training rows for this batch. "
        f"Sermon topic theme: {topic}. {chop_hint}",
        "",
        "Rows per subtype (each object's 'subtype' must be one of these exactly):",
    ]
    for st, n in plan.items():
        lines.append(f"- {st}: {n} rows. {SUBTYPE_INSTRUCTIONS[st]}")
    lines += [
        "",
        "Style rules (critical):",
        "- Register: extemporaneous preaching, spoken-word cadence, NOT written-essay style.",
        "- Include realistic disfluency: 'no, no, no', 'you know', 'listen', 'alright?', "
        "repeated words, self-correction, occasional ASR mis-transcription of a word.",
        "- Segments are chopped at arbitrary points; mid-sentence starts/stops are expected.",
        "- Do NOT number the rows or add any keys other than text/verse/label/subtype.",
    ]
    if split_seq and any(st in plan for st in POSITIVE_SUBTYPES):
        lines.append(
            "- Include at least 2 multi-row sequences: a single scripture quote SPLIT "
            "across 2 adjacent rows (the quote continues mid-way into the next row; both "
            "rows carry the same verse reference).")
    if anchors:
        lines += ["", "Real example rows from the actual transcript — match THIS register "
                  "exactly (do not copy the content):"]
        lines += [f"  {a}" for a in anchors]
    if seed_verses:
        lines += ["", "Seed verses (real, from our multi-version Bible corpus). Every positive "
                  "row MUST use one of these EXACT references in its 'verse' field, and its "
                  "text must quote or paraphrase THAT verse only. For direct_quote_chopped "
                  "rows, keep the seed's exact wording (that translation's phrasing), then "
                  "chop it:"]
        lines += [f"  {ref} ({ver}): \"{txt}\"" for ref, ver, txt in seed_verses]
    lines += [
        "",
        "Output schema (JSON array only):",
        '[{"text": "...", "verse": "Book Chapter:Verse or 0", "label": "positive|negative", '
        '"subtype": "..."}, ...]',
    ]
    return "\n".join(lines)

# --- Row validation ------------------------------------------------------------------

def validate_row(obj: dict, lookup: dict) -> tuple[dict | None, str | None]:
    """Returns (clean_row, None) or (None, drop_reason)."""
    if not isinstance(obj, dict):
        return None, "not_object"
    text = str(obj.get("text") or "").strip()
    subtype = str(obj.get("subtype") or "").strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        return None, "bad_text"
    if subtype not in TARGETS:
        return None, "bad_subtype"
    expected_label = "positive" if subtype in POSITIVE_SUBTYPES else "negative"
    label = str(obj.get("label") or "").strip().lower()
    if label != expected_label:
        label = expected_label  # subtype is the source of truth
    if subtype in POSITIVE_SUBTYPES:
        canonical = resolve_reference(str(obj.get("verse") or ""), lookup)
        if canonical is None:
            return None, "verse_unresolved"
        # verbatim-quote rows must actually share wording with the real verse
        # (checked against ALL available versions of that verse; the row may
        # quote any translation the seed offered)
        if subtype == "direct_quote_chopped":
            texts = verse_texts(canonical, lookup)
            if not texts or max(content_overlap(text, t) for t in texts.values()) < 3:
                return None, "quote_mismatch"
        verse = canonical
    else:
        verse = "0"
    return {"text": text, "verse": verse, "label": label, "subtype": subtype}, None


def norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())

# --- Batch planning --------------------------------------------------------------------

def plan_batch(remaining: dict[str, int], batch_size: int, rng: random.Random) -> dict[str, int]:
    """Pick 1-3 focus subtypes weighted by remaining counts; allocate the batch."""
    avail = [(st, n) for st, n in remaining.items() if n > 0]
    if not avail:
        return {}
    k = min(len(avail), rng.choice([1, 2, 2, 3]))
    weights = [n for _st, n in avail]
    chosen: dict[str, int] = {}
    pool = avail[:]
    for _ in range(k):
        total = sum(n for _st, n in pool)
        r = rng.uniform(0, total)
        acc = 0
        for idx, (st, n) in enumerate(pool):
            acc += n
            if r <= acc:
                chosen[st] = 0
                pool.pop(idx)
                break
    total_w = sum(remaining[st] for st in chosen)
    left = batch_size
    items = sorted(chosen, key=lambda s: -remaining[s])
    for i, st in enumerate(items):
        if i == len(items) - 1:
            share = left
        else:
            share = max(5, round(batch_size * remaining[st] / total_w))
            share = min(share, left - 5 * (len(items) - i - 1))
        share = min(share, remaining[st])
        chosen[st] = share
        left -= share
    return {st: n for st, n in chosen.items() if n > 0}

# --- Main ------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic sermon-ASR training data.")
    ap.add_argument("--out", default=str(HERE / "synthetic_dataset.jsonl"))
    ap.add_argument("--csv-out", default=str(HERE / "synthetic_dataset.csv"))
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--limit", type=int, default=0, help="stop after N accepted rows (smoke test)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--export-only", action="store_true")
    args = ap.parse_args()

    load_env()
    lookup, seed_pool, versions = load_corpus_lookup()
    n_verses = sum(len(v) for v in lookup.values())
    print(f"[corpus] {len(versions)} versions ({', '.join(versions)}), "
          f"{n_verses} verses each, {len(seed_pool)} seedable verse/version pairs", flush=True)

    # --- resume state: reload existing output -----------------------------------------
    out_path = Path(args.out)
    accepted_counts = {st: 0 for st in TARGETS}
    seen: set[str] = set()
    total_dropped: dict[str, int] = {}
    if out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                seen.add(norm_text(row["text"]))
                if row["subtype"] in accepted_counts:
                    accepted_counts[row["subtype"]] += 1
        print(f"[resume] loaded {len(seen)} existing rows from {out_path.name}", flush=True)

    # --- real anchor rows from the sample CSV ------------------------------------------
    anchors: list[str] = []
    src_csv = HERE / "preacher_text_only.csv"
    if src_csv.exists():
        import csv as csv_mod
        with open(src_csv, newline="", encoding="utf-8") as fh:
            for r in csv_mod.reader(fh):
                if r and r[0].strip() and r[0].strip().lower() != "text":
                    anchors.append(r[0].strip())
    print(f"[anchors] {len(anchors)} real rows available as style anchors", flush=True)

    if args.export_only:
        export_csv(out_path, Path(args.csv_out))
        return 0

    api_key = os.environ.get("GLM_API_KEY")
    base_url = os.environ.get("GLM_BASE_URL")
    model = os.environ.get("GLM_MODEL", "z-ai/glm-5.2")
    if not api_key or not base_url:
        print("[!] GLM_API_KEY / GLM_BASE_URL not set (check .env)", file=sys.stderr)
        return 1

    rng = random.Random(args.seed)
    targets = dict(TARGETS)
    if args.limit:
        total_target = args.limit
        scale = args.limit / sum(targets.values())
        targets = {st: max(1, round(n * scale)) for st, n in targets.items()}
    else:
        total_target = sum(targets.values())

    remaining = {st: max(0, targets[st] - accepted_counts[st]) for st in targets}
    if sum(remaining.values()) == 0:
        print("[done] targets already met; nothing to generate.", flush=True)
        print_summary(accepted_counts, seen, total_dropped, 0)
        export_csv(out_path, Path(args.csv_out))
        return 0

    print(f"[run] target {total_target} rows total; remaining: "
          f"{sum(remaining.values())} across {sum(1 for n in remaining.values() if n)} subtypes", flush=True)

    n_batches = 0
    t0 = time.time()
    with open(out_path, "a", encoding="utf-8") as out_fh:
        while sum(remaining.values()) > 0 and n_batches < MAX_BATCHES:
            n_batches += 1
            plan = plan_batch(remaining, args.batch_size, rng)
            if not plan:
                break
            n_pos = sum(plan.get(st, 0) for st in POSITIVE_SUBTYPES)
            k_seeds = min(8, max(3, n_pos // 4)) if n_pos else 0
            # random version per seed: pool holds every verse x version pair
            seed_verses = [seed_pool[rng.randrange(len(seed_pool))]
                           for _ in range(k_seeds)]
            batch_anchors = rng.sample(anchors, k=min(len(anchors), rng.randint(3, 5)))
            topic = rng.choice(TOPICS)
            chop = rng.choice(CHOP_HINTS)
            split_seq = (n_batches % 6 == 0)

            prompt = build_prompt(plan, batch_anchors, seed_verses, topic, chop, split_seq)
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt}]

            rows_raw = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    raw = llm_generate(messages, api_key, base_url, model)
                    rows_raw = extract_json_array(raw)
                    break
                except Exception as exc:
                    wait = 2 ** attempt
                    print(f"[batch {n_batches}] attempt {attempt} failed: {exc}; "
                          f"retry in {wait}s", flush=True)
                    time.sleep(wait)
            if rows_raw is None:
                print(f"[batch {n_batches}] SKIPPED after {MAX_RETRIES} failures", flush=True)
                continue

            n_acc = n_dup = 0
            for obj in rows_raw:
                row, why = validate_row(obj, lookup)
                if row is None:
                    total_dropped[why] = total_dropped.get(why, 0) + 1
                    continue
                key = norm_text(row["text"])
                if key in seen:
                    n_dup += 1
                    continue
                if remaining.get(row["subtype"], 0) <= 0:
                    continue  # subtype quota already filled by this batch
                seen.add(key)
                remaining[row["subtype"]] -= 1
                accepted_counts[row["subtype"]] += 1
                out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n_acc += 1
            out_fh.flush()

            pos_done = sum(accepted_counts[st] for st in POSITIVE_SUBTYPES)
            neg_done = sum(accepted_counts[st] for st in NEGATIVE_SUBTYPES)
            pos_tgt = sum(targets[st] for st in POSITIVE_SUBTYPES)
            neg_tgt = sum(targets[st] for st in NEGATIVE_SUBTYPES)
            dt = time.time() - t0
            rate = (pos_done + neg_done) / dt * 60 if dt else 0
            print(f"[batch {n_batches}] +{n_acc} rows "
                  f"(dup {n_dup}, dropped {len(rows_raw)-n_acc-n_dup}) | "
                  f"pos {pos_done}/{pos_tgt} neg {neg_done}/{neg_tgt} | "
                  f"{rate:.0f} rows/min", flush=True)
            time.sleep(BATCH_DELAY_S)

    print()
    print_summary(accepted_counts, seen, total_dropped, n_batches)
    export_csv(out_path, Path(args.csv_out))
    return 0


def print_summary(counts: dict, seen: set, dropped: dict, n_batches: int) -> None:
    print("=" * 64)
    print("GENERATION SUMMARY")
    print("=" * 64)
    pos = sum(counts[st] for st in POSITIVE_SUBTYPES)
    neg = sum(counts[st] for st in NEGATIVE_SUBTYPES)
    total = pos + neg
    for st in TARGETS:
        lbl = "pos" if st in POSITIVE_SUBTYPES else "neg"
        print(f"  {st:<24} {lbl}  {counts[st]:>6}")
    print("  " + "-" * 44)
    print(f"  {'TOTAL positive':<24}      {pos:>6}  ({pos/max(1,total)*100:.1f}%)")
    print(f"  {'TOTAL negative':<24}      {neg:>6}  ({neg/max(1,total)*100:.1f}%)")
    print(f"  {'TOTAL rows':<24}      {total:>6}")
    print(f"  unique normalized texts: {len(seen)}  |  batches: {n_batches}")
    if dropped:
        print("  dropped rows by reason:")
        for why, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"    {why:<20} {n:>5}")


def export_csv(jsonl_path: Path, csv_path: Path) -> None:
    import csv as csv_mod
    n = 0
    with open(jsonl_path, encoding="utf-8") as src, \
         open(csv_path, "w", newline="", encoding="utf-8") as dst:
        wtr = csv_mod.writer(dst)
        wtr.writerow(["text", "verse", "label", "subtype"])
        for line in src:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            wtr.writerow([row["text"], row["verse"], row["label"], row["subtype"]])
            n += 1
    print(f"[export] wrote {n} rows -> {csv_path}")


if __name__ == "__main__":
    raise SystemExit(main())
