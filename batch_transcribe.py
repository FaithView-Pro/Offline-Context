#!/usr/bin/env python3
"""
Batch-transcribe every audio file in a directory using the existing
transcribe.py module, writing one timestamped JSON transcript per file.

Designed to run unattended overnight:
  - Resumable: skips files that already have an output JSON (use --force to redo)
  - Isolated failures: one bad file logs an error and the batch continues
  - Progress + ETA logging to both stdout and a log file
  - Offline-safe: sets HF offline env vars before importing anything model-related

Usage:
    python batch_transcribe.py
    python batch_transcribe.py --audio-dir audios --out-dir transcripts
    python batch_transcribe.py --force            # re-transcribe everything
    python batch_transcribe.py --word-timestamps   # default: on (recommended)
"""

import os

# Must happen BEFORE importing transcribe/models, so no accidental network calls
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("FAITHVIEW_OFFLINE", "1")

import argparse
import dataclasses
import json
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# --- import your existing pipeline modules ---
try:
    from transcribe import transcribe
except ImportError as e:
    print(f"ERROR: could not import transcribe() from transcribe.py: {e}")
    sys.exit(1)

try:
    from config import Settings
except ImportError:
    Settings = None  # fall back to no-settings call if your transcribe() doesn't need one

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg"}


def log(msg: str, log_file):
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    log_file.write(line + "\n")
    log_file.flush()


def serialize_segments(segments):
    """Convert list[Segment] (dataclasses, possibly nested with Word dataclasses)
    into plain JSON-serializable dicts, without assuming exact field names."""
    out = []
    for seg in segments:
        if dataclasses.is_dataclass(seg):
            out.append(dataclasses.asdict(seg))
        elif hasattr(seg, "__dict__"):
            out.append(dict(seg.__dict__))
        else:
            # already a plain dict/tuple - best effort
            out.append(seg)
    return out


def find_audio_files(audio_dir: Path):
    files = [p for p in sorted(audio_dir.iterdir()) if p.suffix.lower() in AUDIO_EXTS]
    return files


def output_path(out_dir: Path, audio_path: Path) -> Path:
    stem = audio_path.stem
    # sanitize brackets/parens/spaces a bit for a cleaner filename, but keep it unique
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    return out_dir / f"{safe}.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio-dir", default="audios", help="Directory of input audio files")
    ap.add_argument("--out-dir", default="transcripts", help="Directory to write transcript JSONs")
    ap.add_argument("--force", action="store_true", help="Re-transcribe even if output already exists")
    ap.add_argument("--word-timestamps", action="store_true", default=True,
                     help="Request word-level timestamps (recommended for buffer.py)")
    ap.add_argument("--model", default=None, help="Override WHISPER_MODEL from config (e.g. small, small.en)")
    args = ap.parse_args()

    audio_dir = Path(args.audio_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "batch_transcribe.log"
    log_file = open(log_path, "a")

    files = find_audio_files(audio_dir)
    if not files:
        log(f"No audio files found in {audio_dir.resolve()}", log_file)
        return

    settings = None
    if Settings is not None:
        try:
            settings = Settings()
            if args.model:
                settings.WHISPER_MODEL = args.model
        except Exception:
            settings = None

    log(f"Found {len(files)} audio files in {audio_dir.resolve()}", log_file)
    log(f"Writing transcripts to {out_dir.resolve()}", log_file)
    log(f"Word timestamps: {args.word_timestamps} | Force re-run: {args.force}", log_file)
    log("-" * 70, log_file)

    durations = []  # (seconds elapsed) for ETA estimation
    todo = []
    for f in files:
        outp = output_path(out_dir, f)
        if outp.exists() and not args.force:
            log(f"SKIP (already done): {f.name}", log_file)
        else:
            todo.append(f)

    total = len(todo)
    log(f"{total} file(s) queued for transcription.", log_file)
    log("-" * 70, log_file)

    for i, audio_path in enumerate(todo, start=1):
        outp = output_path(out_dir, audio_path)
        log(f"[{i}/{total}] START: {audio_path.name}", log_file)
        t0 = time.time()
        try:
            if settings is not None:
                segments = transcribe(str(audio_path), settings, word_timestamps=args.word_timestamps)
            else:
                segments = transcribe(str(audio_path), word_timestamps=args.word_timestamps)

            payload = {
                "source_file": audio_path.name,
                "transcribed_at": datetime.now().isoformat(),
                "word_timestamps": args.word_timestamps,
                "segments": serialize_segments(segments),
            }

            with open(outp, "w") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            elapsed = time.time() - t0
            durations.append(elapsed)
            avg = sum(durations) / len(durations)
            remaining = total - i
            eta = timedelta(seconds=int(avg * remaining))
            log(f"[{i}/{total}] DONE in {elapsed:.1f}s -> {outp.name} "
                f"(avg {avg:.1f}s/file, ETA for remaining {remaining} files: {eta})", log_file)

        except Exception as e:
            elapsed = time.time() - t0
            log(f"[{i}/{total}] ERROR after {elapsed:.1f}s on {audio_path.name}: {e}", log_file)
            log(traceback.format_exc(), log_file)
            log(f"[{i}/{total}] Continuing to next file...", log_file)
            continue

    log("-" * 70, log_file)
    log(f"Batch complete. {len(durations)}/{total} succeeded.", log_file)
    log_file.close()


if __name__ == "__main__":
    main()
