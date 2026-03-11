import time
import httpx
import warnings
import csv
import tqdm
import concurrent.futures
import logging
from datetime import datetime
import json

# Suppress SSL warnings for small biz sites with expired certificates
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

with open('config.json', 'r') as f:
    config = json.load(f)

logging.basicConfig(
    filename=config.get('log_file', 'errors.log'),
    level=logging.ERROR,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

def log_error(msg):
    logging.error(msg)
    print(f"\nERROR: {msg}")

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
        log_error(f"detect_pixel failed for {url}")
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
                log_error(f"Lighthouse API rate limit/server error (HTTP {response.status_code}) for {url}, attempt {attempt + 1}")
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

            time.sleep(config.get('lighthouse_delay', 2))
            return data, scores

        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            log_error(f"Lighthouse request error for {url} (attempt {attempt + 1}): {e}")
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
    has_ads = detect_pixel(url)
    data['has_ads'] = has_ads

    if has_ads:
        lighthouse_data, scores = fetch_lighthouse_report(url)
        if scores is not None:
            for score_name in scores:
                if scores[score_name] > config.get(f'max_{score_name}', scores[score_name]):
                    for s in scores:
                        data[s] = scores[s]
                    data["raw_data"] = lighthouse_data
                    break

    collection.insert_one(data)

def _lighthouse_task(base_data, url):
    """Runs in a worker thread: fetches lighthouse and merges scores into base_data."""
    lighthouse_data, scores = fetch_lighthouse_report(url)
    if scores is not None:
        for score_name in scores:
            if scores[score_name] > config.get(f'max_{score_name}', scores[score_name]):
                for s in scores:
                    base_data[s] = scores[s]
                base_data["raw_data"] = lighthouse_data
                break
    return base_data

def rate(url, email, name):
    data = {"url": url, "timestamp": datetime.today().isoformat(), "email": email, "name": name}
    has_ads = detect_pixel(url)
    data['has_ads'] = has_ads
    return data

if __name__ == "__main__":
    sbsData = loadSBS("sample.csv")
    results = []
    pbar = tqdm.tqdm(total=len(sbsData))

    # Pool for lighthouse scans — runs concurrently while main thread keeps scanning for ads
    with concurrent.futures.ThreadPoolExecutor() as lighthouse_pool:
        pending_futures = {}

        for row in sbsData:
            try:
                data = rate(row['url'], row['email'], row['name'])
                if data['has_ads']:
                    # Hand off to a worker thread immediately and keep going
                    future = lighthouse_pool.submit(_lighthouse_task, data, row['url'])
                    pending_futures[future] = data
                else:
                    results.append(data)
            except Exception as e:
                log_error(f"Failed to process {row['url']}: {e}")
            pbar.update(1)

        pbar.close()

        # Collect lighthouse results as they finish
        print(f"\nWaiting for {len(pending_futures)} lighthouse scans to complete...")
        for future in concurrent.futures.as_completed(pending_futures):
            try:
                results.append(future.result())
            except Exception as e:
                original_data = pending_futures[future]
                log_error(f"Lighthouse task failed for {original_data['url']}: {e}")
                results.append(original_data)

    with open("results.json", "w") as f:
        json.dump(results, f, indent=4)