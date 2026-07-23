



from datasets import load_dataset

# Download the dataset
ds = load_dataset("Bateesa/preacher-tts-dataset")

# Convert the train split to a pandas DataFrame
df = ds["train"].to_pandas()

# Save as CSV
df.to_csv("preacher_tts_dataset.csv", index=False)

print(df.head())
