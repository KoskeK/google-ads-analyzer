import time
import httpx
import warnings
import csv
import tqdm
import concurrent.futures
from datetime import datetime
import json

# Suppress SSL warnings for small biz sites with expired certificates
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

with open('config.json', 'r') as f:
    config = json.load(f)

def detect_pixel(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }

    try:
        with httpx.Client(headers=headers, verify=False, follow_redirects=True, timeout=15.0) as client:
            response = client.get(url)
            # Use .lower() so we don't worry about casing
            html = response.text.lower()

            # 1. Check for Google Ads Footprints
            google_triggers = ['gtag', 'googleadservices', 'aw-', 'ads-wrapper', 'gclid']
            if any(x in html for x in google_triggers):
                return True
            else:
                return False

    except Exception:
        return False

with open("google_key", "r") as f:
    GOOGLE_API_KEY = f.read().strip()

def fetch_lighthouse_report(url, strategy="mobile", key=GOOGLE_API_KEY):
    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {
        "url": url,
        "key": key,
        "strategy": strategy,
        "category": ["performance", "accessibility", "best-practices", "seo"]
    }
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"Fetching Lighthouse report for {url} (Attempt {attempt + 1})...")
            response = httpx.get(endpoint, params=params, timeout=60)
            
            if response.status_code in [429, 500, 503]:
                print(f"API rate limit hit (Status {response.status_code}). Retrying in 10s...")
                time.sleep(10)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            categories = data['lighthouseResult']['categories']
            scores = {name: cat['score'] * 100 for name, cat in categories.items() if cat.get('score') is not None}
            lcp_audit = data['lighthouseResult']['audits'].get('largest-contentful-paint', {})
            lcp_value = lcp_audit.get('numericValue')
            if lcp_value is not None:
                scores['lcp'] = lcp_value / 1000
            
            return data, scores

        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            print(f"Request error: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return None, None

def read_csv(file_path):
    with open(file_path, mode='r', newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        return [row for row in reader]

def loadSBS(file_path):
    rawData = read_csv(file_path)
    data = []
    for row in rawData:
        data.append({
            "name": row["Contact person's name"],
            "email": row["Contact person's email"],
            "url": row["Website"] if row["Website"] else row["Additional website"]
        })
    return data

def rateAndSave(url, collection, email, name):
    data = {"url": url, "timestamp": datetime.today().isoformat(), "email": email, "name": name}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        pixel_future = executor.submit(detect_pixel, url)
        lighthouse_future = executor.submit(fetch_lighthouse_report, url)
        has_ads = pixel_future.result()
        lighthouse_data, scores = lighthouse_future.result()

    data['has_ads'] = has_ads

    if scores is not None:
        for score_name in scores:
            if scores[score_name] > config.get(f'max_{score_name}', scores[score_name]):
                for s in scores:
                    data[s] = scores[s]
                data["raw_data"] = lighthouse_data
                break

    collection.insert_one(data)

def rate(url, email, name):
    data = {"url": url, "timestamp": datetime.today().isoformat(), "email": email, "name": name}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        pixel_future = executor.submit(detect_pixel, url)
        lighthouse_future = executor.submit(fetch_lighthouse_report, url)
        has_ads = pixel_future.result()
        lighthouse_data, scores = lighthouse_future.result()

    data['has_ads'] = has_ads

    if has_ads and scores is not None:
        for score_name in scores:
            if scores[score_name] > config.get(f'max_{score_name}', scores[score_name]):
                for s in scores:
                    data[s] = scores[s]
                data["raw_data"] = lighthouse_data
                break

    return data

if __name__ == "__main__":
    sbsData = loadSBS("sample.csv")
    data = []
    pbar = tqdm.tqdm(total=len(sbsData))
    for row in sbsData:
        try:
            data.append(rate(row['url'], row['email'], row['name']))
        except Exception as e:
            print(f"\nFailed to process {row['url']}: {e}")
        pbar.update(1)
    with open("results.json", "w") as f:
        json.dump(data, f, indent=4)