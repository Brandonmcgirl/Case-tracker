"""Photo case counts (Claude reads the Markem 5000 screen) and the weekly schedule API.

Private data lives in data/ (git-ignored, the repo is public):
  data/products.json             case SKU -> product, from the US + Canadian M# checklists
  data/photos/<id>.jpg           recent Markem photos (kept PHOTO_KEEP_DAYS)
  data/schedules/<week>_<sheet>.json   one week of one schedule sheet ("8hr" or "12hr")
"""
import base64
import datetime
import io
import json
import os
import re
import secrets
import time

import anthropic
from flask import request, jsonify, send_from_directory
from PIL import Image, ImageOps

from server import (app, check_role, load_log, save_log, load_downtime, save_downtime,
                    end_run, format_local_time, TIMEZONE)

DATA_DIR = "data"
PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
SCHEDULE_DIR = os.path.join(DATA_DIR, "schedules")
PHOTO_KEEP_DAYS = 7
PHOTO_MAX_SIDE = 1280
# Which Claude reads the photos: claude-opus-5-5 (default) or claude-haiku-4-5 (about 1/5 the cost).
# Set CASE_TRACKER_MODEL in .env to switch.
MODEL = os.environ.get("CASE_TRACKER_MODEL", "claude-opus-5-5")

READ_PROMPT = """This photo should show the screen of a Markem 5000 Series case coder on a food packing line.
Read these values exactly as printed on the screen:
- job: the full text after "Job:" at the top (for example "L12-8_300000000")
- batch: the number after "Batch :" under Counts (the case count for this run)
- total: the number after "Total :" under Counts
- screen_time: the date and time shown at the top right, as "YYYY-MM-DD HH:MM:SS" (24-hour)
- state: the value after "State:" (for example "Printing")
If the photo is not a Markem screen or a value cannot be read with confidence, set readable to false
or leave that field empty (use -1 for an unreadable number). Never guess digits."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "job": {"type": "string"},
        "batch": {"type": "integer"},
        "total": {"type": "integer"},
        "screen_time": {"type": "string"},
        "state": {"type": "string"},
        "problem": {"type": "string", "description": "Short reason if something could not be read, else empty"},
    },
    "required": ["readable", "job", "batch", "total", "screen_time", "state", "problem"],
    "additionalProperties": False,
}


def _role():
    data = request.form if request.form else (request.get_json(silent=True) or {})
    return check_role(data.get("password", ""))


def load_products():
    if not os.path.exists(PRODUCTS_FILE):
        return {}
    with open(PRODUCTS_FILE) as f:
        return json.load(f)


def save_products(products):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PRODUCTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(products, f, indent=2, sort_keys=True)
    os.replace(tmp, PRODUCTS_FILE)


def parse_job(job):
    """'L12-8_300000000' -> ('12', '300000000'). Either part may be '' if missing."""
    job = (job or "").upper().replace(" ", "")
    line = re.search(r"L(\d{1,3})", job)
    sku = re.search(r"(\d{9})", job)
    return (line.group(1) if line else ""), (sku.group(1) if sku else "")


def _prune_photos():
    cutoff = time.time() - PHOTO_KEEP_DAYS * 86400
    for name in os.listdir(PHOTO_DIR):
        p = os.path.join(PHOTO_DIR, name)
        if os.path.getmtime(p) < cutoff:
            os.remove(p)


def _shrink(raw):
    """Upright, at most PHOTO_MAX_SIDE px, JPEG. Keeps the API call cheap and the stored copy small."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def read_markem(jpeg, model=None):
    model = model or MODEL
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from the hub's secrets file (systemd EnvironmentFile)
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(jpeg).decode()}},
            {"type": "text", "text": READ_PROMPT},
        ],
    }]
    fmt = {"type": "json_schema", "schema": READ_SCHEMA}
    if model.startswith("claude-haiku"):
        # Haiku 4.5 takes no effort setting and no server-side fallbacks
        response = client.messages.create(model=model, max_tokens=1000, output_config={"format": fmt},
                                          messages=messages)
    else:
        response = client.beta.messages.create(
            model=model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": fmt},
            messages=messages,
        )
    print(f"photo read: {model} in={response.usage.input_tokens} out={response.usage.output_tokens}")
    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to read this photo")
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)


@app.route("/api/photo/read", methods=["POST"])
def photo_read():
    is_admin, is_user = _role()
    if not (is_admin or is_user):
        return jsonify({"error": "Wrong password"}), 403
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "Photo reading isn't switched on yet (no Claude API key on the server)."}), 503
    upload = request.files.get("photo")
    if not upload:
        return jsonify({"error": "No photo attached"}), 400
    try:
        jpeg = _shrink(upload.read())
    except Exception:
        return jsonify({"error": "That file isn't a photo I can open"}), 400

    os.makedirs(PHOTO_DIR, exist_ok=True)
    _prune_photos()
    photo_id = datetime.datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    with open(os.path.join(PHOTO_DIR, photo_id + ".jpg"), "wb") as f:
        f.write(jpeg)

    try:
        reading = read_markem(jpeg)
    except anthropic.RateLimitError:
        return jsonify({"error": "Claude is busy right now, try again in a minute"}), 503
    except anthropic.APIConnectionError:
        return jsonify({"error": "Couldn't reach Claude, check the server's internet"}), 503
    except anthropic.APIStatusError as e:
        print(f"Claude API error: {e.status_code} {e.message}")
        return jsonify({"error": f"Claude API error ({e.status_code})"}), 502
    except (ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e) or "Couldn't understand Claude's answer"}), 502

    line, sku = parse_job(reading.get("job"))
    product = load_products().get(sku) if sku else None
    screen_time = None
    try:
        screen_time = datetime.datetime.strptime(reading.get("screen_time", ""), "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        pass

    # What would saving do? Compare with the line's active run.
    log_data = load_log()
    active = next((e for e in log_data if e.get("line") == line and e.get("time") == "Start"), None)
    last = None
    if active:
        rows = [e for e in log_data if e.get("line") == line and e.get("time") not in ("Init", "Start") and "last_run" not in e]
        last = rows[-1]["count"] if rows else active.get("count", 0)
    # A run started by hand has no SKU, so treat it as the same product.
    if not active:
        plan = "start"
    elif not sku or not active.get("sku") or active.get("sku") == sku:
        plan = "add"
    else:
        plan = "new_run"

    return jsonify({
        "photo_id": photo_id,
        "reading": reading,
        "line": line,
        "line_exists": any(e.get("line") == line for e in log_data),
        "sku": sku,
        "product": product,
        "count": reading.get("batch", -1),
        "screen_time": screen_time,
        "plan": plan,
        "active_material": active.get("material") if active else None,
        "last_count": last,
    })


def _material_label(sku, name):
    if name and sku:
        return f"{name} ({sku})"
    return name or sku or "-"


@app.route("/api/photo/save", methods=["POST"])
def photo_save():
    """Save a checked photo reading: hourly entry on the active run, or start a (new) run."""
    data = request.get_json(force=True)
    is_admin, is_user = check_role(data.get("password", ""))
    if not (is_admin or is_user):
        return jsonify({"error": "Wrong password"}), 403
    line = str(data.get("line", "")).strip()
    sku = str(data.get("sku", "")).strip()
    name = str(data.get("product_name", "")).strip()
    try:
        count = int(data.get("count"))
    except (TypeError, ValueError):
        return jsonify({"error": "Case count must be a number"}), 400
    if not line or count < 0:
        return jsonify({"error": "Line and case count are required"}), 400
    try:
        when = TIMEZONE.localize(datetime.datetime.strptime(data.get("time", ""), "%Y-%m-%dT%H:%M"))
    except ValueError:
        when = datetime.datetime.now(TIMEZONE)
    target = data.get("target")
    target = int(target) if str(target or "").isdigit() else None

    products = load_products()
    if sku and name and sku not in products:  # new product typed in from the confirm screen
        products[sku] = {"name": name, "market": "", "kind": "12pk", "item": "", "added": format_local_time()}
        save_products(products)
    if sku and not name and sku in products:
        name = products[sku]["name"]
    material = _material_label(sku, name)

    log_data = load_log()
    if not any(e.get("line") == line for e in log_data):
        if not is_admin:
            return jsonify({"error": "Admin password required to create lines"}), 403
        log_data.append({"date": when.strftime("%a %b %d %Y"), "time": "Init", "material": "-",
                         "count": 0, "diff": 0, "line": line})
        save_log(log_data)

    # Only a confirmed "new_run" ends a run; otherwise an active run always gets the entry
    # (the page's plan may be stale if someone else saved in the meantime).
    active = next((e for e in log_data if e.get("line") == line and e.get("time") == "Start"), None)
    if active and data.get("plan") == "new_run":
        end_run(line, log_data)
        log_data = load_log()
        active = None

    if not active:
        log_data.append({
            "date": when.strftime("%a %b %d %Y"), "time": "Start", "material": material, "sku": sku,
            "count": count, "diff": 0, "line": line, "targetCount": target,
            "timestamp": when.isoformat(), "photo": data.get("photo_id", ""),
        })
        downtime = load_downtime()
        downtime.setdefault(line, {"status": "stopped", "start_time": None, "total_uptime": 0, "total_downtime": 0})
        if downtime[line]["start_time"] is None:
            downtime[line]["start_time"] = when.isoformat()
        save_downtime(downtime)
        save_log(log_data)
        return jsonify({"status": f"Run started on line {line}: {material}, {count} cases"})

    rows = [e for e in log_data if e.get("line") == line and e.get("time") not in ("Init", "Start") and "last_run" not in e]
    last = rows[-1]["count"] if rows else active.get("count", 0)
    log_data.append({
        "date": when.strftime("%a %b %d %Y"),
        "time": when.strftime("%I:%M %p").lstrip("0").lower(),
        "material": active.get("material", material), "sku": active.get("sku", sku),
        "count": count, "diff": count - (last or 0), "line": line, "photo": data.get("photo_id", ""),
    })
    save_log(log_data)
    return jsonify({"status": f"Line {line}: {count} cases (+{count - (last or 0)})"})


@app.route("/api/photo/<photo_id>.jpg")
def photo_file(photo_id):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", photo_id):
        return "Not found", 404
    return send_from_directory(PHOTO_DIR, photo_id + ".jpg")


@app.route("/api/products")
def products_list():
    return jsonify(load_products())


# --- Schedule ---------------------------------------------------------------

@app.route("/api/schedules")
def schedules_index():
    """Every loaded week/sheet, newest first: [{week_start, sheet, status, title}]"""
    out = []
    if os.path.isdir(SCHEDULE_DIR):
        for name in os.listdir(SCHEDULE_DIR):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(SCHEDULE_DIR, name)) as f:
                s = json.load(f)
            out.append({"id": name[:-5], "week_start": s.get("week_start"), "sheet": s.get("sheet"),
                        "status": s.get("status", "posted"), "title": s.get("title", "")})
    out.sort(key=lambda s: (s["week_start"] or "", s["sheet"] or ""), reverse=True)
    return jsonify(out)


@app.route("/api/schedules/<sched_id>")
def schedule_get(sched_id):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}_(8hr|12hr)", sched_id):
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(SCHEDULE_DIR, sched_id + ".json")
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))
