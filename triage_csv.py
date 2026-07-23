"""Triage a Whisper-transcribed sermon CSV through the offline pipeline.

Reads preacher_text_only.csv (text / verse / label in original speaking order),
runs every UNLABELED row through the real pipeline modules (quote_detect,
retrieve, rerank, score, context) and writes a review CSV sorted for fast
hand-confirmation. The pipeline output is a first draft for a human to
correct, not a finished labeled dataset.

Gold rows (label column already filled) are copied through untouched and kept
at the top of the output. Before touching the unlabeled rows, the pipeline is
run on the gold rows as-if-unlabeled and its suggestions are compared against
the real labels, so the reviewer knows how much to trust the triage.

No audio here, so transcribe.py does not apply. The rolling context window
(prev + candidate + next ~12 words) is reconstructed from CSV row order and
fed through the same buffer.Window shape the live pipeline uses. Blank-text
rows are hard boundaries: no context is bridged across them.

Usage:
    python triage_csv.py
    python triage_csv.py --in preacher_text_only.csv --out preacher_text_only_triaged.csv
"""

from __future__ import annotations

import argparse
import csv
import os

# Fully offline: models + FAISS index must already be cached/built.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

import config
from buffer import Window
import quote_detect
import rerank as rerank_mod
import score as score_mod
from context import SermonContext
from models import get_embedder
from retrieve import Retriever

BAND_ORDER = {config.BAND_AUTOPILOT: 0, config.BAND_REVIEW: 1, config.BAND_IGNORED: 2}
NEXT_WORDS = config.BUFFER_NEXT_WORDS

OUT_COLUMNS = [
    "text", "verse", "label",
    "suggested_label", "suggested_verse", "confidence", "confidence_band",
    "quote_probability", "top_candidate",
    "confirmed_label", "confirmed_verse", "notes",
]


# --- CSV I/O -----------------------------------------------------------------

def read_rows(path: str) -> list[dict]:
    """Read the source CSV, preserving row order.

    The header is ``text,verse,,`` -- the label lives in column index 3.
    Rows are padded to 4 fields; if a row has MORE than 4 fields (an unquoted
    comma slipped into the text), the extras are rejoined into the text so
    verse/label always come from the last columns.
    """
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        rdr = csv.reader(fh)
        next(rdr)  # header
        for i, r in enumerate(rdr):
            if len(r) > 4:
                r = [",".join(r[: len(r) - 3])] + r[len(r) - 3:]
            r = list(r) + [""] * (4 - len(r))
            rows.append({
                "line": i + 2,              # 1-based line number (header = line 1)
                "text": r[0].strip(),
                "verse": r[1].strip(),
                "label": r[3].strip(),
            })
    return rows


# --- Stage 2 adapter: CSV row order -> buffer.Window --------------------------

def build_windows(rows: list[dict]) -> dict[int, Window]:
    """Reconstruct the same Window shape buffer.py produces, from CSV order.

    candidate_text = row N text
    prev_text      = row N-1 text ("" if N is first or row N-1 is blank)
    next_text      = first ~12 words of row N+1 (same blank-row rule)

    Blank-text rows are hard boundaries: they get no window and no context is
    bridged across them. Returns {row_index: Window} for non-blank rows.
    """
    windows: dict[int, Window] = {}
    n = len(rows)
    for i, row in enumerate(rows):
        if not row["text"]:
            continue
        prev_text = rows[i - 1]["text"] if i > 0 and rows[i - 1]["text"] else ""
        next_text = ""
        if i + 1 < n and rows[i + 1]["text"]:
            next_text = " ".join(rows[i + 1]["text"].split()[:NEXT_WORDS])
        full = " ".join(p for p in (prev_text, row["text"], next_text) if p)
        windows[i] = Window(
            index=i,
            text=full,
            candidate_text=row["text"],
            start_time=float(i),   # no timestamps in CSV; row index keeps order
            end_time=float(i),
            prev_text=prev_text,
            next_text=next_text,
        )
    return windows


# --- Pipeline -----------------------------------------------------------------

def run_row(window: Window, detector, retriever: Retriever, ctx: SermonContext,
            accepted_keys: set, accepted_books: dict, settings: config.Settings):
    """Run one row through Stages 3-6. Returns (entry, qprob, ranked).

    Retrieval runs REGARDLESS of the quote-probability threshold: for triage we
    want the top candidate even on borderline/low-scoring rows. Context/history
    trackers update only on surfaced (non-ignored) entries, mirroring run.py.
    """
    qprob = detector.score(window.candidate_text)
    cands = retriever.search_one(window.candidate_text, top_k=settings.top_k)
    if not cands:
        return None, qprob, []
    ranked = rerank_mod.rerank_candidates(
        cands, window.candidate_text, qprob, ctx, accepted_keys, accepted_books,
        weights=settings.weights,
    )
    selected, _ = rerank_mod.select_representative(ranked)
    if selected is None:
        return None, qprob, ranked
    entry = score_mod.build_entry(window, qprob, selected, ranked)
    if entry["confidence_band"] != config.BAND_IGNORED:
        accepted_keys.add(selected.candidate.key)
        accepted_books[selected.candidate.book] = accepted_books.get(selected.candidate.book, 0) + 1
        ctx.add_candidate(selected.candidate, window.text)
    return entry, qprob, ranked


def suggestion_from(entry: dict | None, ranked: list) -> dict:
    """Turn a pipeline entry into the triage suggestion fields."""
    if entry is None:
        return {
            "suggested_label": "negative", "suggested_verse": "0",
            "confidence": "", "confidence_band": "ignored",
            "quote_probability": "", "top_candidate": "",
        }
    positive = entry["confidence_band"] in (config.BAND_AUTOPILOT, config.BAND_REVIEW)
    top1 = f"{ranked[0].candidate.reference} ({ranked[0].final:.2f})" if ranked else ""
    return {
        "suggested_label": "positive" if positive else "negative",
        "suggested_verse": entry["selected_reference"] if positive else "0",
        "confidence": entry["confidence"],
        "confidence_band": entry["confidence_band"],
        "quote_probability": entry["quote_probability"],
        "top_candidate": top1,
    }


def sanity_check(gold: list[dict], windows: dict[int, Window], detector,
                 retriever: Retriever, settings: config.Settings) -> dict:
    """Run the pipeline on gold rows as-if-unlabeled; compare to real labels.

    Uses its own fresh context so it does not pollute the real triage run.
    Returns stats used for the trust read in the final summary.
    """
    ctx = SermonContext()
    accepted_keys: set = set()
    accepted_books: dict = {}

    caught = missed = rejected = flagged = 0
    missed_rows: list[tuple[dict, dict]] = []    # gold positive, pipeline negative
    flagged_rows: list[tuple[dict, dict]] = []   # gold negative, pipeline positive
    all_gold: list[tuple[dict, dict]] = []       # every gold row + suggestion

    for row in gold:
        w = windows.get(row["_idx"])
        if w is None:
            continue
        entry, _q, ranked = run_row(w, detector, retriever, ctx,
                                    accepted_keys, accepted_books, settings)
        sug = suggestion_from(entry, ranked)
        all_gold.append((row, sug))
        gold_pos = row["label"].lower() == "positive"
        if gold_pos and sug["suggested_label"] == "positive":
            caught += 1
        elif gold_pos:
            missed += 1
            missed_rows.append((row, sug, entry))
        elif not gold_pos and sug["suggested_label"] == "negative":
            rejected += 1
        else:
            flagged += 1
            flagged_rows.append((row, sug, entry))

    total_pos = caught + missed
    total_neg = rejected + flagged

    print("=" * 74)
    print(f"SANITY CHECK: pipeline vs {len(gold)} gold rows (run as-if-unlabeled)")
    print("=" * 74)

    # Per-row table so the reviewer sees the confidence separation between
    # gold positives and gold negatives -- this is what calibrates how much
    # to trust the triage suggestions (and where a triage threshold would sit).
    print(f"  {'line':<5} {'gold':<9} {'gold verse':<12} {'pred':<9} {'conf':>6} {'band':<19} top candidate")
    print("  " + "-" * 100)
    for row, sug in all_gold:
        top = sug["top_candidate"] if sug["top_candidate"] else "-"
        mark = "  <-- WRONG" if ((row["label"].lower() == "positive")
                                 != (sug["suggested_label"] == "positive")) else ""
        print(f"  {row['line']:<5} {row['label']:<9} {row['verse']:<12} "
              f"{sug['suggested_label']:<9} {str(sug['confidence']):>6} "
              f"{sug['confidence_band']:<19} {top}{mark}")
    print()

    print(f"Gold positives ({total_pos}): caught {caught}/{total_pos}   missed {missed}")
    print(f"Gold negatives ({total_neg}): correctly rejected {rejected}/{total_neg}   falsely flagged {flagged}")

    if missed_rows:
        print("\nMissed positives (pipeline said negative -- FALSE NEGATIVES):")
        for row, sug, entry in missed_rows:
            print(f"  line {row['line']:<4} gold={row['verse']:<12} band={sug['confidence_band']:<10} "
                  f"conf={sug['confidence']} qprob={sug['quote_probability']}")
            print(f"         text: {row['text'][:90]}")
            print(f"         top:  {sug['top_candidate']}")
    if flagged_rows:
        print("\nFalsely flagged (pipeline said positive -- FALSE POSITIVES):")
        for row, sug, entry in flagged_rows:
            print(f"  line {row['line']:<4} gold=negative    suggested={sug['suggested_verse']:<14} "
                  f"band={sug['confidence_band']:<10} conf={sug['confidence']}")
            print(f"         text: {row['text'][:90]}")
    if not missed_rows and not flagged_rows:
        print("\nPipeline matched every gold row.")
    print()
    return {
        "total_pos": total_pos, "caught": caught, "missed": missed,
        "total_neg": total_neg, "rejected": rejected, "flagged": flagged,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Triage a sermon CSV through the FaithView pipeline.")
    ap.add_argument("--in", dest="src", default="preacher_text_only.csv")
    ap.add_argument("--out", dest="dst", default="preacher_text_only_triaged.csv")
    ap.add_argument("--top-k", type=int, default=config.TOP_K)
    args = ap.parse_args()

    settings = config.Settings(offline=True, top_k=args.top_k)

    rows = read_rows(args.src)
    for i, row in enumerate(rows):
        row["_idx"] = i
    gold = [r for r in rows if r["label"]]
    unlabeled = [r for r in rows if not r["label"] and r["text"]]
    blank = [r for r in rows if not r["label"] and not r["text"]]
    print(f"[load] {len(rows)} rows: {len(gold)} gold, {len(unlabeled)} unlabeled, "
          f"{len(blank)} blank (hard boundaries, skipped)")

    windows = build_windows(rows)

    print("[load] embedder + FAISS index ...")
    embedder = get_embedder(offline=True)
    retriever = Retriever.from_disk(embedder,
                                    index_path=os.path.join(config.INDEX_DIR, "verses.faiss"),
                                    meta_path=os.path.join(config.INDEX_DIR, "verses_meta.json"))
    detector = quote_detect.get_detector("semantic", retriever=retriever)

    # --- Sanity check on gold rows BEFORE touching unlabeled rows ------------
    stats = sanity_check(gold, windows, detector, retriever, settings)

    # --- Triage the unlabeled rows (fresh context, sermon order) -------------
    print(f"[triage] running {len(unlabeled)} unlabeled rows through the pipeline ...")
    ctx = SermonContext()
    accepted_keys: set = set()
    accepted_books: dict = {}

    triaged: list[tuple[dict, dict]] = []
    for n, row in enumerate(unlabeled, 1):
        w = windows[row["_idx"]]
        entry, _q, ranked = run_row(w, detector, retriever, ctx,
                                    accepted_keys, accepted_books, settings)
        triaged.append((row, suggestion_from(entry, ranked)))
        if n % 50 == 0:
            print(f"[triage] {n}/{len(unlabeled)}")

    # --- Sort: gold rows on top (original order), then by band, ---------------
    # --- then confidence descending within each band. --------------------------
    def sort_key(item):
        _row, sug = item
        conf = float(sug["confidence"]) if sug["confidence"] != "" else 0.0
        return (BAND_ORDER.get(sug["confidence_band"], 3), -conf)

    triaged.sort(key=sort_key)

    # --- Write the review CSV ---------------------------------------------------
    with open(args.dst, "w", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(OUT_COLUMNS)
        for row in gold:  # unchanged, suggestions blank, confirmation cols blank
            wtr.writerow([row["text"], row["verse"], row["label"],
                          "", "", "", "", "", "", "", "", ""])
        for row, sug in triaged:
            wtr.writerow([row["text"], row["verse"], row["label"],
                          sug["suggested_label"], sug["suggested_verse"],
                          sug["confidence"], sug["confidence_band"],
                          sug["quote_probability"], sug["top_candidate"],
                          "", "", ""])  # confirmed_label / confirmed_verse / notes: hand-fill

    # --- Final summary -----------------------------------------------------------
    bands: dict[str, int] = {}
    positives = 0
    for _row, sug in triaged:
        bands[sug["confidence_band"]] = bands.get(sug["confidence_band"], 0) + 1
        if sug["suggested_label"] == "positive":
            positives += 1

    n_ap = bands.get(config.BAND_AUTOPILOT, 0)
    n_rq = bands.get(config.BAND_REVIEW, 0)
    n_ig = bands.get(config.BAND_IGNORED, 0)

    print("=" * 74)
    print(f"TRIAGE COMPLETE -> {args.dst}")
    print("=" * 74)
    print(f"Rows written: {len(gold)} gold (top, unchanged) + {len(triaged)} triaged "
          f"({len(blank)} blank boundary rows omitted)")
    print(f"Band counts:  autopilot-eligible: {n_ap}   review queue: {n_rq}   ignored: {n_ig}")
    print(f"Suggested positives: {positives}   suggested negatives: {len(triaged) - positives}")
    print()
    print("Trust read from the gold sanity check:")
    if stats["total_pos"]:
        print(f"  - pipeline caught {stats['caught']}/{stats['total_pos']} real quotes "
              f"(missed {stats['missed']} -> false negatives DO land in 'ignored')")
    if stats["total_neg"]:
        print(f"  - pipeline rejected {stats['rejected']}/{stats['total_neg']} real negatives "
              f"(falsely flagged {stats['flagged']} -> check 'positive' suggestions for these)")
    print()
    print("Review guidance:")
    print(f"  * {n_ap + n_rq} rows suggested POSITIVE (autopilot + review queue) -- "
          f"batch-confirm friendly, but spot-check; sanity check flagged "
          f"{stats['flagged']}/{stats['total_neg']} gold negatives as positive.")
    print(f"  * {n_ig} rows suggested negative (ignored) -- needs the closest look; "
          f"sanity check missed {stats['missed']}/{stats['total_pos']} gold positives here.")

    # Where the false negatives are most likely hiding: highest-confidence
    # ignored rows (already at the top of the 'ignored' block in the CSV).
    ignored_sorted = [t for t in triaged if t[1]["confidence_band"] == config.BAND_IGNORED]
    if ignored_sorted:
        print()
        print(f"Top {min(10, len(ignored_sorted))} highest-confidence 'ignored' rows "
              f"(prime false-negative candidates -- review these first):")
        for row, sug in ignored_sorted[:10]:
            print(f"  {sug['confidence']:>6}  {sug['top_candidate']:<28} "
                  f"{row['text'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
