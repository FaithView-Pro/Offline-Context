
#!/usr/bin/env python3
"""
Export the fine-tuned classifier to ONNX for OnnxQuoteDetector.
Uses torch.onnx.export directly, bypassing optimum's ORTModelForSequenceClassification
wrapper (avoids the torch.int4 version-compatibility bug).
"""
import argparse
from pathlib import Path
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out-dir", default="onnx_model")
    ap.add_argument("--max-len", type=int, default=64)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Loading model from {args.model_dir} ...")
    tokenizer = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(args.model_dir)
    model.eval()

    dummy = tokenizer(
        "example text for export tracing",
        return_tensors="pt",
        padding="max_length",
        max_length=args.max_len,
        truncation=True,
    )

    onnx_path = out / "model.onnx"
    print(f"Exporting to {onnx_path} ...")
    torch.onnx.export(
        model,
        (dummy["input_ids"], dummy["attention_mask"]),
        str(onnx_path),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "sequence"},
            "attention_mask": {0: "batch", 1: "sequence"},
            "logits": {0: "batch"},
        },
        opset_version=14,
    )

    tokenizer.save_pretrained(out)
    print(f"\nDone. ONNX model + tokenizer written to {out.resolve()}")
    print("Use model_path=<out_dir>/model.onnx, tokenizer_path=<out_dir> in get_detector('onnx', ...)")


if __name__ == "__main__":
    main()

