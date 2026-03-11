import json

goodData = {}
print("File loading...")
with open("results.json", "r") as f:
    data = json.load(f)
    print("File loaded, processing...")
    counter = len(data)
    for entry in data:
        if entry["has_ads"]:
            goodData[entry["url"]] = entry
        print(f"Processing {counter} out of {len(data)}")
        counter -= 1

with open("good_data.json", "w") as f:
    json.dump(goodData, f, indent=2)