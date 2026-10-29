from flask import Flask, request, jsonify, send_from_directory, session, redirect
import json
import os
import datetime
import pytz

# Settings live in .env (never committed): SECRET_KEY, CASE_TRACKER_MODEL.
# Real secrets (ANTHROPIC_API_KEY, DATA_KEY) come from the hub's root-only
# /etc/northbridge/secrets/case-tracker.env via the systemd drop-in.
def _load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                k, v = raw.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="ct_session",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
)
LOG_FILE = "log.json"
SUGGESTIONS_FILE = "suggestions.txt"
TIMEZONE = pytz.timezone("America/New_York")  # EST equivalent


def current_role():
    """'admin', 'scheduler', 'member' or None. Set by accounts.check_session on every request."""
    return getattr(request, "role", None)

def is_admin():
    return current_role() == "admin"


@app.after_request
def security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
    if request.path.startswith("/api/") or request.path.endswith(".json"):
        response.headers["Cache-Control"] = "no-store"
    return response

# Serve frontend
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/schedule")
def schedule_page():
    return send_from_directory(".", "schedule.html")

# Only plain web assets with no hidden or private path parts are served (2026-10-24 security fix).
_PUBLIC_EXT = (".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2", ".webmanifest")
_PRIVATE_DIRS = {"venv", "scripts", "__pycache__", ".git", ".github", ".snapshots", "data"}

@app.route("/<path:path>")
def static_files(path):
    parts = path.split("/")
    if any(p.startswith(".") or p in _PRIVATE_DIRS for p in parts) or not path.lower().endswith(_PUBLIC_EXT):
        return "Not found", 404
    return send_from_directory(".", path)

# Safely load log.json
def load_log():
    if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
        with open(LOG_FILE, "w") as f:
            json.dump([], f)
    with open(LOG_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            with open(LOG_FILE, "w") as f2:
                json.dump([], f2)
            return []

def save_log(data):
    tmp = LOG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LOG_FILE)

# Handle suggestions with error handling
def save_suggestion(suggestion, user):
    try:
        timestamp = datetime.datetime.now(TIMEZONE).strftime("%a %b %d %Y %I:%M %p")
        suggestion_entry = f"[{timestamp}] {user}: {suggestion}\n"
        with open(SUGGESTIONS_FILE, "a") as f:
            f.write(suggestion_entry)
        return True
    except Exception as e:
        print(f"Error saving suggestion: {e}")
        return False

@app.route("/log.json")
def get_log():
    data = load_log()
    return jsonify(data)

@app.route("/suggestions", methods=["POST"])
def add_suggestion():
    try:
        data = request.get_json(force=True)
        suggestion = data.get("suggestion", "")
        user = getattr(request, "user_name", "") or "Anonymous"
        if not suggestion:
            return jsonify({"error": "Suggestion required"}), 400
        success = save_suggestion(suggestion, user)
        if success:
            return jsonify({"status": "Suggestion saved"})
        else:
            return jsonify({"error": "Failed to save suggestion"}), 500
    except Exception as e:
        print(f"Suggestion error: {e}")
        return jsonify({"error": "Server error"}), 500

def format_local_time(dt=None):
    if dt is None:
        dt = datetime.datetime.now(TIMEZONE)
    return dt.strftime("%a %b %d %Y %I:%M %p")

def end_run(line, log_data):
    """Close the active run on a line: keep its Init row, store last_run. Saves the log."""
    first_init = None
    last_entry = None
    start_entry = None
    new_log = []
    last_run_info = {}
    for e in log_data:
        if e.get("line") == line:
            if e.get("time") == "Init" and not first_init:
                first_init = e
            elif e.get("time") == "Start":
                start_entry = e
            elif e.get("time") != "Init":
                last_entry = e
        else:
            new_log.append(e)
    if start_entry and last_entry:
        start_dt = datetime.datetime.fromisoformat(start_entry["timestamp"])
        start_local = start_dt.astimezone(TIMEZONE)
        last_run_info = {
            "material": last_entry.get("material", "-"),
            "count": last_entry.get("count", 0),
            "start": format_local_time(start_local),
            "end": format_local_time(),
            "targetCount": start_entry.get("targetCount", 0),
        }
    elif last_entry:
        last_run_info = {
            "material": last_entry.get("material", "-"),
            "count": last_entry.get("count", 0),
            "start": last_entry.get("date", "") + " " + last_entry.get("time", ""),
            "end": format_local_time(),
            "targetCount": 0,
        }
    if first_init:
        new_log.append(first_init)
    last_run_entry = {"line": line, "last_run": last_run_info}
    new_log = [e for e in new_log if "last_run" not in e] + [last_run_entry]
    save_log(new_log)
    return last_run_info

@app.route("/add", methods=["POST"])
def add_entry():
    data = request.get_json(force=True)
    reset = data.get("reset", False)
    line = data.get("line", "")
    action = data.get("action", "add")
    entry = data.get("entry", {})
    if not line:
        return jsonify({"error": "Line not specified"}), 400
    log_data = load_log()
    if action == "delete":
        if not is_admin():
            return jsonify({"error": "Only an admin can delete lines"}), 403
        log_data = [e for e in log_data if e.get("line") != line]
        save_log(log_data)
        return jsonify({"status": f"Line {line} deleted"})
    if reset:
        log_data = [e for e in log_data if e.get("line") != line]
        save_log(log_data)
        return jsonify({"status": f"Line {line} reset"})
    if action == "endrun":
        last_run_info = end_run(line, log_data)
        return jsonify({"status": f"Run ended for {line}", "last_run": last_run_info})
    if entry.get("time") == "Init":
        line_exists = any(e.get("line") == line for e in log_data)
        if not line_exists and not is_admin():
            return jsonify({"error": "Only an admin can create lines"}), 403
    if entry.get("time") == "Start":
        has_active_start = any(e.get("line") == line and e.get("time") == "Start" for e in log_data)
        if has_active_start:
            return jsonify({"status": "Start already exists for active run"}), 200
        entry["timestamp"] = datetime.datetime.now(TIMEZONE).isoformat()
    log_data.append(entry)
    save_log(log_data)
    return jsonify({"status": "entry added"})

import accounts  # noqa: E402,F401  sign-up, sign-in with authenticator codes, admin (registers routes + login gate)
import features  # noqa: E402,F401  photo counts + schedule API (registers routes on app)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
