"""One-time model download (network ON). Run this once on a machine with
internet, then run everything else in default offline mode.

    python download_models.py                 # embedder + whisper (small)
    python download_models.py --cross-encoder # also fetch the optional cross-encoder
    python download_models.py --whisper-model small.en

Models cache into the Hugging Face cache (~/.cache/huggingface). After this,
set FAITHVIEW_OFFLINE=1 (the default) and the pipeline never touches the network.
"""

from __future__ import annotations

import argparse
import os

import config


def download_embedder(model_name: str) -> None:
    print(f"[download] sentence-transformers: {model_name}")
    from sentence_transformers import SentenceTransformer
    SentenceTransformer(model_name)  # downloads + caches
    print("[download] embedder cached.")


def download_whisper(model_name: str) -> None:
    print(f"[download] faster-whisper: {model_name}")
    from faster_whisper import WhisperModel
    WhisperModel(model_name, device="cpu", compute_type="int8")  # downloads + caches
    print("[download] whisper cached.")


def download_cross_encoder(model_name: str) -> None:
    print(f"[download] cross-encoder: {model_name}")
    from sentence_transformers import CrossEncoder
    CrossEncoder(model_name)
    print("[download] cross-encoder cached.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Download all models once (network ON).")
    ap.add_argument("--embed-model", default=config.EMBED_MODEL)
    ap.add_argument("--whisper-model", default=config.WHISPER_MODEL)
    ap.add_argument("--cross-encoder", action="store_true")
    ap.add_argument("--cross-encoder-model", default=config.CROSS_ENCODER_MODEL)
    ap.add_argument("--skip-whisper", action="store_true")
    ap.add_argument("--skip-embedder", action="store_true")
    args = ap.parse_args()

    # Make sure we are NOT in offline mode for this script.
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ.pop(k, None)

    if not args.skip_embedder:
        download_embedder(args.embed_model)
    if not args.skip_whisper:
        download_whisper(args.whisper_model)
    if args.cross_encoder:
        download_cross_encoder(args.cross_encoder_model)

    print("\n[download] all requested models cached. You can now run fully offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
