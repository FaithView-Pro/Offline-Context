"""FaithView Pro -- offline semantic verse-detection engine (batch POC).

Pipeline (exact order):
  1. transcribe.py  -- Whisper -> timestamped segments
  2. buffer.py      -- rolling sentence windows (prev + current + next words)
  3. quote_detect.py-- local classifier; only >= threshold proceed
  4. retrieve.py    -- bge-small + FAISS top-k across AMP + NKJV combined
  5. rerank.py      -- hybrid blend (45/20/15/10/10) + optional cross-encoder
  6. score.py       -- confidence + band (pure local decision)

CLI:
    python run.py --audio sermon.wav --out results.json
    python run.py --transcript transcript.json --out results.json   # no Whisper
    python run.py ... --cross-encoder --include-ignored --quote-threshold 0.65
"""

from __future__ import annotations

import argparse
import json
import os
import time

import config


def _fmt_time(s: float) -> str:
    m, sec = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{sec:02d}"


def _print_table(entries: list[dict]) -> None:
    print("\n=== FaithView Pro -- detected scripture quotes ===")
    hdr = f"{'time':>10}  {'verse':<22} {'trans':<6} {'band':<20} {'conf':>5} {'qprob':>6}"
    print(hdr)
    print("-" * len(hdr))
    for e in entries:
        print(f"{_fmt_time(e['start_time']):>10}  {e['selected_reference']:<22} "
              f"{e['translation']:<6} {e['confidence_band']:<20} "
              f"{e['confidence']:>5.2f} {e['quote_probability']:>6.2f}")
    bands = {}
    for e in entries:
        bands[e["confidence_band"]] = bands.get(e["confidence_band"], 0) + 1
    print("-" * len(hdr))
    print(f"total: {len(entries)}  |  " + "  ".join(f"{k}: {v}" for k, v in bands.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="FaithView Pro offline verse detection.")
    ap.add_argument("--audio", help="path to sermon audio (wav/mp3)")
    ap.add_argument("--transcript", help="path to a pre-existing transcript JSON "
                                         "(list of {text,start_time,end_time}); skips Whisper")
    ap.add_argument("--out", default="results.json", help="output JSON path")
    ap.add_argument("--index-dir", default=config.INDEX_DIR)
    ap.add_argument("--quote-detector", default="heuristic", choices=["heuristic", "onnx", "hybrid"])
    ap.add_argument("--quote-threshold", type=float, default=config.QUOTE_THRESHOLD)
    ap.add_argument("--top-k", type=int, default=config.TOP_K)
    ap.add_argument("--cross-encoder", action="store_true", help="enable optional cross-encoder re-rank")
    ap.add_argument("--include-ignored", action="store_true", help="emit <0.80 entries too")
    ap.add_argument("--offline", action="store_true", default=True, help="force local-files-only (default)")
    ap.add_argument("--online", action="store_true", help="allow model downloads (first run)")
    ap.add_argument("--whisper-model", default=config.WHISPER_MODEL)
    ap.add_argument("--language", default=config.WHISPER_LANGUAGE)
    ap.add_argument("--word-timestamps", action="store_true", help="request Whisper word timestamps")
    ap.add_argument("--no-semantic-detect", action="store_true",
                    help="disable semantic pre-check in quote detection (pure heuristic only)")
    args = ap.parse_args()

    if not args.audio and not args.transcript:
        ap.error("provide --audio or --transcript")
    if args.online:
        args.offline = False

    offline = args.offline
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_DATASETS_OFFLINE"] = "1"

    settings = config.Settings(
        index_dir=args.index_dir,
        whisper_model=args.whisper_model,
        whisper_language=args.language,
        quote_threshold=args.quote_threshold,
        top_k=args.top_k,
        use_cross_encoder=args.cross_encoder,
        offline=offline,
    )

    # --- Stage 1: transcription -------------------------------------------
    t0 = time.time()
    if args.audio:
        import transcribe
        print(f"[1/6] transcribing {args.audio} with Whisper '{settings.whisper_model}' ...")
        segments = transcribe.transcribe(args.audio, settings, word_timestamps=True)
    else:
        import transcribe
        with open(args.transcript, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        segments = transcribe.segments_from_dicts(data)
        print(f"[1/6] loaded {len(segments)} segments from {args.transcript}")
    print(f"[1/6] transcription: {len(segments)} segments in {time.time()-t0:.1f}s")

    # --- Stage 2: sentence buffering --------------------------------------
    import buffer
    windows = buffer.buffer_segments(segments)
    print(f"[2/6] sentence windows: {len(windows)}")

    # --- Load embedder + retriever early (shared by Stage 3 detection
    #     and Stage 4 retrieval; the semantic quote detector needs the index
    #     for its proximity pre-check) ---------------------------------------
    import models
    from retrieve import Retriever
    embedder = models.get_embedder(offline=offline)
    retriever = Retriever.from_disk(embedder,
                                    index_path=os.path.join(settings.index_dir, "verses.faiss"),
                                    meta_path=os.path.join(settings.index_dir, "verses_meta.json"))

    # --- Stage 3: quote detection -----------------------------------------
    # Default to 'semantic' detector (heuristic + FAISS proximity boost) when
    # the index is available; falls back to pure heuristic via --quote-detector heuristic.
    import quote_detect
    detector_kind = args.quote_detector
    if detector_kind == "heuristic" and not args.no_semantic_detect:
        detector_kind = "semantic"
    detector = quote_detect.get_detector(detector_kind, retriever=retriever) if detector_kind == "semantic" \
        else quote_detect.get_detector(detector_kind)
    passed, all_scored = quote_detect.filter_quote_windows(windows, detector, settings.quote_threshold)
    print(f"[3/6] quote detection ({detector_kind}): {len(passed)}/{len(windows)} windows >= {settings.quote_threshold}")
    if detector_kind == "semantic" and not passed:
        print("[3/6] no semantic passes; retrying pure heuristic at lower bar for visibility...")
        h_detector = quote_detect.get_detector("heuristic")
        h_passed, _ = quote_detect.filter_quote_windows(windows, h_detector, 0.30)
        for w, p in h_passed[:5]:
            print(f"        heuristic {p:.2f}: {w.candidate_text[:70]}")
    passed.sort(key=lambda wp: wp[0].start_time)

    if not passed:
        print("[!] no windows passed quote detection; writing empty result.")
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump([], fh, indent=2)
        return 0

    # --- Stage 4: semantic retrieval --------------------------------------
    # Retrieve using the CANDIDATE sentence (the actual quote), not the full
    # prev+current+next window. The full window dilutes the query with
    # non-scripture context and degrades retrieval. Re-ranking (Stage 5)
    # still uses window.text for lexical/context scoring.
    query_texts = [w.candidate_text for w, _ in passed]
    t1 = time.time()
    batch_results = retriever.search(query_texts, top_k=settings.top_k)
    print(f"[4/6] retrieval: {len(batch_results)} x top-{settings.top_k} in {time.time()-t1:.1f}s")

    # --- Stage 5 + 6: re-rank, score, and update running context/history ---
    import rerank as rerank_mod
    from context import SermonContext
    import score as score_mod

    cross_encoder = models.get_cross_encoder(offline=offline) if settings.use_cross_encoder else None
    if settings.use_cross_encoder and cross_encoder is None:
        print("[5/6] cross-encoder requested but unavailable; continuing without it.")

    ctx = SermonContext()
    accepted_keys: set[str] = set()
    accepted_books: dict = {}
    entries: list[dict] = []

    for (window, qprob), candidates in zip(passed, batch_results):
        if not candidates:
            continue
        ranked = rerank_mod.rerank_candidates(
            candidates, window.candidate_text, qprob, ctx, accepted_keys, accepted_books,
            weights=settings.weights, cross_encoder=cross_encoder,
        )
        selected, _ = rerank_mod.select_representative(ranked)
        if selected is None:
            continue
        entry = score_mod.build_entry(window, qprob, selected, ranked)
        if score_mod.is_surfaced(entry, include_ignored=args.include_ignored):
            entries.append(entry)
            # Update running trackers with accepted verses (chronological).
            accepted_keys.add(selected.candidate.key)
            accepted_books[selected.candidate.book] = accepted_books.get(selected.candidate.book, 0) + 1
            ctx.add_candidate(selected.candidate, window.text)

    entries.sort(key=lambda e: e["start_time"])
    print(f"[5/6] re-rank + [6/6] scoring: {len(entries)} surfaced quotes")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)
    print(f"[done] wrote {args.out}")

    _print_table(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
