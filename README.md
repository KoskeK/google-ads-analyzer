# Google Ads Analyzer

A Flask web application that takes a CSV of business contacts, detects Google Ads pixels on their websites, runs Google Lighthouse performance audits on sites that have ads, and saves / exports the results as JSON (optionally into MongoDB).

---

## Table of Contents

1. [Requirements](#requirements)
2. [Setup](#setup)
3. [Configuration](#configuration)
4. [Running the App](#running-the-app)
5. [Usage Walkthrough](#usage-walkthrough)
6. [Project Structure](#project-structure)

---

## Requirements

- Python 3.10+
- A Google PageSpeed / Lighthouse API key
- *(Optional)* A running MongoDB instance

---

## Setup

### 1. Clone / download the project

```bash
git clone <repo-url>
cd google-ads-analyzer
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install flask httpx pymongo tqdm
```

### 4. Add your Google API key

Create a file named `google_key` in the project root containing **only** your API key (no newline padding needed):

```
AIzaSy...your_key_here...
```

You can create a key at <https://console.cloud.google.com/> — enable the **PageSpeed Insights API**.

### 5. Configure the application

Edit `config.json` (see [Configuration](#configuration) below), then start the server.

---

## Configuration

All settings live in `config.json` at the project root. They can also be changed at runtime through the **Settings** page in the web UI (`/config`).

```jsonc
{
    // ── Login ───────────────────────────────────────────────
    "username": "admin",        // Web UI login username
    "password": "changeme",     // Web UI login password (plaintext)

    // ── MongoDB (optional) ──────────────────────────────────
    "mongo_url": "",            // MongoDB connection string, e.g. "mongodb://localhost:27017"
                                // Leave empty to disable — results will only exist in memory
                                // and in the downloaded JSON file.
    "mongo_db": "ads_analyzer", // Database name
    "mongo_collection": "results", // Collection name

    // ── Lighthouse score thresholds ─────────────────────────
    // A site is included in Lighthouse results only when at least
    // one of its scores EXCEEDS the corresponding threshold.
    // Set a value to 100 to effectively disable that filter.
    "max_performance": 75,
    "max_accessibility": 80,
    "max_best-practices": 100,
    "max_seo": 100,
    "max_lcp": 2.5,             // Largest Contentful Paint in seconds

    // ── Scan behaviour ──────────────────────────────────────
    "lighthouse_delay": 0.1,    // Seconds to wait between Lighthouse API calls

    // ── Logging ─────────────────────────────────────────────
    "log_file": "errors.log"    // Path to the error log file (relative to project root)
}
```

### Configuration reference

| Key | Type | Default | Description |
|---|---|---|---|
| `username` | string | `"admin"` | Web UI login username |
| `password` | string | `"changeme"` | Web UI login password (stored in plaintext) |
| `mongo_url` | string | `""` | Full MongoDB connection URI. Empty = MongoDB disabled. |
| `mongo_db` | string | `"ads_analyzer"` | MongoDB database name |
| `mongo_collection` | string | `"results"` | MongoDB collection name |
| `max_performance` | int 0–100 | `75` | Lighthouse performance threshold |
| `max_accessibility` | int 0–100 | `80` | Lighthouse accessibility threshold |
| `max_best-practices` | int 0–100 | `100` | Lighthouse best-practices threshold |
| `max_seo` | int 0–100 | `100` | Lighthouse SEO threshold |
| `max_lcp` | float | `2.5` | Largest Contentful Paint threshold (seconds) |
| `lighthouse_delay` | float | `0.1` | Pause (seconds) between Lighthouse API requests to avoid rate-limiting |
| `log_file` | string | `"errors.log"` | Path for the error log (relative to project root) |

---

## Running the App

```bash
source .venv/bin/activate
python web.py
```

The server starts at **http://127.0.0.1:5000**.

> The reloader is intentionally disabled (`use_reloader=False`) because the scan jobs run in background threads — the reloader would kill them on file changes.

---

## Usage Walkthrough

### 1. Log in

Navigate to `http://127.0.0.1:5000`. You will be redirected to the login page. Enter the credentials from `config.json` (defaults: `admin` / `changeme`).

### 2. Upload a CSV

Click **Upload & Map Columns** after selecting your CSV file. The file must be:
- UTF-8 encoded (UTF-8 with BOM is also accepted)
- No larger than 50 MB

The CSV can have any column names — you map them in the next step.

### 3. Map columns

Choose which CSV column corresponds to each field:

| Field | Required | Description |
|---|---|---|
| Website URL | **Yes** | Primary URL column to scan |
| URL Fallback | No | Used when the primary URL cell is empty |
| Contact Name | No | Stored alongside scan results |
| Contact Email | No | Stored alongside scan results |

Common column names are auto-detected and pre-selected.

Check **Skip URLs already in the database** (on by default) to avoid re-scanning sites already saved in MongoDB.

### 4. Scan

The app runs two phases in the background:

1. **Ad pixel check** — visits each URL and looks for Google Ads fingerprints (`gtag`, `googleadservices`, `aw-`, `gclid`, etc.) in the page HTML.
2. **Lighthouse** — for every site where ads were found, a Lighthouse report is fetched from the Google PageSpeed API concurrently.

A live progress page shows both phases with progress bars. Terminal output (prefixed `[scan]`) shows each URL being processed. The watchdog thread prints a heartbeat every 15 seconds so you can see if a URL is stuck.

### 5. Download results

When the scan completes, click **Download results.json**. The file contains an array of objects, one per URL:

```jsonc
[
  {
    "url": "https://example.com",
    "timestamp": "2026-03-12T14:00:00.000000",
    "email": "contact@example.com",
    "name": "Jane Smith",
    "has_ads": true,
    "performance": 62.0,
    "accessibility": 84.0,
    "best-practices": 95.0,
    "seo": 91.0,
    "lcp": 3.1,
    "raw_data": { ... }   // full Lighthouse JSON, only present when thresholds are exceeded
  },
  {
    "url": "https://no-ads-site.com",
    "timestamp": "...",
    "has_ads": false
  }
]
```

If MongoDB is configured, each result is also inserted into the collection as it completes.

### 6. Settings

Click the gear icon (⚙ Settings) in the top-right of any page to open the settings panel. Changes are written to `config.json` immediately and take effect for the next scan without restarting the server.

---

## Project Structure

```
google-ads-analyzer/
├── web.py              # Flask application (upload, map, scan, download, config, auth)
├── rater.py            # Core scanning logic (pixel detection, Lighthouse, CSV reading)
├── api.py              # FastAPI endpoint (alternative programmatic interface)
├── config.json         # Runtime configuration
├── google_key          # Google PageSpeed API key (not committed to version control)
├── sample.csv          # Example input CSV
├── templates/
│   ├── base.html       # Shared navbar layout
│   ├── login.html      # Login page
│   ├── index.html      # CSV upload page
│   ├── map.html        # Column mapping page
│   ├── scan.html       # Live scan progress page
│   └── config.html     # Settings page
└── tools/
    ├── parser.py       # Filters results.json to entries with ads
    ├── compare.py      # Counts differences between results files
    └── to_csv.py       # Converts good_data.json to a CSV
```
