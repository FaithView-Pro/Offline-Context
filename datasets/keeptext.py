import pandas as pd

df = pd.read_csv("preacher_tts_dataset.csv")

# Keep only the text column
df = df[["text"]]

df.to_csv("preacher_text_only.csv", index=False)
