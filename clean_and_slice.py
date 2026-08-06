#!/usr/bin/env python3
"""
Slice the first N rows (default 3000) of the labeling spreadsheet and
enforce strict label/subtype consistency:

  - label == negative  ->  subtype is FORCED to "negative", no exceptions
  - label == positive  ->  subtype must be "cue_phrase" or "explicit_reference"
                            (if it's anything else - blank, "negative",
                            "verbatim_no_cue", a typo, etc. - it's reassigned:
                            "explicit_reference" if the text contains a
                            book/chapter:verse pattern, else "cue_phrase")
  - any row where label itself isn't exactly "positive" or "negative"
    (blank, typo, NaN) is dropped and reported, since it can't be trained on

Usage:
    python clean_and_slice.py --in raw_labeling_dataset.xlsx --out clean_0_3000.csv
    python clean_and_slice.py --in raw_labeling_dataset.xlsx --out clean_0_3000.csv --start 0 --end 3000
"""
import argparse
import re
import pandas as pd

REFERENCE_PATTERN = re.compile(
    r"\b(?:[1-3]\s?)?[A-Z][a-z]+\s\d{1,3}[:.]\d{1,3}\b|\b\d{1,3}[:.]\d{1,3}\b"
)

# Map a distinctive substring of each real source_file name -> anonymized label.
# Matching is done by substring (case-insensitive) rather than exact equality,
# since sanitized filenames can vary slightly (spaces/brackets stripped etc.).
ANONYMIZE_MAP = {
    "a-cure-for-heart-trouble-billy-graham": "person 1",
    "april-miracle-service": "person 2",
    "bishop_isaiah_the_holy_spirit": "person 3",
}


def anonymize_source_file(series: pd.Series) -> pd.Series:
    lowered = series.astype(str).str.lower()
    out = series.astype(str).copy()
    matched_mask = pd.Series(False, index=series.index)

    for substring, label in ANONYMIZE_MAP.items():
        hit = lowered.str.contains(substring, regex=False)
        out.loc[hit] = label
        matched_mask |= hit

    unmatched = series[~matched_mask].unique()
    if len(unmatched):
        print(f"\nWARNING: {len(unmatched)} distinct source_file value(s) did NOT match any "
              f"anonymization mapping and were left UNCHANGED (still contain real names):")
        for u in unmatched:
            print(f"  - {u}")
        print("Add a mapping for these in ANONYMIZE_MAP if they should be anonymized too.\n")

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", dest="outfile", default="clean_0_3000.csv")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=3000)
    args = ap.parse_args()

    if args.infile.endswith(".xlsx"):
        df = pd.read_excel(args.infile)
    else:
        df = pd.read_csv(args.infile)

    df = df.iloc[args.start:args.end].copy()
    print(f"Sliced rows {args.start}:{args.end} -> {len(df)} rows")

    # normalize label text (strip whitespace/case) before validating
    df["label"] = df["label"].astype(str).str.strip().str.lower()

    valid_mask = df["label"].isin(["positive", "negative"])
    dropped = df[~valid_mask]
    if len(dropped):
        print(f"Dropping {len(dropped)} rows with invalid/blank label: ids {dropped['id'].tolist()}")
    df = df[valid_mask].copy()

    # --- negative rows: subtype forced to "negative", always ---
    neg_mask = df["label"] == "negative"
    changed_neg = df.loc[neg_mask, "subtype"].astype(str).str.strip().str.lower() != "negative"
    print(f"Forcing subtype='negative' on {changed_neg.sum()} negative rows that had a different subtype")
    df.loc[neg_mask, "subtype"] = "negative"

    # --- positive rows: subtype must be cue_phrase or explicit_reference ---
    pos_mask = df["label"] == "positive"
    valid_pos_subtypes = {"cue_phrase", "explicit_reference"}
    current_subtype = df.loc[pos_mask, "subtype"].astype(str).str.strip().str.lower()
    needs_fix = ~current_subtype.isin(valid_pos_subtypes)

    fix_idx = df.loc[pos_mask].loc[needs_fix].index
    if len(fix_idx):
        print(f"Reassigning subtype on {len(fix_idx)} positive rows that weren't cue_phrase/explicit_reference")
        for idx in fix_idx:
            text = str(df.at[idx, "text"])
            df.at[idx, "subtype"] = "explicit_reference" if REFERENCE_PATTERN.search(text) else "cue_phrase"

    # keep valid ones as-is (already cue_phrase or explicit_reference)
    df.loc[pos_mask & ~needs_fix, "subtype"] = current_subtype[~needs_fix]

    # --- final sanity check ---
    bad_neg = ((df["label"] == "negative") & (df["subtype"] != "negative")).sum()
    bad_pos = ((df["label"] == "positive") & (~df["subtype"].isin(valid_pos_subtypes))).sum()
    assert bad_neg == 0, f"{bad_neg} negative rows still have wrong subtype"
    assert bad_pos == 0, f"{bad_pos} positive rows still have wrong subtype"

    df["source_file"] = anonymize_source_file(df["source_file"])

    df.to_csv(args.outfile, index=False)
    print(f"\nWrote {len(df)} rows to {args.outfile}")
    print(df["label"].value_counts())
    print(df[df.label == "positive"]["subtype"].value_counts())


if __name__ == "__main__":
    main()
