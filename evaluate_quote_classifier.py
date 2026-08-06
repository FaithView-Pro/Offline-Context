import torch
import pandas as pd
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from sklearn.metrics import classification_report, confusion_matrix

MODEL_DIR = "quote_classifier_model"
VAL_FILE = "val.csv"

tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)

model.eval()

df = pd.read_csv(VAL_FILE)

label2id = model.config.label2id

texts = df["text"].tolist()
labels = [label2id[str(l)] for l in df["label"]]

predictions = []

for text in texts:
    inputs = tokenizer(
        text,
        truncation=True,
        padding=True,
        max_length=256,
        return_tensors="pt"
    )

    with torch.no_grad():
        outputs = model(**inputs)

    pred = torch.argmax(outputs.logits, dim=1).item()
    predictions.append(pred)


print("\n=== RESULTS ===")

print(
    classification_report(
        labels,
        predictions,
        target_names=["negative", "positive"]
    )
)

print("\nConfusion Matrix:")
print(confusion_matrix(labels, predictions))
