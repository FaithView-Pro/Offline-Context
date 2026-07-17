"""Stages 2-6 plumbing + output-format validation. No ML deps required.

Uses a deterministic stub embedder (hashed bag-of-words -> 384-dim) so the
FAISS build/search/rerank/score path is exercised end-to-end on a real corpus
subset without downloading bge-small. Run: python test_pipeline.py
"""
from __future__ import annotations
import hashlib, os, tempfile, json
import numpy as np
import faiss

import config
from corpus import load_corpus
from transcribe import segments_from_dicts
from buffer import buffer_segments
from quote_detect import get_detector, filter_quote_windows
from retrieve import build_index, Retriever, Candidate
from context import SermonContext
from rerank import rerank_candidates, select_representative
from score import build_entry, is_surfaced


class StubEmbedder:
    """Deterministic hashed bag-of-words embedder with the same interface as models.Embedder."""
    model_name = "stub-bow"
    dim = config.EMBED_DIM

    def _vec(self, texts, prefix=""):
        import re
        mat = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z']+", (prefix + " " + (t or "")).lower()):
                if len(w) <= 2:
                    continue
                h = int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim
                mat[i, h] += 1.0
            n = np.linalg.norm(mat[i])
            if n > 0:
                mat[i] /= n
        return np.ascontiguousarray(mat, dtype="float32")

    def encode_corpus(self, texts, batch_size=64, show_progress=False):
        return self._vec(texts)

    def encode_queries(self, texts, batch_size=32):
        return self._vec(texts, prefix=config.QUERY_PREFIX)


def test_corpus():
    verses = load_corpus(config.AMP_PATH, config.NKJV_PATH)
    assert len(verses) > 60000, f"expected ~62k verses, got {len(verses)}"
    amps = [v for v in verses if v.translation == "AMP"]
    nkjvs = [v for v in verses if v.translation == "NKJV"]
    assert amps and nkjvs, "both translations must be present"
    ez = [v for v in verses if v.book == "Ezekiel" and v.chapter == 36 and v.verse == 27]
    assert len(ez) == 2, f"Ezekiel 36:27 should appear in both translations, got {len(ez)}"
    print(f"[corpus] OK: {len(verses)} verses (AMP={len(amps)}, NKJV={len(nkjvs)}); "
          f"Ezekiel 36:27 -> {[v.reference+' '+v.translation for v in ez]}")
    return verses


def test_buffer_and_quote():
    segs = segments_from_dicts([
        {"text": "Let me tell you a funny story about my neighbor last week.", "start_time": 0.0, "end_time": 6.0},
        {"text": "Now the Bible says, I will put my Spirit within you and cause you to walk in my statutes.", "start_time": 8.0, "end_time": 17.0},
        {"text": "And you will keep my judgments and do them.", "start_time": 17.0, "end_time": 21.0},
        {"text": "Breaking news, the markets rallied today after the report.", "start_time": 23.0, "end_time": 28.0},
    ])
    windows = buffer_segments(segs)
    det = get_detector("heuristic")
    passed, all_scored = filter_quote_windows(windows, det, config.QUOTE_THRESHOLD)
    print(f"[buffer/quote] windows={len(windows)} passed={len(passed)}")
    for w, p in all_scored:
        flag = "PASS" if p >= config.QUOTE_THRESHOLD else "    "
        print(f"   {flag} {p:.2f}  {w.candidate_text[:60]}")
    assert any("Spirit within you" in w.candidate_text or "Spirit within you" in w.text for w, _ in passed), \
        "the Ezekiel quote window must pass quote detection"
    assert not any("Breaking news" in w.candidate_text for w, _ in passed), "news must not pass"
    return windows, passed


def test_retrieval_rerank_score(verses, passed):
    # Build a stub index on a subset that includes Ezekiel 36 + Jeremiah 31 + some distractors.
    subset = [v for v in verses if v.book in ("Ezekiel", "Jeremiah", "John", "Romans", "Psalms")]
    assert any(v.book == "Ezekiel" and v.chapter == 36 and v.verse == 27 for v in subset)
    tmp = tempfile.mkdtemp(prefix="fvptest_")
    ip = os.path.join(tmp, "verses.faiss")
    mp = os.path.join(tmp, "verses_meta.json")
    emb = StubEmbedder()
    index, meta = build_index(subset, emb, index_path=ip, meta_path=mp, show_progress=False)
    print(f"[retrieve] built stub index: {index.ntotal} vectors over {len(subset)} verses")

    retr = Retriever.from_disk(emb, index_path=ip, meta_path=mp)
    # Take the Ezekiel quote window as the query.
    qwin, qprob = [wp for wp in passed if "Spirit within you" in wp[0].text][0]
    cands = retr.search_one(qwin.text, top_k=8)
    print(f"[retrieve] top-8 for quote window:")
    for c in cands:
        print(f"    {c.score:.3f}  {c.reference:<18} {c.translation}")

    ctx = SermonContext()
    ranked = rerank_candidates(cands, qwin.text, qprob, ctx, set(), {}, weights=config.RERANK_WEIGHTS)
    sel, _ = select_representative(ranked)
    entry = build_entry(qwin, qprob, sel, ranked)
    print(f"[rerank/score] selected={entry['selected_reference']} {entry['translation']} "
          f"conf={entry['confidence']} band={entry['confidence_band']}")
    print("[rerank/score] ranked components (sem/lex/ctx/hist/qp):")
    for r in ranked[:5]:
        print(f"    {r.final:.3f}  {r.candidate.reference:<18} {r.candidate.translation}  "
              f"sem={r.semantic:.2f} lex={r.lexical:.2f} ctx={r.context:.2f} hist={r.history:.2f} qp={r.quote_prob:.2f}")
    print(f"[output] entry keys: {sorted(entry.keys())}")

    # Format assertions (match the spec exactly)
    expected_keys = {"start_time", "end_time", "transcript", "quote_probability",
                     "selected_reference", "translation", "confidence", "confidence_band", "candidates"}
    assert set(entry.keys()) == expected_keys, f"entry keys mismatch: {set(entry.keys()) ^ expected_keys}"
    assert all(set(c.keys()) == {"reference", "translation", "score"} for c in entry["candidates"])
    # With the (non-semantic) stub embedder we only assert presence + format;
    # strict "Ezekiel 36:27 is selected" is validated separately with the real bge model.
    refs = [c["reference"] for c in entry["candidates"]]
    assert "Ezekiel 36:27" in refs, f"Ezekiel 36:27 must be among candidates: {refs}"
    assert entry["translation"] in ("AMP", "NKJV")
    assert entry["confidence_band"] in ("autopilot-eligible", "review queue", "ignored")
    print("[rerank/score] all format + presence assertions PASSED (stub embedder)")
    return entry


def main():
    print("== corpus ==")
    verses = test_corpus()
    print("\n== buffer + quote detection ==")
    windows, passed = test_buffer_and_quote()
    print("\n== retrieval + rerank + score (stub FAISS) ==")
    entry = test_retrieval_rerank_score(verses, passed)
    print("\nALL PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
