import json
import csv

with open("good_data.json", "r") as f:
    data = json.load(f)

with open("good_data.csv", "w", newline='', encoding='utf-8') as csvfile:
    fieldnames = ["url", "timestamp", "email", "name", "has_ads", "performance", "accessibility", "best-practices", "seo", "lcp"]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()
    for entry in data.values():
        writer.writerow({
            "url": entry.get("url"),
            "timestamp": entry.get("timestamp"),
            "email": entry.get("email"),
            "name": entry.get("name"),
            "has_ads": entry.get("has_ads"),
            "performance": entry.get("performance"),
            "accessibility": entry.get("accessibility"),
            "best-practices": entry.get("best_practices"),
            "seo": entry.get("seo"),
            "lcp": entry.get("lcp")
        })
    csvfile.flush()
    csvfile.close()