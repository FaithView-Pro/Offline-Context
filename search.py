"""Text-to-verse search CLI. Type a quote or phrase, get the nearest verses.

    python search.py "I will put my spirit within you"
    python search.py "the Lord is my shepherd I shall not want" --top-k 5
    python search.py "love your enemies" --translation NKJV
    python search.py                       # interactive mode
"""

from __future__ import annotations

import argparse
import os

import config
from models import get_embedder
from retrieve import Retriever
from rerank import rerank_candidates
from context import SermonContext


def search(query: str, retriever: Retriever, top_k: int = 10,
           translation: str | None = None, rerank: bool = True) -> list:
    """Search the corpus and return ranked candidates."""
    cands = retriever.search_one(query, top_k=top_k * 2 if translation else top_k)

    if translation:
        cands = [c for c in cands if c.translation == translation.upper()][:top_k]

    if rerank and cands:
        ctx = SermonContext()
        ranked = rerank_candidates(cands, query, 0.90, ctx, set(), {})
        return ranked[:top_k]

    return cands[:top_k]


def print_results(query: str, results) -> None:
    print(f"\n  Query: \"{query}\"")
    print(f"  {'#':>2}  {'score':>6}  {'reference':<22} {'trans':<6}  verse")
    print("  " + "-" * 100)
    for i, r in enumerate(results, 1):
        if hasattr(r, 'candidate'):
            score = r.final
            ref = r.candidate.reference
            trans = r.candidate.translation
            text = r.candidate.text
        else:
            score = r.score
            ref = r.reference
            trans = r.translation
            text = r.text
        print(f"  {i:>2}  {score:.4f}  {ref:<22} {trans:<6}  {text[:75]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Search the Bible corpus for nearest verses.")
    ap.add_argument("query", nargs="?", default=None, help="text to search for")
    ap.add_argument("--top-k", type=int, default=10, help="number of results (default 10)")
    ap.add_argument("--translation", default=None, choices=["AMP", "NKJV"],
                    help="filter to one translation")
    ap.add_argument("--no-rerank", action="store_true", help="skip hybrid re-ranking (FAISS only)")
    args = ap.parse_args()

    offline = True
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    embedder = get_embedder(offline=offline)
    retriever = Retriever.from_disk(embedder)

    if args.query:
        results = search(args.query, retriever, top_k=args.top_k,
                         translation=args.translation, rerank=not args.no_rerank)
        print_results(args.query, results)
        return 0

    # Interactive mode
    print("FaithView Pro -- Verse Search")
    print("Type a phrase or quote and press Enter. Type 'quit' to exit.\n")
    while True:
        try:
            query = input("search> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            break
        results = search(query, retriever, top_k=args.top_k,
                         translation=args.translation, rerank=not args.no_rerank)
        print_results(query, results)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
