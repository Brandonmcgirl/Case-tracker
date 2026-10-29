"""Photo case counts (Claude reads the Markem 5000 screen) and the weekly schedule API.

Private data lives in data/ (git-ignored, the repo is public):
  data/products.json             case SKU -> product, from the US + Canadian M# checklists
  data/photos/<id>.jpg           recent Markem photos (kept PHOTO_KEEP_DAYS)
  data/schedules/<week>_<sheet>.enc    one week of one schedule sheet ("8hr" or "12hr"), encrypted
  data/import/<week>_<sheet>.json      drop a plain schedule here; it is encrypted and deleted on next use
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

import securestore as store
from server import app, load_log, save_log, end_run, format_local_time, is_admin, TIMEZONE

DATA_DIR = "data"
PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
SCHEDULE_DIR = os.path.join(DATA_DIR, "schedules")
IMPORT_DIR = os.path.join(DATA_DIR, "import")
SCHEDULE_ID = re.compile(r"\d{4}-\d{2}-\d{2}_(8hr|12hr)")
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
    # A run typed in by hand has no sku field; use a SKU number in its material text if there is one.
    active_sku = ""
    if active:
        active_sku = active.get("sku") or (re.search(r"\d{9}", active.get("material", "")) or [""])[0]
    if not active:
        plan = "start"
    elif not sku or not active_sku or active_sku == sku:
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
        if not is_admin():
            return jsonify({"error": "Only an admin can create lines"}), 403
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

def import_schedules():
    """Encrypt any plain schedule files dropped into data/import/, then delete the plain copies."""
    if not os.path.isdir(IMPORT_DIR):
        return
    for name in os.listdir(IMPORT_DIR):
        if not (name.endswith(".json") and SCHEDULE_ID.fullmatch(name[:-5])):
            continue
        path = os.path.join(IMPORT_DIR, name)
        with open(path) as f:
            sched = json.load(f)
        store.write(os.path.join(SCHEDULE_DIR, name[:-5] + ".enc"), sched)
        os.remove(path)
        print(f"schedule imported: {name[:-5]}")


@app.route("/api/schedules")
def schedules_index():
    """Every loaded week/sheet, newest first: [{id, week_start, sheet, status, title}]"""
    with store.locked("schedules"):
        import_schedules()
    out = []
    if os.path.isdir(SCHEDULE_DIR):
        for name in os.listdir(SCHEDULE_DIR):
            if not (name.endswith(".enc") and SCHEDULE_ID.fullmatch(name[:-4])):
                continue
            s = store.read(os.path.join(SCHEDULE_DIR, name), {})
            out.append({"id": name[:-4], "week_start": s.get("week_start"), "sheet": s.get("sheet"),
                        "status": s.get("status", "posted"), "title": s.get("title", "")})
    out.sort(key=lambda s: (s["week_start"] or "", s["sheet"] or ""), reverse=True)
    return jsonify(out)


@app.route("/api/schedules/<sched_id>")
def schedule_get(sched_id):
    path = os.path.join(SCHEDULE_DIR, sched_id + ".enc")
    if not SCHEDULE_ID.fullmatch(sched_id) or not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    return jsonify(store.read(path, {}))


# --- Schedule upload: photo / PDF / Word / Excel -> draft -> check -> publish ---------------------
# Read on this server only (local OCR / file text, no AI, no cloud). The uploaded file is never stored.

import subprocess
import sys
import schedule_parser

JOB_DIR = os.path.join(DATA_DIR, "jobs")
DRAFT_DIR = os.path.join(DATA_DIR, "drafts")
UPLOAD_TYPES = (".jpg", ".jpeg", ".png", ".webp", ".pdf", ".docx", ".xlsx", ".xlsm")


def _staff():
    return getattr(request, "role", None) in ("admin", "scheduler")


def _all_schedules():
    out = []
    if os.path.isdir(SCHEDULE_DIR):
        for name in sorted(os.listdir(SCHEDULE_DIR)):
            if name.endswith(".enc") and SCHEDULE_ID.fullmatch(name[:-4]):
                out.append(store.read(os.path.join(SCHEDULE_DIR, name), {}))
    return out


def _roster():
    names = set()
    for s in _all_schedules():
        for a in s.get("areas", []):
            for j in a["jobs"]:
                for sl in j.get("slots", {}).values():
                    for ppl in sl.values():
                        for p in ppl:
                            n = p if isinstance(p, str) else p.get("name", "")
                            if n and n[0].isalpha() and n != "(open)":
                                names.add(n)
    import accounts
    for g in accounts.load_gpids().values():
        names.add(g["schedule_name"])
    return names


def _job_write(job):
    os.makedirs(JOB_DIR, exist_ok=True)
    with open(os.path.join(JOB_DIR, job["id"] + ".json"), "w") as f:
        json.dump(job, f)


def _run_job(job, filename, data):
    try:
        job["message"] = "Reading the file..."; _job_write(job)
        words, img = schedule_parser.read_any(filename, data)
        sheet = "12hr" if any("CREW" in w["t"].upper() for w in words) else "8hr"
        same = [s for s in _all_schedules() if s.get("sheet") == sheet]
        template = same[-1] if same else None
        job["message"] = "Sorting names into days, shifts and jobs..."; _job_write(job)
        weeks = schedule_parser.parse(words, template=template, roster=_roster(), img=img)
        if not weeks:
            raise ValueError("Couldn't find the day headers (Sunday, Monday...) on this sheet.")
        for w in weeks:
            w["source"] = filename
            w["template"] = template.get("week_start") if template else None
            did = f"{w['week_start']}_{w['sheet']}_{w['status']}_{secrets.token_hex(3)}"
            store.write(os.path.join(DRAFT_DIR, did + ".enc"), w)
            job["drafts"].append(did)
        job["status"], job["message"] = "done", f"Read {len(weeks)} week(s). Check and publish below."
    except Exception as e:
        print(f"schedule job {job['id']} failed: {e!r}")
        job["status"], job["message"] = "error", f"Couldn't read that file: {e}"
    finally:
        del data
        _job_write(job)


@app.route("/api/schedules/upload", methods=["POST"])
def schedule_upload():
    if not _staff():
        return jsonify({"error": "Only the scheduler or admin can upload schedules"}), 403
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(UPLOAD_TYPES):
        return jsonify({"error": "Send a photo (JPG/PNG), PDF, Word (.docx) or Excel (.xlsx) file"}), 400
    job = {"id": datetime.datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3),
           "status": "running", "message": "Starting...", "file": f.filename, "drafts": [], "by": request.user_name}
    _job_write(job)
    # Read it in a separate low-priority process (OCR is CPU-heavy; the web workers must stay responsive).
    # The file goes over a pipe, never to disk.
    proc = subprocess.Popen([sys.executable, "-c", "import sys, features; features.run_job_stdin(sys.argv[1], sys.argv[2])",
                             job["id"], f.filename], stdin=subprocess.PIPE, cwd=os.path.dirname(os.path.abspath(__file__)),
                            start_new_session=True, preexec_fn=lambda: os.nice(10))
    proc.stdin.write(f.read())
    proc.stdin.close()
    return jsonify(job)


def run_job_stdin(job_id, filename):
    with open(os.path.join(JOB_DIR, job_id + ".json")) as fh:
        job = json.load(fh)
    _run_job(job, filename, sys.stdin.buffer.read())


@app.route("/api/schedules/jobs/<job_id>")
def schedule_job(job_id):
    if not _staff() or not re.fullmatch(r"[0-9-]+[0-9a-f]{6}", job_id):
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(JOB_DIR, job_id + ".json")
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    with open(path) as fh:
        return jsonify(json.load(fh))


DRAFT_ID = re.compile(r"\d{4}-\d{2}-\d{2}_(8hr|12hr)_(posted|proposed)_[0-9a-f]{6}")


def _draft_path(did):
    return os.path.join(DRAFT_DIR, did + ".enc") if DRAFT_ID.fullmatch(did) else None


def _review_count(d):
    return sum(1 for a in d["areas"] for j in a["jobs"] for sl in j.get("slots", {}).values()
               for ppl in sl.values() for p in ppl if isinstance(p, dict) and p.get("check"))


@app.route("/api/schedules/drafts")
def drafts_list():
    if not _staff():
        return jsonify({"error": "Not allowed"}), 403
    out = []
    if os.path.isdir(DRAFT_DIR):
        for name in sorted(os.listdir(DRAFT_DIR), reverse=True):
            if name.endswith(".enc") and DRAFT_ID.fullmatch(name[:-4]):
                d = store.read(os.path.join(DRAFT_DIR, name), {})
                out.append({"id": name[:-4], "week_start": d["week_start"], "sheet": d["sheet"], "status": d["status"],
                            "source": d.get("source"), "to_check": _review_count(d), "notes": d.get("review", [])[:50]})
    return jsonify(out)


@app.route("/api/schedules/drafts/<did>", methods=["GET", "POST", "DELETE"])
def draft_one(did):
    if not _staff():
        return jsonify({"error": "Not allowed"}), 403
    path = _draft_path(did)
    if not path or not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    if request.method == "DELETE":
        os.remove(path)
        return jsonify({"status": "deleted"})
    with store.locked("drafts"):
        d = store.read(path, {})
        if request.method == "GET":
            return jsonify(d)
        body = request.get_json(force=True)
        if body.get("action") == "publish":
            for a in d["areas"]:
                for j in a["jobs"]:
                    for sl in j.get("slots", {}).values():
                        for k, ppl in sl.items():
                            sl[k] = [{x: v for x, v in p.items() if x != "check"} if isinstance(p, dict) else p for p in ppl]
            d["review"] = []
            d["published_by"] = request.user_name
            d["published"] = format_local_time()
            store.write(os.path.join(SCHEDULE_DIR, f"{d['week_start']}_{d['sheet']}.enc"), d)
            os.remove(path)
            return jsonify({"status": "published", "id": f"{d['week_start']}_{d['sheet']}"})
        # edit one cell: {job, date, shift, names: ["LAST FIRST", ...]}
        job, date, sk = body.get("job"), body.get("date"), body.get("shift")
        names = [str(n).strip().upper() for n in body.get("names", []) if str(n).strip()]
        target = next((j for a in d["areas"] for j in a["jobs"] if j["name"] == job), None)
        if target is None:
            return jsonify({"error": "No such job"}), 404
        old = {(p if isinstance(p, str) else p["name"]): p for p in target.get("slots", {}).get(date, {}).get(sk, [])}
        new = []
        for n in names:
            p = old.get(n)
            new.append({x: v for x, v in p.items() if x != "check"} if isinstance(p, dict) else {"name": n})
        target.setdefault("slots", {}).setdefault(date, {})[sk] = new
        store.write(path, d)
        return jsonify({"status": "saved", "to_check": _review_count(d)})
