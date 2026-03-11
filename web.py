import os
import uuid
import csv
import json
import threading
import concurrent.futures
import io
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file

# Ensure we run from the project root so rater.py can open config.json / google_key
_base_path = os.path.dirname(os.path.abspath(__file__))
if _base_path:
    os.chdir(_base_path)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "ads-analyzer-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

UPLOAD_DIR = "/tmp/ads_analyzer_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# In-memory stores (single-server / dev use)
uploads = {}  # upload_id -> {path, headers}
jobs = {}     # job_id -> {status, progress, total, lh_done, lh_total, results, error}
jobs_lock = threading.Lock()


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def guess_column(headers: list, keywords: list) -> str:
    """Return the first header whose name contains any of the keywords (case-insensitive)."""
    for h in headers:
        for kw in keywords:
            if kw.lower() in h.lower():
                return h
    return ""


def _watchdog(job_id: str, stop_event: threading.Event):
    """Prints a heartbeat every 15 s so you can see the job is alive and where it's stuck."""
    import time
    while not stop_event.wait(15):
        job = jobs.get(job_id, {})
        status   = job.get("status", "?")
        progress = job.get("progress", 0)
        total    = job.get("total", 0)
        lh_done  = job.get("lh_done", 0)
        lh_total = job.get("lh_total", 0)
        current  = job.get("current_url", "—")
        if status in ("done", "error"):
            break
        print(
            f"[watchdog] job={job_id[:8]}  status={status}  "
            f"pixel={progress}/{total}  lh={lh_done}/{lh_total}  "
            f"working_on={current}"
        )


def run_scan(job_id: str, rows: list):
    """Background thread: scan ad pixels then run Lighthouse concurrently."""
    import rater  # lazy import — avoids blocking Flask startup
    job = jobs[job_id]
    job["status"] = "scanning"
    results = []

    stop_event = threading.Event()
    watchdog = threading.Thread(target=_watchdog, args=(job_id, stop_event), daemon=True)
    watchdog.start()

    try:
        with concurrent.futures.ThreadPoolExecutor() as lh_pool:
            pending: dict = {}

            for row in rows:
                url = row["url"]
                with jobs_lock:
                    job["current_url"] = url
                print(f"[scan] pixel-check  {url}")
                try:
                    data = rater.rate(url, row["email"], row["name"])
                    if data["has_ads"]:
                        print(f"[scan] has_ads=True → queuing Lighthouse  {url}")
                        future = lh_pool.submit(rater._lighthouse_task, data, url)
                        pending[future] = data
                    else:
                        print(f"[scan] has_ads=False  {url}")
                        results.append(data)
                except Exception as e:
                    rater.log_error(f"Pixel scan failed for {url}: {e}")

                with jobs_lock:
                    job["progress"] += 1

            # Phase 2 – wait for Lighthouse scans
            with jobs_lock:
                job["status"] = "lighthouse"
                job["lh_total"] = len(pending)
                job["lh_done"] = 0
                job["current_url"] = "—"

            print(f"[scan] lighthouse phase — {len(pending)} URLs queued")
            for future in concurrent.futures.as_completed(pending):
                orig = pending[future]
                print(f"[scan] lighthouse done  {orig['url']}")
                try:
                    results.append(future.result())
                except Exception as e:
                    rater.log_error(f"Lighthouse failed for {orig['url']}: {e}")
                    results.append(orig)

                with jobs_lock:
                    job["lh_done"] += 1

        with jobs_lock:
            job["results"] = results
            job["status"] = "done"
        print(f"[scan] finished — {len(results)} results")

    except Exception as e:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = str(e)
        print(f"[scan] ERROR: {e}")

    finally:
        stop_event.set()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("csv_file")
    if not f or not f.filename.lower().endswith(".csv"):
        return render_template("index.html", error="Please upload a valid .csv file.")

    upload_id = str(uuid.uuid4())
    path = os.path.join(UPLOAD_DIR, f"{upload_id}.csv")
    f.save(path)

    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            headers = next(reader)
    except Exception:
        os.remove(path)
        return render_template("index.html", error="Could not read the CSV file. Make sure it is UTF-8 encoded.")

    uploads[upload_id] = {"path": path, "headers": headers}
    return redirect(url_for("map_columns", upload_id=upload_id))


@app.route("/map/<upload_id>")
def map_columns(upload_id):
    upload = uploads.get(upload_id)
    if not upload:
        return redirect(url_for("index"))

    headers = upload["headers"]
    guesses = {
        "url": guess_column(headers, ["website", "url", "web", "site", "link"]),
        "url_fallback": guess_column(headers, ["additional website", "additional url", "fallback", "alt"]),
        "name": guess_column(headers, ["contact person", "name"]),
        "email": guess_column(headers, ["email", "mail"]),
    }
    return render_template("map.html", upload_id=upload_id, headers=headers, guesses=guesses)


@app.route("/scan/<upload_id>", methods=["POST"])
def start_scan(upload_id):
    upload = uploads.get(upload_id)
    if not upload:
        return redirect(url_for("index"))

    url_col = request.form.get("url_col", "").strip()
    url_fallback_col = request.form.get("url_fallback_col", "").strip()
    name_col = request.form.get("name_col", "").strip()
    email_col = request.form.get("email_col", "").strip()

    if not url_col:
        return render_template(
            "map.html",
            upload_id=upload_id,
            headers=upload["headers"],
            guesses={},
            error="URL column is required.",
        )

    # Build rows from the CSV according to the user's mapping
    rows = []
    with open(upload["path"], newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            url = normalize_url(row.get(url_col, ""))
            if not url and url_fallback_col:
                url = normalize_url(row.get(url_fallback_col, ""))
            if not url:
                continue
            rows.append({
                "url": url,
                "name": row.get(name_col, "").strip() if name_col else "",
                "email": row.get(email_col, "").strip() if email_col else "",
            })

    if not rows:
        return render_template(
            "map.html",
            upload_id=upload_id,
            headers=upload["headers"],
            guesses={},
            error="No valid URLs found in the selected column.",
        )

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "starting",
            "progress": 0,
            "total": len(rows),
            "lh_done": 0,
            "lh_total": 0,
            "results": None,
            "error": None,
        }

    t = threading.Thread(target=run_scan, args=(job_id, rows), daemon=True)
    t.start()

    return redirect(url_for("scan_progress", job_id=job_id))


@app.route("/progress/<job_id>")
def scan_progress(job_id):
    if job_id not in jobs:
        return redirect(url_for("index"))
    return render_template("scan.html", job_id=job_id)


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "lh_done": job["lh_done"],
        "lh_total": job["lh_total"],
        "error": job["error"],
    })


@app.route("/download/<job_id>")
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return redirect(url_for("index"))

    buf = io.BytesIO(json.dumps(job["results"], indent=2).encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name="results.json")


if __name__ == "__main__":
    print("Starting Flask server on http://127.0.0.1:5000 ...")
    app.run(port=5000, debug=True, use_reloader=False)
