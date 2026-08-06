#!/usr/bin/env python3
"""
Fine-tune a small transformer classifier on the labeled quote-detection
dataset, then export to ONNX for the OnnxQuoteDetector slot in
quote_detect.py.

One-time setup (network ON, run once to cache the base model):
    pip install transformers torch datasets scikit-learn optimum[onnxruntime] --break-system-packages

Usage:
    python train_quote_classifier.py --train train.csv --val val.csv
    python train_quote_classifier.py --train train.csv --val val.csv --model distilbert-base-uncased --epochs 4
"""
import argparse
import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_fscore_support, accuracy_score
from sklearn.utils.class_weight import compute_class_weight
import torch
from torch.utils.data import Dataset
from transformers import (
    AutoTokenizer, AutoModelForSequenceClassification,
    TrainingArguments, Trainer,
)


LABEL2ID = {"negative": 0, "positive": 1}
ID2LABEL = {0: "negative", 1: "positive"}


class TextDataset(Dataset):
    def __init__(self, texts, labels, tokenizer, max_len=64):
        self.encodings = tokenizer(
            list(texts), truncation=True, padding=True, max_length=max_len
        )
        self.labels = [LABEL2ID[l] for l in labels]

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        item = {k: torch.tensor(v[idx]) for k, v in self.encodings.items()}
        item["labels"] = torch.tensor(self.labels[idx])
        return item


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=1)
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, preds, average="binary", zero_division=0
    )
    acc = accuracy_score(labels, preds)
    return {"accuracy": acc, "precision": precision, "recall": recall, "f1": f1}


class WeightedTrainer(Trainer):
    """Applies class weights so the ~90/10 imbalance doesn't get ignored."""
    def __init__(self, *args, class_weights=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.class_weights = class_weights

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        labels = inputs.pop("labels")
        outputs = model(**inputs)
        logits = outputs.logits
        loss_fct = torch.nn.CrossEntropyLoss(weight=self.class_weights.to(logits.device))
        loss = loss_fct(logits, labels)
        return (loss, outputs) if return_outputs else loss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--model", default="distilbert-base-uncased")
    ap.add_argument("--out-dir", default="quote_classifier_model")
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    args = ap.parse_args()

    train_df = pd.read_csv(args.train)
    val_df = pd.read_csv(args.val)

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model, num_labels=2, id2label=ID2LABEL, label2id=LABEL2ID
    )

    train_ds = TextDataset(train_df["text"], train_df["label"], tokenizer)
    val_ds = TextDataset(val_df["text"], val_df["label"], tokenizer)

    # class weights from the TRAIN split's actual imbalance
    weights = compute_class_weight(
        class_weight="balanced",
        classes=np.array([0, 1]),
        y=[LABEL2ID[l] for l in train_df["label"]],
    )
    class_weights = torch.tensor(weights, dtype=torch.float)
    print(f"Class weights (negative, positive): {weights}")

    training_args = TrainingArguments(
        output_dir=args.out_dir + "_checkpoints",
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        learning_rate=args.lr,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        logging_steps=20,
        report_to="none",
    )

    trainer = WeightedTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        compute_metrics=compute_metrics,
        class_weights=class_weights,
    )

    trainer.train()

    print("\n=== Final validation metrics ===")
    print(trainer.evaluate())

    trainer.save_model(args.out_dir)
    tokenizer.save_pretrained(args.out_dir)
    print(f"\nModel saved to {args.out_dir}/")
    print("Next: run export_to_onnx.py to produce the .onnx file for OnnxQuoteDetector")


if __name__ == "__main__":
    main()
