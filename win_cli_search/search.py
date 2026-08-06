#!/usr/bin/env python3
"""
FaithView Pro — offline Bible vector search CLI.

Embeds your query with the same BAAI/bge-small-en-v1.5 model that built
the FAISS index (62k verses across AMP + NKJV), then retrieves the top-K
verses by cosine similarity. Works fully offline once models are cached.

Usage:
    python search.py "I will put my spirit within you"
    python search.py "the Lord is my shepherd" --top-k 5
    python search.py                  (enter interactive mode)
    python search.py --interactive

First run downloads ~130 MB (bge-small-en-v1.5) from Hugging Face.
After that the tool stays 100% offline — no network needed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer
except ImportError as exc:
    print(f"\nMissing dependencies: {exc}")
    print("Run:  pip install sentence-transformers faiss-cpu numpy")
    print("Or:   setup.bat")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
INDEX_PATH = HERE / "index" / "verses.faiss"
META_PATH = HERE / "index" / "verses_meta.json"

MODEL_NAME = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
DEFAULT_TOP_K = 10


class BibleSearcher:
    def __init__(self):
        self._model = None
        self._index = None
        self._meta = None

    def load(self):
        if not INDEX_PATH.exists():
            sys.exit(f"Index not found: {INDEX_PATH}\n"
                     f"Copy verses.faiss from the main project into {INDEX_PATH.parent}")
        if not META_PATH.exists():
            sys.exit(f"Metadata not found: {META_PATH}\n"
                     f"Copy verses_meta.json from the main project into {META_PATH.parent}")

        print(f"Loading embedder ({MODEL_NAME})...", end=" ", flush=True)
        t0 = time.time()
        # offline-safe: sets HF_HUB_OFFLINE if the model is already cached
        if not os.environ.get("HF_HUB_OFFLINE"):
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        # Allow first-time download; subsequent runs use cache
        if "HF_HUB_OFFLINE" in os.environ:
            del os.environ["HF_HUB_OFFLINE"]
            del os.environ["TRANSFORMERS_OFFLINE"]

        self._model = SentenceTransformer(MODEL_NAME)
        print(f"({time.time() - t0:.1f}s)")

        print(f"Loading FAISS index...", end=" ", flush=True)
        t0 = time.time()
        self._index = faiss.read_index(str(INDEX_PATH))
        print(f"({self._index.ntotal} vectors, {time.time() - t0:.1f}s)")

        print(f"Loading metadata...", end=" ", flush=True)
        t0 = time.time()
        import json
        self._meta = json.loads(META_PATH.read_text("utf-8"))
        print(f"({len(self._meta)} entries, {time.time() - t0:.1f}s)")
        print()

    def search(self, query: str, top_k: int = DEFAULT_TOP_K):
        """Return top-k results as [(score, reference, translation, text), ...]."""
        print(f"Searching: \"{query}\"")
        t0 = time.time()

        # embed with bge query prefix
        qv = self._model.encode(
            [QUERY_PREFIX + query],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).astype("float32")

        D, I = self._index.search(qv, top_k)

        results = []
        for score, idx in zip(D[0], I[0]):
            if idx < 0:
                continue
            meta = self._meta[int(idx)]
            results.append((
                float(score),
                meta.get("reference") or f"{meta['book']} {meta['chapter']}:{meta['verse']}",
                meta.get("translation", "???"),
                meta.get("text", ""),
            ))

        elapsed = time.time() - t0
        print(f"  found {len(results)} results ({elapsed:.3f}s)")
        return results


def print_results(results, max_text=120):
    if not results:
        print("  (no results)")
        return
    print()
    print(f"{'#':>3}  {'score':>6}  {'reference':<22} {'trans':<5}  text")
    print(f"{'---':>3}  {'-----':>6}  {'----------------------':<22} {'-----':<5}  {'-' * min(60, max_text)}")
    for i, (score, ref, trans, text) in enumerate(results, 1):
        tail = "" if len(text) <= max_text else "…"
        print(f"{i:>3}  {score:6.4f}  {ref:<22} {trans:<5}  {text[:max_text]}{tail}")
    print()


def interactive(searcher: BibleSearcher):
    print("=" * 60)
    print("  FaithView Pro — Bible Vector Search")
    print("  Type a query and press Enter.  /quit to exit.")
    print("  /top N  to set result count (default 10).")
    print("=" * 60)
    print()
    top_k = DEFAULT_TOP_K
    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not raw:
            continue
        if raw.lower() in ("/quit", "/exit", "/q"):
            print("Bye.")
            break
        if raw.lower().startswith("/top"):
            parts = raw.split()
            if len(parts) >= 2 and parts[1].isdigit():
                top_k = int(parts[1])
                print(f"  results per query: {top_k}")
            continue
        results = searcher.search(raw, top_k=top_k)
        print_results(results)


def main():
    ap = argparse.ArgumentParser(
        description="FaithView Pro offline Bible vector search (AMP + NKJV).")
    ap.add_argument("query", nargs="*", help="Search query (wrap in quotes).")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                    help=f"Number of results (default {DEFAULT_TOP_K}).")
    ap.add_argument("--interactive", "-i", action="store_true",
                    help="Enter interactive REPL mode.")
    ap.add_argument("--txt", type=int, default=120,
                    help="Max characters of verse text to show (default 120).")
    args = ap.parse_args()

    q = " ".join(args.query).strip()

    if not q and not args.interactive:
        print("No query provided.  Use --interactive or pass a query string.")
        print("Example:  python search.py \"the Lord is my shepherd\"")
        sys.exit(1)

    s = BibleSearcher()
    try:
        s.load()
    except Exception as exc:
        sys.exit(f"\nError loading searcher: {exc}")

    if q and not args.interactive:
        results = s.search(q, top_k=args.top_k)
        print_results(results, max_text=args.txt)
        return

    if args.interactive:
        interactive(s)
        return

    # both query and interactive: run query then enter interactive
    results = s.search(q, top_k=args.top_k)
    print_results(results, max_text=args.txt)
    interactive(s)


if __name__ == "__main__":
    main()
