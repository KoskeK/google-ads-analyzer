import json

with open("results.json", "r") as f:
    original_data = json.load(f)

with open("good_data.json", "r") as f:
    new_data = json.load(f)

print(f"Original data has {len(original_data)} entries.")
print(f"New data has {len(new_data)} entries.")
print(f"The difference is {len(new_data) - len(original_data)} entries.")
print(f"That is a {((len(new_data) - len(original_data)) / len(original_data)) * 100:.2f}% change.")