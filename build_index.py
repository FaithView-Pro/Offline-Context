"""One-time setup: build the FAISS index from the two translations.

    python build_index.py                 # uses amplified.json + nkjv.json in cwd
    python build_index.py --online        # allow first-time model download
    python build_index.py --limit 2000    # quick smoke test on a subset
    python build_index.py --batch-size 96

Generates one embedding per verse per translation with BAAI/bge-small-en-v1.5
and writes:

    index/verses.faiss   (IndexFlatIP, ~62k x 384)
    index/verses_meta.json (one row per vector: translation, book, chapter, verse, text)

Run this once before `python run.py`. After the first download, re-run with the
default offline mode (no --online) to guarantee zero network calls.
"""

from __future__ import annotations

import argparse
import os
import time

import config
from corpus import load_corpus
from retrieve import build_index


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the FAISS verse index (offline setup).")
    ap.add_argument("--amp", default=config.AMP_PATH)
    ap.add_argument("--nkjv", default=config.NKJV_PATH)
    ap.add_argument("--index-dir", default=config.INDEX_DIR)
    ap.add_argument("--model", default=config.EMBED_MODEL)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0, help="embed only the first N verses (smoke test)")
    ap.add_argument("--online", action="store_true", help="allow model download (first run only)")
    args = ap.parse_args()

    offline = not args.online
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    verses = load_corpus(args.amp, args.nkjv)
    print(f"[build] loaded {len(verses)} verses from {args.amp} + {args.nkjv}")
    if args.limit:
        verses = verses[: args.limit]
        print(f"[build] --limit: using first {len(verses)} verses")

    import models
    embedder = models.get_embedder(offline=offline)
    if embedder.model_name != args.model:
        print(f"[build] note: requested {args.model} but singleton uses {embedder.model_name}")

    index_path = os.path.join(args.index_dir, "verses.faiss")
    meta_path = os.path.join(args.index_dir, "verses_meta.json")

    t0 = time.time()
    build_index(verses, embedder, index_path=index_path, meta_path=meta_path,
                batch_size=args.batch_size, show_progress=True)
    dt = time.time() - t0
    print(f"[build] done in {dt:.1f}s  ({len(verses)/dt:.0f} verses/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
