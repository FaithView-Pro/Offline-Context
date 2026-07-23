#!/usr/bin/env python3
"""
Convert transcripts/*.json (from batch_transcribe.py) into a labeling-ready CSV.

Segmentation strategy (v3):
  - A large trigger library (single words AND multi-word phrases) is matched
    against the transcript text. Each trigger has its own LOOK-BEHIND
    distance - when found, the line is forced to START that many words
    BEFORE the trigger, so you get the lead-up context rather than the line
    starting mid-thought right at the trigger.
  - Categories: navigation ("turn with me", "go to", "flip to"...), bible
    handling ("open your bible", "grab your bibles"...), verse/chapter
    references ("next verse", "first verse", "chapter"...), reading
    instructions ("read together", "continue reading"...), and translation
    call-outs ("read from the niv", "kjv says", "another translation
    says"...) - plus all 66 Bible book names.
  - Between trigger-forced breaks, lines target ~9 words, preferring a
    sentence-ending boundary (. ? !) within a 5-14 word window.
  - Each row gets a pre-filled suggested label/subtype so you're correcting
    rows, not typing from blank.

Output CSV columns:
  id, source_file, start_time, end_time, text,
  matched_trigger, auto_score, label, subtype

Usage:
    python build_labeling_csv.py
    python build_labeling_csv.py --transcripts-dir transcripts --out labeling_dataset.csv
    python build_labeling_csv.py --list-triggers      # just print the trigger library and exit
"""

import argparse
import bisect
import csv
import json
import re
from pathlib import Path

# ===========================================================================
# TRIGGER LIBRARY  (phrase -> look-behind word count)
# ===========================================================================
TRIGGERS = {}


def _add(phrases, lookback):
    for p in phrases:
        TRIGGERS.setdefault(p.lower(), lookback)


# --- single-word attribution/reading cues -------------------------------
_add(["says", "said", "saying", "verse", "verses", "chapter", "chapters",
      "bible", "scripture", "scriptures", "written", "writes", "wrote"], 6)
_add(["according"], 3)
_add(["testament"], 4)

# --- navigation phrases ("turn with me", "go to", "flip to"...) ---------
NAV_VERBS = ["turn", "turn with me", "let's turn", "lets turn",
             "let us turn", "go", "let's go", "lets go", "let us go",
             "flip", "skip", "jump", "come back", "go back", "get back"]
NAV_JOINS = ["to", "over to", "back to", "ahead to", "on to"]
for v in NAV_VERBS:
    for j in NAV_JOINS:
        _add([f"{v} {j}"], 6)
_add(["turn with me", "look with me", "look at", "notice in",
      "if you look at", "let's look at", "lets look at", "as you look at",
      "come with me to", "join me in"], 6)

# --- bible-handling phrases ("open your bible", "grab your bibles"...) --
OPEN_VERBS = ["open", "open up", "grab", "take", "pull out", "get out",
              "take out", "reach for", "pick up"]
BIBLE_NOUNS = ["your bible", "your bibles", "the bible", "the scriptures",
               "your bible to", "your bibles to"]
for v in OPEN_VERBS:
    for n in BIBLE_NOUNS:
        _add([f"{v} {n}"], 6)

# --- verse / chapter reference phrases -----------------------------------
_add(["next verse", "previous verse", "first verse", "second verse",
      "third verse", "fourth verse", "last verse", "final verse",
      "opening verse", "closing verse", "that verse", "this verse",
      "the verse", "verse number", "next chapter", "previous chapter",
      "same chapter", "that chapter", "this chapter"], 6)

# --- reading-instruction phrases -----------------------------------------
_add(["read together", "let's read", "lets read", "let us read",
      "read along", "read with me", "continue reading", "keep reading",
      "read on", "reading on", "as we read", "let's continue reading",
      "lets continue reading", "read it together", "let's read together",
      "lets read together", "follow along", "read this together"], 5)

# --- translation call-outs -------------------------------------------------
TRANSLATIONS = ["niv", "amp", "amplified", "nkjv", "kjv", "esv", "nlt",
                "msg", "message", "nasb", "csb", "nrsv", "asv"]
TEMPLATES = ["read from the {t}", "in the {t}", "the {t} says",
             "{t} says", "according to the {t}", "the {t} version says",
             "the {t} translation says"]
for t in TRANSLATIONS:
    for tmpl in TEMPLATES:
        _add([tmpl.format(t=t)], 6)
_add(["another translation says", "other versions say",
      "some translations say", "in another version",
      "a different translation says", "other translations say",
      "different versions say"], 6)

# --- misc / skip / continue -----------------------------------------------
_add(["skip to", "skip over to", "skip ahead to", "continue on",
      "moving on to", "next up", "we move to", "we go now to"], 6)

# --- all 66 Bible books -----------------------------------------------------
BIBLE_BOOKS = [
    "genesis", "exodus", "leviticus", "numbers", "deuteronomy", "joshua",
    "judges", "ruth", "samuel", "kings", "chronicles", "ezra", "nehemiah",
    "esther", "job", "psalms", "psalm", "proverbs", "ecclesiastes",
    "solomon", "isaiah", "jeremiah", "lamentations", "ezekiel", "daniel",
    "hosea", "joel", "amos", "obadiah", "jonah", "micah", "nahum",
    "habakkuk", "zephaniah", "haggai", "zechariah", "malachi", "matthew",
    "mark", "luke", "john", "acts", "romans", "corinthians", "galatians",
    "ephesians", "philippians", "colossians", "thessalonians", "timothy",
    "titus", "philemon", "hebrews", "james", "peter", "jude", "revelation",
]
_add(BIBLE_BOOKS, 6)

# sort longest-first (by word count, then char length) so multi-word
# phrases are tried before their single-word substrings
_SORTED_TRIGGERS = sorted(TRIGGERS.keys(), key=lambda p: (-len(p.split()), -len(p)))
_TRIGGER_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _SORTED_TRIGGERS) + r")\b",
    re.IGNORECASE,
)

REFERENCE_PATTERN = re.compile(
    r"\b(?:[1-3]\s?)?[A-Z][a-z]+\s\d{1,3}[:.]\d{1,3}\b|\b\d{1,3}[:.]\d{1,3}\b"
)
ARCHAIC_WORDS = {"thee", "thou", "thy", "hath", "saith", "doth", "behold", "ye", "unto"}
NEGATIVE_MARKERS = [
    "breaking news", "stock market", "knock knock", "the weather",
    "traffic on", "marketplace", "let's laugh", "funny story",
]
SENT_END_CHARS = (".", "?", "!")


# ===========================================================================
def load_words(transcript_path: Path):
    data = json.loads(transcript_path.read_text())
    words = []
    for seg in data.get("segments", []):
        seg_words = seg.get("words")
        if seg_words:
            for w in seg_words:
                words.append({
                    "text": w.get("text", "").strip(),
                    "start_time": w.get("start_time", seg.get("start_time")),
                    "end_time": w.get("end_time", seg.get("end_time")),
                })
        else:
            toks = seg.get("text", "").split()
            if not toks:
                continue
            dur = (seg.get("end_time", 0) - seg.get("start_time", 0)) / max(len(toks), 1)
            for i, t in enumerate(toks):
                words.append({
                    "text": t,
                    "start_time": seg.get("start_time", 0) + i * dur,
                    "end_time": seg.get("start_time", 0) + (i + 1) * dur,
                })
    return [w for w in words if w["text"]]


def find_forced_breaks(words):
    """Match the trigger library against the joined transcript text and
    return dict: word_index -> matched_trigger_phrase, where word_index is
    already adjusted by that trigger's look-behind distance."""
    joined_parts = []
    offsets = []  # (char_start_of_word, word_index), ascending
    pos = 0
    for i, w in enumerate(words):
        offsets.append((pos, i))
        joined_parts.append(w["text"])
        pos += len(w["text"]) + 1
    full_text = " ".join(joined_parts)
    offset_positions = [o for o, _ in offsets]

    breaks = {}
    for m in _TRIGGER_RE.finditer(full_text):
        phrase = m.group(0).lower()
        lookback = TRIGGERS.get(phrase)
        if lookback is None:
            continue
        start_char = m.start()
        pos_idx = bisect.bisect_right(offset_positions, start_char) - 1
        pos_idx = max(pos_idx, 0)
        word_idx = offsets[pos_idx][1]
        fs = max(0, word_idx - lookback)
        if fs not in breaks:
            breaks[fs] = phrase
    return breaks


def merge_close_breaks(breaks, min_gap):
    """When several triggers fire within a few words of each other (e.g.
    'Proverbs', 'chapter', 'verse', 'scripture', 'says' all in one clause),
    keep only the earliest break in each cluster instead of fragmenting
    into tiny 1-2 word lines. Keeps the trigger with the most lead-up
    context (the earliest one)."""
    merged = {}
    last_kept = -min_gap - 1
    for fs in sorted(breaks.keys()):
        if fs - last_kept >= min_gap:
            merged[fs] = breaks[fs]
            last_kept = fs
    return merged


def build_lines(words, break_map, target_words, min_words, max_words):
    forced_starts = sorted(break_map.keys())
    lines = []
    n = len(words)
    cursor = 0
    fi = 0
    while cursor < n:
        while fi < len(forced_starts) and forced_starts[fi] <= cursor:
            fi += 1
        next_start = forced_starts[fi] if fi < len(forced_starts) else None

        if next_start is not None and next_start - cursor <= max_words:
            end = next_start if next_start > cursor else cursor + 1
        else:
            end = None
            for j in range(cursor + min_words - 1, min(cursor + max_words, n)):
                if words[j]["text"].rstrip().endswith(SENT_END_CHARS):
                    end = j + 1
                    break
            if end is None:
                end = min(cursor + target_words, n)

        end = max(end, cursor + 1)
        chunk = words[cursor:end]
        trig = break_map.get(cursor, "")
        lines.append({
            "text": " ".join(w["text"] for w in chunk),
            "start_time": chunk[0]["start_time"],
            "end_time": chunk[-1]["end_time"],
            "matched_trigger": trig,
        })
        cursor = end
    return lines


def suggest_label(text: str, matched_trigger: str):
    lower = text.lower()
    score = 0.1
    has_ref = bool(REFERENCE_PATTERN.search(text))
    has_archaic = any(w in lower.split() for w in ARCHAIC_WORDS)
    has_negative = any(neg in lower for neg in NEGATIVE_MARKERS)

    if matched_trigger:
        score += 0.5
    if has_ref:
        score += 0.5
    if has_archaic:
        score += 0.3
    if has_negative:
        score -= 0.4

    score = max(0.0, min(1.0, score))

    if has_negative:
        return score, "negative", "negative"
    if score >= 0.5:
        subtype = "explicit_reference" if has_ref else "cue_phrase"
        return score, "positive", subtype
    return score, "negative", "negative"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts-dir", default="transcripts")
    ap.add_argument("--out", default="labeling_dataset.csv")
    ap.add_argument("--target-words", type=int, default=9)
    ap.add_argument("--min-words", type=int, default=5)
    ap.add_argument("--max-words", type=int, default=14)
    ap.add_argument("--list-triggers", action="store_true")
    args = ap.parse_args()

    if args.list_triggers:
        for p in sorted(TRIGGERS):
            print(f"{p!r}: lookback={TRIGGERS[p]}")
        print(f"\nTotal triggers: {len(TRIGGERS)}")
        return

    tdir = Path(args.transcripts_dir)
    files = sorted(f for f in tdir.glob("*.json") if f.name != "batch_transcribe.log")

    rows = []
    row_id = 1
    for f in files:
        words = load_words(f)
        if not words:
            print(f"WARNING: no words extracted from {f.name}, skipping")
            continue
        break_map = find_forced_breaks(words)
        break_map = merge_close_breaks(break_map, args.min_words)
        lines = build_lines(words, break_map, args.target_words, args.min_words, args.max_words)

        for line in lines:
            score, label, subtype = suggest_label(line["text"], line["matched_trigger"])
            rows.append({
                "id": row_id,
                "source_file": f.stem,
                "start_time": round(line["start_time"], 2) if line["start_time"] is not None else "",
                "end_time": round(line["end_time"], 2) if line["end_time"] is not None else "",
                "text": line["text"],
                "matched_trigger": line["matched_trigger"],
                "auto_score": round(score, 2),
                "label": label,
                "subtype": subtype,
            })
            row_id += 1

        print(f"{f.name}: {len(lines)} lines")

    out_path = Path(args.out)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "id", "source_file", "start_time", "end_time", "text",
            "matched_trigger", "auto_score", "label", "subtype",
        ])
        writer.writeheader()
        writer.writerows(rows)

    pos = sum(1 for r in rows if r["label"] == "positive")
    neg = len(rows) - pos
    print("-" * 60)
    print(f"Trigger library size: {len(TRIGGERS)}")
    print(f"Total rows: {len(rows)}")
    print(f"Suggested positive: {pos} ({pos/len(rows)*100:.1f}%)")
    print(f"Suggested negative: {neg} ({neg/len(rows)*100:.1f}%)")
    print(f"Written to {out_path.resolve()}")


if __name__ == "__main__":
    main()
