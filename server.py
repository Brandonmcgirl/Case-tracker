from flask import Flask, request, jsonify, send_from_directory
import json
import os
import datetime
import pytz

app = Flask(__name__)
LOG_FILE = "log.json"
SUGGESTIONS_FILE = "suggestions.txt"
DOWNTIME_FILE = "downtime.json"
ADMIN_PASSWORD = "tyvek"
USER_PASSWORD = "pep"
TIMEZONE = pytz.timezone("America/New_York")  # EST equivalent

# --- CORS ---
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# Serve frontend
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/<path:path>")
def static_files(path):
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
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# Handle downtime tracking - PER LINE
def load_downtime():
    if not os.path.exists(DOWNTIME_FILE) or os.path.getsize(DOWNTIME_FILE) == 0:
        default_downtime = {}
        save_downtime(default_downtime)
        return default_downtime
    with open(DOWNTIME_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            default_downtime = {}
            save_downtime(default_downtime)
            return default_downtime

def save_downtime(data):
    with open(DOWNTIME_FILE, "w") as f:
        json.dump(data, f, indent=2)

# Get current downtime stats for a line - FIXED WITH REAL-TIME CALCULATION
def get_current_downtime_stats(line):
    """Calculate real-time uptime/downtime including current session"""
    downtime_data = load_downtime()
    if line not in downtime_data:
        return {"uptime": 0, "downtime": 0}
    
    line_data = downtime_data[line]
    start_time_str = line_data.get("start_time")
    
    # If no run has started, return zeros
    if not start_time_str:
        return {"uptime": 0, "downtime": 0}
    
    try:
        # Calculate total run time (from start until now)
        start_time = datetime.datetime.fromisoformat(start_time_str)
        total_run_time = (datetime.datetime.now(TIMEZONE) - start_time).total_seconds()
        
        # Get accumulated uptime from stopped sessions
        uptime = line_data.get("total_uptime", 0)
        
        # Add current running session time if currently tracking uptime
        if line_data.get("status") == "running" and line_data.get("session_start"):
            session_start = datetime.datetime.fromisoformat(line_data["session_start"])
            current_session_time = (datetime.datetime.now(TIMEZONE) - session_start).total_seconds()
            uptime += current_session_time
        
        # Simple math: downtime = total time - uptime
        downtime = max(0, total_run_time - uptime)
        
        return {"uptime": uptime, "downtime": downtime}
    except Exception as e:
        print(f"Error calculating downtime stats: {e}")
        return {"uptime": 0, "downtime": 0}

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

@app.route("/downtime.json")
def get_downtime():
    data = load_downtime()
    calculated_data = {}
    for line, line_data in data.items():
        current_stats = get_current_downtime_stats(line)
        calculated_data[line] = {
            "status": line_data["status"],
            "total_uptime": current_stats["uptime"],
            "total_downtime": current_stats["downtime"],
            "start_time": line_data["start_time"],
            "session_start": line_data.get("session_start")  # Include session start for frontend
        }
    return jsonify(calculated_data)

@app.route("/downtime/<line>/start", methods=["POST"])
def start_downtime_tracking(line):
    data = request.get_json(force=True)
    password = data.get("password", "")
    is_admin = password == ADMIN_PASSWORD
    is_user = password == USER_PASSWORD
    has_permission = is_admin or is_user
    if not has_permission:
        return jsonify({"error": "Wrong password"}), 403
    
    downtime_data = load_downtime()
    
    if line not in downtime_data:
        downtime_data[line] = {
            "status": "stopped",
            "start_time": None,
            "total_uptime": 0,
            "total_downtime": 0
        }
    
    # Check if there's an active run
    log_data = load_log()
    active_run = any(e.get("line") == line and e.get("time") == "Start" for e in log_data)
    if not active_run:
        return jsonify({"error": "No active run"}), 400
    
    # Only start if currently stopped
    if downtime_data[line]["status"] == "stopped":
        downtime_data[line]["status"] = "running"
        # Record when this uptime session started (for accumulating time)
        downtime_data[line]["session_start"] = datetime.datetime.now(TIMEZONE).isoformat()
        save_downtime(downtime_data)
        return jsonify({"status": f"Uptime tracking started for line {line}"})
    else:
        return jsonify({"status": f"Line {line} already running"}), 200

@app.route("/downtime/<line>/stop", methods=["POST"])
def stop_downtime_tracking(line):
    data = request.get_json(force=True)
    password = data.get("password", "")
    is_admin = password == ADMIN_PASSWORD
    is_user = password == USER_PASSWORD
    has_permission = is_admin or is_user
    if not has_permission:
        return jsonify({"error": "Wrong password"}), 403
    
    downtime_data = load_downtime()
    
    if line not in downtime_data:
        downtime_data[line] = {
            "status": "stopped",
            "start_time": None,
            "total_uptime": 0,
            "total_downtime": 0
        }
    
    # Only stop if currently running
    if downtime_data[line]["status"] == "running" and downtime_data[line].get("session_start"):
        # Calculate time elapsed during this uptime session
        session_start = datetime.datetime.fromisoformat(downtime_data[line]["session_start"])
        elapsed = (datetime.datetime.now(TIMEZONE) - session_start).total_seconds()
        downtime_data[line]["total_uptime"] += elapsed
        downtime_data[line]["status"] = "stopped"
        # Remove session start time since we're stopping
        downtime_data[line].pop("session_start", None)
        save_downtime(downtime_data)
        return jsonify({"status": f"Uptime tracking stopped for line {line}"})
    else:
        return jsonify({"status": f"Line {line} already stopped"}), 200

@app.route("/suggestions", methods=["POST"])
def add_suggestion():
    try:
        data = request.get_json(force=True)
        suggestion = data.get("suggestion", "")
        user = data.get("user", "Anonymous")
        if not suggestion:
            return jsonify({"error": "Suggestion required"}), 400
        if not os.path.exists(SUGGESTIONS_FILE):
            with open(SUGGESTIONS_FILE, "w") as f:
                pass
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

@app.route("/add", methods=["POST", "OPTIONS"])
def add_entry():
    if request.method == "OPTIONS":
        return "", 200
    data = request.get_json(force=True)
    password = data.get("password", "")
    is_admin = password == ADMIN_PASSWORD
    is_user = password == USER_PASSWORD
    has_permission = is_admin or is_user
    if not has_permission:
        return jsonify({"error": "Wrong password"}), 403
    reset = data.get("reset", False)
    line = data.get("line", "")
    action = data.get("action", "add")
    entry = data.get("entry", {})
    if action in ["delete"] and not is_admin:
        return jsonify({"error": "Admin password required for this action"}), 403
    if not line:
        return jsonify({"error": "Line not specified"}), 400
    log_data = load_log()
    if action == "delete":
        if not is_admin:
            return jsonify({"error": "Admin password required to delete lines"}), 403
        log_data = [e for e in log_data if e.get("line") != line]
        save_log(log_data)
        return jsonify({"status": f"Line {line} deleted"})
    if reset:
        log_data = [e for e in log_data if e.get("line") != line]
        save_log(log_data)
        return jsonify({"status": f"Line {line} reset"})
    if action == "endrun":
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
            downtime_stats = get_current_downtime_stats(line)
            last_run_info = {
                "material": last_entry.get("material", "-"),
                "count": last_entry.get("count", 0),
                "start": format_local_time(start_local),
                "end": format_local_time(),
                "targetCount": start_entry.get("targetCount", 0),
                "uptime": downtime_stats["uptime"],
                "downtime": downtime_stats["downtime"]
            }
        elif last_entry:
            downtime_stats = get_current_downtime_stats(line)
            last_run_info = {
                "material": last_entry.get("material", "-"),
                "count": last_entry.get("count", 0),
                "start": last_entry.get("date", "") + " " + last_entry.get("time", ""),
                "end": format_local_time(),
                "targetCount": 0,
                "uptime": downtime_stats["uptime"],
                "downtime": downtime_stats["downtime"]
            }
        if first_init:
            new_log.append(first_init)
        last_run_entry = {"line": line, "last_run": last_run_info}
        new_log = [e for e in new_log if "last_run" not in e] + [last_run_entry]
        downtime_data = load_downtime()
        if line in downtime_data:
            downtime_data[line] = {
                "status": "stopped",
                "start_time": None,
                "total_uptime": 0,
                "total_downtime": 0
            }
            save_downtime(downtime_data)
        save_log(new_log)
        return jsonify({"status": f"Run ended for {line}", "last_run": last_run_info})
    if entry.get("time") == "Init":
        line_exists = any(e.get("line") == line for e in log_data)
        if not line_exists and not is_admin:
            return jsonify({"error": "Admin password required to create lines"}), 403
    if entry.get("time") == "Start":
        has_active_start = any(e.get("line") == line and e.get("time") == "Start" for e in log_data)
        if has_active_start:
            return jsonify({"status": "Start already exists for active run"}), 200
        entry["timestamp"] = datetime.datetime.now(TIMEZONE).isoformat()
        downtime_data = load_downtime()
        if line not in downtime_data:
            downtime_data[line] = {
                "status": "stopped",
                "start_time": None,
                "total_uptime": 0,
                "total_downtime": 0
            }
        # Set start_time only once when run starts - THIS IS THE KEY FIX
        if downtime_data[line]["start_time"] is None:
            downtime_data[line]["start_time"] = datetime.datetime.now(TIMEZONE).isoformat()
        save_downtime(downtime_data)
    log_data.append(entry)
    save_log(log_data)
    return jsonify({"status": "entry added"})

if __name__ == "__main__":
    if not os.path.exists(SUGGESTIONS_FILE):
        with open(SUGGESTIONS_FILE, "w") as f:
            f.write("# Hourly Case Tracker Suggestions\n")
        print(f"Created {SUGGESTIONS_FILE}")
    if not os.path.exists(DOWNTIME_FILE):
        with open(DOWNTIME_FILE, "w") as f:
            json.dump({}, f, indent=2)
        print(f"Created {DOWNTIME_FILE}")
    print("Server running on port 5000")
    app.run(host="0.0.0.0", port=5000)