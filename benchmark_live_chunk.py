#!/usr/bin/env python3
"""
Benchmark per-chunk transcription latency for live mic use, comparing
model sizes and beam_size settings against actual chunk length.
Extended (Task 1) to also measure the FULL pipeline per-chunk time:
transcription + buffer + intent_router + onnx classifier + FAISS retrieve +
rerank + score.

Usage:
    python benchmark_live_chunk.py --audio BIM.mp3 --chunk-seconds 6.5
    python benchmark_live_chunk.py --audio BIM.mp3 --full-pipeline
    
    The default chunk now reflects the WIDENED 5s new-audio + 1.5s overlap
    = 6.5s tiles (was 4s/1s = 5s tiles). This widening is NECESSARY for the
    small.en model to maintain real-time margin — without it, small.en at
    beam=1 on a 5s tile would leave only ~0.3s for the rest of the pipeline.
"""
import argparse
import time
import numpy as np
from faster_whisper import WhisperModel


def load_audio_chunk(path, seconds, sr=16000):
    import soundfile as sf
    data, file_sr = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if file_sr != sr:
        from scipy.signal import resample_poly
        import math
        g = math.gcd(sr, file_sr)
        data = resample_poly(data, sr // g, file_sr // g)
    n_samples = int(seconds * sr)
    return data[:n_samples].astype(np.float32)


def bench_whisper_only(chunk, chunk_seconds, models, beam_sizes, runs, window_budget):
    """Original whisper-only benchmark, now uses dynamic window_budget."""
    print(f"\n{'model':<12} {'beam':<6} {'avg_time(s)':<12} {'rt_factor':<16} "
          f"fits in {window_budget:.1f}s?")
    print("-" * 75)
    for model_name in models:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        for beam in beam_sizes:
            times = []
            for _ in range(runs):
                t0 = time.time()
                segments, info = model.transcribe(
                    chunk, language="en", beam_size=beam,
                    vad_filter=True, condition_on_previous_text=False,
                )
                list(segments)
                times.append(time.time() - t0)
            avg = sum(times) / len(times)
            rt_factor = chunk_seconds / avg
            fits = "YES" if avg < window_budget else "NO - will lag"
            print(f"{model_name:<12} {beam:<6} {avg:<12.2f} {rt_factor:<16.2f} {fits}")


def bench_full_pipeline(chunk, chunk_seconds):
    """Measure ALL pipeline stages for a single chunk to get real per-tile cost."""
    import sys
    import os
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

    from transcribe import Segment
    import config
    import live_transcribe
    import bible_db
    import intent_router
    import buffer as buffer_mod
    import quote_detect
    from retrieve import Retriever
    import rerank as rerank_mod
    import score as score_mod
    from context import SermonContext

    # Load resources once
    print("\nLoading resources (one-time)...")
    db = bible_db.get_bible_db()
    import models
    embedder = models.get_embedder(offline=True)
    retriever = Retriever.from_disk(
        embedder,
        index_path=os.path.join(config.INDEX_DIR, "verses.faiss"),
        meta_path=os.path.join(config.INDEX_DIR, "verses_meta.json"),
    )
    detector = quote_detect.get_detector(
        "onnx",
        model_path=os.path.join(config.HERE, "onnx_model", "model.onnx"),
        tokenizer_path=os.path.join(config.HERE, "onnx_model"),
    )
    ctx = SermonContext()
    session = intent_router.SessionState()
    print("Resources loaded.\n")

    # Warm up whisper model once
    model = live_transcribe._load_whisper(config.WHISPER_MODEL, config.OFFLINE)

    runs = 5
    timings = {"whisper": [], "buffer": [], "intent": [], "onnx": [],
               "retrieve": [], "rerank": [], "score": []}
    total_times = []

    for run in range(runs):
        t_total = time.time()

        # --- 1. Whisper transcription ---
        t0 = time.time()
        segs, info = model.transcribe(
            chunk, language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE,
            vad_filter=config.WHISPER_VAD_FILTER,
            condition_on_previous_text=False,
        )
        segs = list(segs)
        t_whisper = time.time() - t0
        timings["whisper"].append(t_whisper)

        segs_objs = [Segment(s.text.strip(), float(s.start), float(s.end)) for s in segs if s.text.strip()]

        # --- 2. Buffer ---
        t0 = time.time()
        windows = buffer_mod.buffer_segments(segs_objs)
        t_buf = time.time() - t0
        timings["buffer"].append(t_buf)

        for w in windows:
            text = w.candidate_text or ""
            if len(text.split()) < 4:
                continue

            # --- 3. Intent router ---
            t0 = time.time()
            result = intent_router.route(text, session, db)
            t_intent = time.time() - t0
            timings["intent"].append(t_intent)

            if result.intent != intent_router.CUE_PHRASE:
                continue

            # --- 4. ONNX classifier ---
            t0 = time.time()
            qp = detector.score(text)
            t_onnx = time.time() - t0
            timings["onnx"].append(t_onnx)

            if qp < config.QUOTE_THRESHOLD:
                continue

            # --- 5. FAISS retrieve ---
            t0 = time.time()
            candidates = retriever.search_one(text, top_k=config.TOP_K)
            t_faiss = time.time() - t0
            timings["retrieve"].append(t_faiss)

            if not candidates:
                continue

            # --- 6. Rerank ---
            t0 = time.time()
            ranked = rerank_mod.rerank_candidates(
                candidates, text, qp, ctx, set(), {},
                weights=config.RERANK_WEIGHTS, cross_encoder=None,
            )
            selected, _ = rerank_mod.select_representative(ranked)
            t_rerank = time.time() - t0
            timings["rerank"].append(t_rerank)

            if selected is None:
                continue

            # --- 7. Score ---
            t0 = time.time()
            conf = score_mod.confidence_for(selected)
            band = score_mod.band(conf)
            t_score = time.time() - t0
            timings["score"].append(t_score)

        total_times.append(time.time() - t_total)

    # Report averages (only for stages that ran)
    print(f"Full pipeline per-chunk (avg of {runs} runs, {chunk_seconds:.1f}s tile):")
    print(f"{'stage':<20} {'avg_ms':>8} {'pct_of_total':>12}")
    print("-" * 48)
    total_avg = sum(total_times) / len(total_times)
    for stage in ["whisper", "buffer", "intent", "onnx", "retrieve", "rerank", "score"]:
        vals = timings[stage]
        if vals:
            avg_ms = (sum(vals) / len(vals)) * 1000
            pct = (sum(vals) / sum(total_times)) * 100
            print(f"{stage:<20} {avg_ms:>7.1f}ms {pct:>10.1f}%")
    print("-" * 48)
    print(f"{'TOTAL PER CHUNK':<20} {(total_avg*1000):>7.1f}ms")
    margin = chunk_seconds - total_avg
    print(f"\nPer-chunk budget: {chunk_seconds:.1f}s | consumed: {total_avg:.2f}s | "
          f"margin: {margin:+.2f}s {'(OK)' if margin > 0 else '(LAGGING!)'}")
    if margin < 1.0:
        print("WARNING: margin < 1s — transcribe may fall behind on slow CPU spikes.")
    return total_avg, margin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True, help="Audio file to slice test chunk from")
    ap.add_argument("--chunk-seconds", type=float, default=6.5,
                    help="Tile duration (default 6.5 = 5s new-audio + 1.5s overlap)")
    ap.add_argument("--models", default="small.en")
    ap.add_argument("--beam-sizes", default="1")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--window-budget", type=float, default=5.0,
                    help="Real-time budget for live (default 5s = new-audio interval)")
    ap.add_argument("--full-pipeline", action="store_true",
                    help="Also measure full pipeline (not just Whisper alone)")
    args = ap.parse_args()

    chunk = load_audio_chunk(args.audio, args.chunk_seconds)
    print(f"Loaded {len(chunk)/16000:.1f}s test chunk from {args.audio}")
    print(f"Budget for processing: {args.window_budget:.1f}s (new-audio interval)")

    models = args.models.split(",")
    beam_sizes = [int(b) for b in args.beam_sizes.split(",")]

    bench_whisper_only(chunk, args.chunk_seconds, models, beam_sizes,
                       args.runs, args.window_budget)

    if args.full_pipeline:
        bench_full_pipeline(chunk, args.chunk_seconds)


if __name__ == "__main__":
    main()