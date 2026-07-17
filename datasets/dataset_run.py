from datasets import load_dataset

ds = load_dataset("odunola/sermon-pair")
print(ds)
print(ds["train"][0])
