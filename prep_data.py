#!/usr/bin/env python3
"""
Prep the labeled dataset for training: dedupe, stratified split.

Usage:
    python prep_data.py --in clean_0_3000_dataset_ready.csv
"""
import argparse
import pandas as pd
from sklearn.model_selection import train_test_split

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--test-size", type=float, default=0.10,
                     help="Smaller than usual (0.10) since positives are scarce")
    args = ap.parse_args()

    df = pd.read_csv(args.infile)
    n_before = len(df)
    df = df.drop_duplicates(subset="text")
    df = df.dropna(subset=["label", "text"])
    print(f"{n_before - len(df)} duplicate/invalid rows removed -> {len(df)} rows")

    train_df, val_df = train_test_split(
        df, test_size=args.test_size, stratify=df["label"], random_state=42
    )

    train_df.to_csv("train.csv", index=False)
    val_df.to_csv("val.csv", index=False)

    print(f"\nTrain: {len(train_df)} rows | positives: {(train_df.label=='positive').sum()}")
    print(f"Val:   {len(val_df)} rows | positives: {(val_df.label=='positive').sum()}")

if __name__ == "__main__":
    main()
