import os
import uuid
import csv
import json
import threading
import concurrent.futures
import io
import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, session
from functools import wraps

# Ensure we run from the project root so rater.py can open config.json / google_key
_base_path = os.path.dirname(os.path.abspath(__file__))
if _base_path:
    os.chdir(_base_path)

# Load config for MongoDB connection details
with open("config.json", "r") as _f:
    _config = json.load(_f)


def _get_mongo_collection():
    """Return a pymongo Collection if mongo_url is configured, else None."""
    mongo_url = _config.get("mongo_url", "").strip()
    if not mongo_url:
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        db_name  = _config.get("mongo_db", "ads_analyzer")
        col_name = _config.get("mongo_collection", "results")
        return client[db_name][col_name]
    except Exception as e:
        print(f"[mongo] Connection failed: {e}")
        return None


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "ads-analyzer-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

UPLOAD_DIR = "/tmp/ads_analyzer_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

RESULTS_DIR = os.path.join(_base_path, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# In-memory stores (single-server / dev use)
uploads = {}  # upload_id -> {path, headers}
jobs = {}     # job_id -> {status, progress, total, lh_done, lh_total, results, error, csv_file}
jobs_lock = threading.Lock()

_CORE_FIELDS = ["url", "timestamp", "email", "name", "has_ads", "detected_tags",
                "performance", "accessibility", "best-practices", "seo", "lcp"]


def save_results_csv(job_id: str, results: list) -> str:
    """Write results to RESULTS_DIR/<timestamp>_<job_id_short>.csv and return the filename."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"scan_{ts}_{job_id[:8]}.csv"
    path = os.path.join(RESULTS_DIR, filename)

    # Build fieldnames: core fields first, then any extra scalar keys.
    # Skip 'raw_data' and any other nested dict/list fields — they don't belong in CSV.
    _SKIP_FIELDS = {"raw_data"}
    extra_keys = []
    for row in results:
        for k, v in row.items():
            if k not in _CORE_FIELDS and k not in extra_keys and k not in _SKIP_FIELDS:
                if not isinstance(v, (dict, list)):
                    extra_keys.append(k)
    fieldnames = _CORE_FIELDS + extra_keys

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore", restval="")
        writer.writeheader()
        for row in results:
            flat = {k: v for k, v in row.items() if not isinstance(v, (dict, list)) or k == "detected_tags"}
            if isinstance(flat.get("detected_tags"), list):
                flat["detected_tags"] = ", ".join(flat["detected_tags"])
            writer.writerow(flat)

    print(f"[scan] saved CSV → {path}  ({len(results)} rows)")
    return filename


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


def run_scan(job_id: str, rows: list, skip_existing: bool = False):
    """Background thread: scan ad pixels then run Lighthouse concurrently."""
    try:
        import rater  # lazy import — avoids blocking Flask startup
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = f"Failed to load scanner module: {e}"
        print(f"[scan] IMPORT ERROR: {e}")
        return

    job = jobs[job_id]
    job["status"] = "scanning"
    results = []

    try:
        collection = _get_mongo_collection()
    except Exception as e:
        print(f"[mongo] Error getting collection: {e}")
        collection = None
    if collection is not None:
        print(f"[mongo] Connected — saving to {_config.get('mongo_db')}.{_config.get('mongo_collection')}")
    else:
        print("[mongo] No mongo_url set — results will only be held in memory")

    # Build set of already-scanned URLs so we can skip them
    already_scanned = set()
    if skip_existing and collection is not None:
        try:
            already_scanned = {doc["url"] for doc in collection.find({}, {"url": 1, "_id": 0})}
            print(f"[mongo] skip_existing=True — {len(already_scanned)} URLs already in DB")
        except Exception as e:
            print(f"[mongo] Could not fetch existing URLs: {e}")

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

                if url in already_scanned:
                    print(f"[scan] skipping (already in DB)  {url}")
                    with jobs_lock:
                        job["progress"] += 1
                    continue

                print(f"[scan] pixel-check  {url}")
                try:
                    data = rater.rate(url, row["email"], row["name"])
                    data.update(row.get("extra", {}))  # merge extra CSV columns into result
                    if data["has_ads"]:
                        print(f"[scan] has_ads=True → queuing Lighthouse  {url}")
                        future = lh_pool.submit(rater._lighthouse_task, data, url)
                        pending[future] = data
                    else:
                        print(f"[scan] has_ads=False  {url}")
                        results.append(data)
                        if collection is not None:
                            try:
                                collection.insert_one({**data})
                            except Exception as me:
                                rater.log_error(f"MongoDB insert failed for {url}: {me}")
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
                    r = future.result()
                    results.append(r)
                    if collection is not None:
                        try:
                            collection.insert_one({**r})
                        except Exception as me:
                            rater.log_error(f"MongoDB insert failed for {orig['url']}: {me}")
                except Exception as e:
                    rater.log_error(f"Lighthouse failed for {orig['url']}: {e}")
                    results.append(orig)
                    if collection is not None:
                        try:
                            collection.insert_one({**orig})
                        except Exception as me:
                            rater.log_error(f"MongoDB insert failed for {orig['url']}: {me}")

                with jobs_lock:
                    job["lh_done"] += 1

        csv_filename = None
        csv_save_error = None
        try:
            csv_filename = save_results_csv(job_id, results)
        except Exception as csv_err:
            csv_save_error = str(csv_err)
            print(f"[scan] CSV save failed: {csv_err}")

        with jobs_lock:
            job["results"] = results
            job["csv_file"] = csv_filename
            job["csv_save_error"] = csv_save_error
            job["status"] = "done"
        print(f"[scan] finished — {len(results)} results")

    except Exception as e:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = str(e)
        print(f"[scan] ERROR: {e}")

    finally:
        stop_event.set()


# ── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == _config.get("username") and password == _config.get("password"):
            session["logged_in"] = True
            next_page = request.args.get("next") or url_for("index")
            return redirect(next_page)
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
@login_required
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
@login_required
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
@login_required
def start_scan(upload_id):
    upload = uploads.get(upload_id)
    if not upload:
        return redirect(url_for("index"))

    url_col = request.form.get("url_col", "").strip()
    url_fallback_col = request.form.get("url_fallback_col", "").strip()
    name_col = request.form.get("name_col", "").strip()
    email_col = request.form.get("email_col", "").strip()
    skip_existing = request.form.get("skip_existing") == "1"
    extra_cols = request.form.getlist("extra_cols")

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
                "extra": {col: row.get(col, "") for col in extra_cols},
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
            "csv_file": None,
            "csv_save_error": None,
        }

    t = threading.Thread(target=run_scan, args=(job_id, rows, skip_existing), daemon=True)
    t.start()

    return redirect(url_for("scan_progress", job_id=job_id))


@app.route("/progress/<job_id>")
@login_required
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
        "csv_file": job.get("csv_file"),
        "csv_save_error": job.get("csv_save_error"),
    })


@app.route("/download/<job_id>")
@login_required
def download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return redirect(url_for("index"))

    buf = io.BytesIO(json.dumps(job["results"], indent=2).encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="application/json", as_attachment=True, download_name="results.json")


@app.route("/download_result/<path:filename>")
@login_required
def download_result(filename):
    # Prevent path traversal
    safe = os.path.basename(filename)
    path = os.path.join(RESULTS_DIR, safe)
    if not os.path.isfile(path):
        return redirect(url_for("results_list"))
    return send_file(path, mimetype="text/csv", as_attachment=True, download_name=safe)


@app.route("/download_csv/<job_id>")
@login_required
def download_csv(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("csv_file"):
        return redirect(url_for("index"))
    path = os.path.join(RESULTS_DIR, job["csv_file"])
    if not os.path.isfile(path):
        return redirect(url_for("index"))
    return send_file(path, mimetype="text/csv", as_attachment=True,
                     download_name=job["csv_file"])


@app.route("/export/<fmt>")
@login_required
def export_db(fmt):
    if fmt not in ("json", "csv"):
        return redirect(url_for("results_list"))

    collection = None
    try:
        collection = _get_mongo_collection()
    except Exception as e:
        pass

    if collection is None:
        return render_template("results.html", files=[], results_dir=RESULTS_DIR,
                               dir_error=None,
                               export_error="MongoDB is not configured. Set mongo_url in Settings."), 400

    try:
        docs = list(collection.find({}, {"_id": 0}))
    except Exception as e:
        return render_template("results.html", files=[], results_dir=RESULTS_DIR,
                               dir_error=None,
                               export_error=f"MongoDB query failed: {e}"), 500

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        buf = io.BytesIO(json.dumps(docs, indent=2, default=str).encode("utf-8"))
        buf.seek(0)
        return send_file(buf, mimetype="application/json", as_attachment=True,
                         download_name=f"db_export_{ts}.json")

    # CSV export
    # Flatten: detected_tags list → comma string; drop nested dicts (raw_data etc)
    _SKIP = {"raw_data"}
    fieldnames = list(_CORE_FIELDS)  # start with canonical order
    extra_seen = []
    for doc in docs:
        for k, v in doc.items():
            if k not in _CORE_FIELDS and k not in extra_seen and k not in _SKIP:
                if not isinstance(v, (dict, list)):
                    extra_seen.append(k)
    fieldnames += extra_seen

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", restval="")
    writer.writeheader()
    for doc in docs:
        flat = {k: v for k, v in doc.items() if not isinstance(v, (dict, list)) or k == "detected_tags"}
        if isinstance(flat.get("detected_tags"), list):
            flat["detected_tags"] = ", ".join(flat["detected_tags"])
        writer.writerow(flat)

    out = io.BytesIO(buf.getvalue().encode("utf-8"))
    out.seek(0)
    return send_file(out, mimetype="text/csv", as_attachment=True,
                     download_name=f"db_export_{ts}.csv")


@app.route("/results")
@login_required
def results_list():
    files = []
    dir_error = None
    try:
        for fname in sorted(os.listdir(RESULTS_DIR), reverse=True):
            if fname.endswith(".csv"):
                full = os.path.join(RESULTS_DIR, fname)
                size_kb = os.path.getsize(full) / 1024
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(full))
                files.append({"name": fname, "size_kb": round(size_kb, 1),
                               "modified": mtime.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        dir_error = str(e)
        print(f"[results] Error listing {RESULTS_DIR}: {e}")
    return render_template("results.html", files=files, results_dir=RESULTS_DIR, dir_error=dir_error)


@app.route("/config", methods=["GET", "POST"])
@login_required
def config_page():
    global _config
    saved = False
    error = None

    if request.method == "POST":
        try:
            new_config = {
                "max_performance":   int(request.form["max_performance"]),
                "max_accessibility": int(request.form["max_accessibility"]),
                "max_best-practices": int(request.form["max_best-practices"]),
                "max_seo":           int(request.form["max_seo"]),
                "max_lcp":           float(request.form["max_lcp"]),
                "lighthouse_delay":  float(request.form["lighthouse_delay"]),
                "log_file":          request.form["log_file"].strip() or "errors.log",
                "mongo_url":         request.form["mongo_url"].strip(),
                "mongo_db":          request.form["mongo_db"].strip() or "ads_analyzer",
                "mongo_collection":  request.form["mongo_collection"].strip() or "results",
                "username":          request.form["username"].strip() or _config.get("username", "admin"),
                "password":          request.form["password"] or _config.get("password", "changeme"),
            }
            with open("config.json", "w") as f:
                json.dump(new_config, f, indent=4)
            _config = new_config
            saved = True
        except (ValueError, KeyError) as e:
            error = f"Invalid value: {e}"

    return render_template("config.html", config=_config, saved=saved, error=error)


if __name__ == "__main__":
    print("Starting Flask server on http://127.0.0.1:5000 ...")
    app.run(port=5000, debug=True, use_reloader=False,host='0.0.0.0')
