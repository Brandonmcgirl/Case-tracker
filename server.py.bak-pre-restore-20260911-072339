from flask import Flask, request, jsonify, send_from_directory
import json, os

app = Flask(__name__)
LOG_FILE = "log.json"
PASSWORD = "tyvek"

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
@app.route("/log.json")
def get_log():
    if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
        with open(LOG_FILE, "w") as f:
            json.dump([], f)

    with open(LOG_FILE, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = []
            with open(LOG_FILE, "w") as f2:
                json.dump([], f2)

    print("Served log.json, entries:", len(data))
    return jsonify(data)

# Add, reset, delete entries
@app.route("/add", methods=["POST", "OPTIONS"])
def add_entry():
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json(force=True)
    print("Received POST:", data)

    if data.get("password") != PASSWORD:
        return jsonify({"error": "Wrong password"}), 403

    reset = data.get("reset", False)
    line = data.get("line", "")
    action = data.get("action", "add")
    entry = data.get("entry", {})

    if not line:
        return jsonify({"error": "Line not specified"}), 400

    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0:
        with open(LOG_FILE, "r") as f:
            try:
                log_data = json.load(f)
            except json.JSONDecodeError:
                log_data = []
    else:
        log_data = []

    # Delete line
    if action == "delete":
        log_data = [e for e in log_data if e.get("line") != line]
        with open(LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2)
        return jsonify({"status": f"Line {line} deleted"})

    # Reset line
    if reset:
        log_data = [e for e in log_data if e.get("line") != line]
        with open(LOG_FILE, "w") as f:
            json.dump(log_data, f, indent=2)
        return jsonify({"status": f"Line {line} reset"})

    # Add entry
    log_data.append(entry)
    with open(LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)
    return jsonify({"status": "entry added"})

if __name__ == "__main__":
    print("Server running on port 5000")
    app.run(host="0.0.0.0", port=5000)