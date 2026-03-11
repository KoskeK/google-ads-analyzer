import json

goodData = {}

with open("results.json", "r") as f:
    data = json.load(f)
    for entry in data:
        if entry["has_ads"]:
            goodData[entry["url"]] = entry

with open("good_data.json", "w") as f:
    json.dump(goodData, f, indent=2)