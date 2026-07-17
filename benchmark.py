"""Benchmark: measure speed of each pipeline stage on this CPU.

Run:
    python benchmark.py                    # text search + full audio pipeline
    python benchmark.py --audio BIM.mp3    # full pipeline only
    python benchmark.py --text-only        # text search only (no audio)
"""

from __future__ import annotations

import argparse
import os
import time

import config


def fmt(ms: float) -> str:
    if ms < 1:
        return f"{ms*1000:.0f} us"
    if ms < 1000:
        return f"{ms:.1f} ms"
    return f"{ms/1000:.2f} s"


def bench_text_search(queries: list[str], top_k: int = 10, rounds: int = 5):
    """Benchmark pure text -> verse retrieval (search.py path)."""
    print("\n" + "=" * 70)
    print("  TEXT SEARCH BENCHMARK (search.py path)")
    print("=" * 70)

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    # --- Model load time (one-time cost) ---
    t0 = time.time()
    from models import get_embedder
    from retrieve import Retriever
    embedder = get_embedder(offline=True)
    t_load_model = time.time() - t0
    print(f"\n  [load] embedder model (one-time):     {fmt(t_load_model*1000)}")

    t0 = time.time()
    retriever = Retriever.from_disk(embedder)
    t_load_index = time.time() - t0
    print(f"  [load] FAISS index + metadata:         {fmt(t_load_index*1000)}")
    print(f"         index size: {retriever.index.ntotal} vectors x {config.EMBED_DIM} dims")

    # --- Per-query timing ---
    from rerank import rerank_candidates
    from context import SermonContext

    print(f"\n  {'query':<55} {'embed':>10} {'faiss':>10} {'rerank':>10} {'total':>10}")
    print("  " + "-" * 100)

    all_embed, all_faiss, all_rerank, all_total = [], [], [], []

    for q in queries:
        embed_times, faiss_times, rerank_times, total_times = [], [], [], []

        for _ in range(rounds):
            tt = time.time()

            # Embed query
            t1 = time.time()
            qv = embedder.encode_queries([q])
            t_embed = (time.time() - t1) * 1000

            # FAISS search
            import numpy as np
            from retrieve import _normalize
            qv_n = _normalize(qv)
            t2 = time.time()
            D, I = retriever.index.search(qv_n, top_k)
            t_faiss = (time.time() - t2) * 1000

            # Build candidates
            cands = []
            for score, idx in zip(D[0], I[0]):
                if idx >= 0:
                    from retrieve import Candidate
                    cands.append(Candidate.from_meta(retriever.meta[int(idx)], float(score)))

            # Re-rank
            t3 = time.time()
            ctx = SermonContext()
            ranked = rerank_candidates(cands, q, 0.90, ctx, set(), {})
            t_rerank = (time.time() - t3) * 1000

            t_total = (time.time() - tt) * 1000
            embed_times.append(t_embed)
            faiss_times.append(t_faiss)
            rerank_times.append(t_rerank)
            total_times.append(t_total)

        avg_e = sum(embed_times) / rounds
        avg_f = sum(faiss_times) / rounds
        avg_r = sum(rerank_times) / rounds
        avg_t = sum(total_times) / rounds

        all_embed.append(avg_e)
        all_faiss.append(avg_f)
        all_rerank.append(avg_r)
        all_total.append(avg_t)

        print(f"  {q[:55]:<55} {fmt(avg_e):>10} {fmt(avg_f):>10} {fmt(avg_r):>10} {fmt(avg_t):>10}")

    n = len(queries)
    print("  " + "-" * 100)
    print(f"  {'AVERAGE (' + str(n) + ' queries, ' + str(rounds) + ' rounds)':<55} "
          f"{fmt(sum(all_embed)/n):>10} {fmt(sum(all_faiss)/n):>10} "
          f"{fmt(sum(all_rerank)/n):>10} {fmt(sum(all_total)/n):>10}")
    print(f"\n  Throughput: {1000/(sum(all_total)/n):.0f} queries/sec (text search)")


def bench_audio_pipeline(audio_path: str):
    """Benchmark the full audio pipeline (transcribe + detect + retrieve + rerank)."""
    print("\n" + "=" * 70)
    print(f"  AUDIO PIPELINE BENCHMARK ({audio_path})")
    print("=" * 70)

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    import ffprobe_utils
    duration = ffprobe_utils.get_duration(audio_path)
    print(f"\n  Audio duration: {duration:.1f}s ({duration/60:.1f} min)")

    stages = {}

    # --- Stage 1: Transcription ---
    import transcribe
    t0 = time.time()
    segments = transcribe.transcribe(audio_path, config.Settings(), word_timestamps=True)
    stages["1. transcribe"] = (time.time() - t0) * 1000
    print(f"  [1/6] transcribe:      {fmt(stages['1. transcribe']):>10}  ({len(segments)} segments, "
          f"{stages['1. transcribe']/duration:.1f}x realtime)")

    # --- Stage 2: Buffering ---
    import buffer
    t0 = time.time()
    windows = buffer.buffer_segments(segments)
    stages["2. buffer"] = (time.time() - t0) * 1000
    print(f"  [2/6] buffer:          {fmt(stages['2. buffer']):>10}  ({len(windows)} windows)")

    # --- Load embedder + index ---
    from models import get_embedder
    from retrieve import Retriever
    t0 = time.time()
    embedder = get_embedder(offline=True)
    retriever = Retriever.from_disk(embedder)
    stages["load models"] = (time.time() - t0) * 1000
    print(f"  [load] embedder+index: {fmt(stages['load models']):>10}")

    # --- Stage 3: Quote detection ---
    import quote_detect
    base = quote_detect.HeuristicQuoteDetector()
    sem_det = quote_detect.SemanticQuoteDetector(base, retriever)
    t0 = time.time()
    passed, all_scored = quote_detect.filter_quote_windows(windows, sem_det, config.QUOTE_THRESHOLD)
    stages["3. quote_detect"] = (time.time() - t0) * 1000
    print(f"  [3/6] quote detect:    {fmt(stages['3. quote_detect']):>10}  "
          f"({len(passed)}/{len(windows)} passed, {len(all_scored)} windows scored)")

    # --- Stage 4: Retrieval ---
    t0 = time.time()
    query_texts = [w.candidate_text for w, _ in passed]
    batch_results = retriever.search(query_texts, top_k=config.TOP_K)
    stages["4. retrieve"] = (time.time() - t0) * 1000
    print(f"  [4/6] retrieval:       {fmt(stages['4. retrieve']):>10}  "
          f"({len(batch_results)} queries x top-{config.TOP_K})")
    if passed:
        per_q = stages["4. retrieve"] / len(passed)
        print(f"                        ({fmt(per_q)} per query)")

    # --- Stage 5+6: Re-rank + score ---
    import rerank as rerank_mod
    from context import SermonContext
    import score as score_mod
    ctx = SermonContext()
    accepted_keys = set()
    accepted_books = {}
    entries = []

    t0 = time.time()
    for (window, qprob), candidates in zip(passed, batch_results):
        if not candidates:
            continue
        ranked = rerank_mod.rerank_candidates(
            candidates, window.candidate_text, qprob, ctx, accepted_keys, accepted_books,
            weights=config.RERANK_WEIGHTS,
        )
        selected, _ = rerank_mod.select_representative(ranked)
        if selected is None:
            continue
        entry = score_mod.build_entry(window, qprob, selected, ranked)
        if score_mod.is_surfaced(entry, include_ignored=True):
            entries.append(entry)
            accepted_keys.add(selected.candidate.key)
            accepted_books[selected.candidate.book] = accepted_books.get(selected.candidate.book, 0) + 1
            ctx.add_candidate(selected.candidate, window.text)
    stages["5+6. rerank+score"] = (time.time() - t0) * 1000
    print(f"  [5+6] rerank+score:    {fmt(stages['5+6. rerank+score']):>10}  ({len(entries)} entries)")

    # --- Summary ---
    total_pipeline = sum(v for k, v in stages.items() if k != "load models")
    print("\n  " + "-" * 60)
    print(f"  {'STAGE':<25} {'TIME':>12} {'% of total':>12}")
    print("  " + "-" * 60)
    for k, v in stages.items():
        pct = v / total_pipeline * 100 if total_pipeline else 0
        label = k if k != "load models" else f"({k})"
        print(f"  {label:<25} {fmt(v):>12} {pct:>10.1f}%")
    print("  " + "-" * 60)
    print(f"  {'TOTAL (excl. load)':<25} {fmt(total_pipeline):>12}")
    print(f"  {'Audio duration':<25} {fmt(duration*1000):>12}")
    print(f"  {'Processing ratio':<25} {total_pipeline/(duration*1000):.2f}x realtime")
    print(f"  {'Throughput':<25} {duration/(total_pipeline/1000):.1f}s audio / s processing")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark FaithView Pro pipeline stages.")
    ap.add_argument("--audio", default="BIM.mp3", help="audio file for pipeline benchmark")
    ap.add_argument("--text-only", action="store_true", help="skip audio benchmark")
    ap.add_argument("--rounds", type=int, default=5, help="rounds per text query")
    args = ap.parse_args()

    queries = [
        "I will put my spirit within you and cause you to walk in my statutes",
        "the lord is my shepherd I shall not want",
        "love your enemies and pray for those who persecute you",
        "for god so loved the world that he gave his only begotten son",
        "faith is the substance of things hoped for the evidence of things not seen",
    ]

    bench_text_search(queries, top_k=10, rounds=args.rounds)

    if not args.text_only and os.path.exists(args.audio):
        bench_audio_pipeline(args.audio)
    elif not args.text_only:
        print(f"\n  [skip] audio file not found: {args.audio}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
