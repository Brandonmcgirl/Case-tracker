"""Per-person accounts: sign up with a GPID on the allow-list, email + password, and an
authenticator app (Authy, Google/Microsoft Authenticator, ...) that is required at every sign-in.
New accounts wait for an admin or scheduler to approve them.

Roles: admin (everything), scheduler (approve people, manage GPIDs), member (use the site).
Stored encrypted (securestore): data/users.enc, data/gpids.enc.
"""
import datetime
import io
import os
import re
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg
from flask import request, session, jsonify, redirect, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

import securestore as store
from server import app, TIMEZONE

USERS_FILE = os.path.join(store.DATA_DIR, "users.enc")
GPIDS_FILE = os.path.join(store.DATA_DIR, "gpids.enc")
ISSUER = "Case Tracker"
MIN_PASSWORD = 10
FAIL_LIMIT, FAIL_WINDOW = 5, 15 * 60          # per account: 5 wrong tries -> locked 15 min
IP_LIMIT = 20                                 # per IP address in the same window
STEP2_SECONDS = 5 * 60                        # time allowed between password and code
ROLES = ("member", "scheduler", "admin")

OPEN_PATHS = {"/login", "/signup", "/logout", "/api/auth/login", "/api/auth/code",
              "/api/auth/signup", "/api/auth/enroll"}
_ip_fails = {}


def _now():
    return datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _ip():
    return request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def _ip_blocked():
    now = time.time()
    _ip_fails[_ip()] = [t for t in _ip_fails.get(_ip(), []) if now - t < FAIL_WINDOW]
    return len(_ip_fails[_ip()]) >= IP_LIMIT


def _ip_fail():
    _ip_fails.setdefault(_ip(), []).append(time.time())


def schedule_name(full_name):
    """'Jane Doe' -> 'DOE JANE' (how the posted schedules print names)."""
    parts = full_name.split()
    return " ".join([parts[-1]] + parts[:-1]).upper() if len(parts) > 1 else full_name.upper()


def load_users():
    return store.read(USERS_FILE, {})


def load_gpids():
    gpids = store.read(GPIDS_FILE, None)
    if gpids is None:  # first start: seed the owner from .env so he can sign up as admin
        gpids = {}
        g, n = os.environ.get("BOOTSTRAP_ADMIN_GPID", ""), os.environ.get("BOOTSTRAP_ADMIN_NAME", "")
        if g and n:
            gpids[g] = {"name": n, "schedule_name": schedule_name(n), "role": "admin", "added": _now()}
        with store.locked("gpids"):
            store.write(GPIDS_FILE, gpids)
    return gpids


def _public(u):
    return {k: u.get(k) for k in ("uid", "gpid", "name", "email", "role", "status", "created", "last_login")}


def _locked_out(u):
    recent = [t for t in u.get("fails", []) if time.time() - t < FAIL_WINDOW]
    return len(recent) >= FAIL_LIMIT


def _record_fail(uid):
    with store.locked("users"):
        users = load_users()
        if uid in users:
            users[uid]["fails"] = [t for t in users[uid].get("fails", []) if time.time() - t < FAIL_WINDOW] + [time.time()]
            store.write(USERS_FILE, users)


# --- the gate: every request except sign-in/sign-up needs an approved, signed-in person ---------

@app.before_request
def check_session():
    if request.path in OPEN_PATHS:
        return None
    uid = session.get("uid")
    user = load_users().get(uid) if uid else None
    if user and user.get("status") == "active" and user.get("sv") == session.get("sv"):
        request.role = user["role"]
        request.user_name = user["name"]
        request.uid = uid
        return None
    session.pop("uid", None)
    if request.path in ("/", "/schedule", "/admin") or request.path.endswith(".html"):
        return redirect("/login")
    return jsonify({"error": "Not signed in"}), 401


@app.route("/login")
def login_page():
    return send_from_directory(".", "login.html")


@app.route("/signup")
def signup_page():
    return send_from_directory(".", "signup.html")


@app.route("/admin")
def admin_page():
    if request.role not in ("admin", "scheduler"):
        return redirect("/")
    return send_from_directory(".", "admin.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/me")
def me():
    users = load_users()
    u = users[request.uid]
    g = load_gpids().get(u["gpid"], {})
    return jsonify({"name": u["name"], "role": u["role"], "schedule_name": g.get("schedule_name", schedule_name(u["name"]))})


# --- sign-in: password, then the 6-digit code from the authenticator app ----------------------

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if _ip_blocked():
        return jsonify({"error": "Too many wrong tries from here. Wait 15 minutes."}), 429
    data = request.get_json(force=True)
    email = str(data.get("email", "")).strip().lower()
    users = load_users()
    user = next((u for u in users.values() if u["email"] == email), None)
    if user and _locked_out(user):
        return jsonify({"error": "This account is locked for 15 minutes after too many wrong tries."}), 429
    if not user or not check_password_hash(user["pw"], str(data.get("password", ""))):
        _ip_fail()
        if user:
            _record_fail(user["uid"])
        return jsonify({"error": "Wrong email or password."}), 403
    if user["status"] == "enrolling":
        return jsonify({"error": "Sign-up wasn't finished. Go to Sign up and start again with your GPID."}), 403
    if user["status"] == "pending":
        return jsonify({"error": "Your account is waiting for approval."}), 403
    if user["status"] != "active":
        return jsonify({"error": "This account is switched off. Ask the admin."}), 403
    session.clear()
    session["pre_uid"], session["pre_t"] = user["uid"], time.time()
    return jsonify({"next": "code"})


@app.route("/api/auth/code", methods=["POST"])
def auth_code():
    uid, started = session.get("pre_uid"), session.get("pre_t", 0)
    if not uid or time.time() - started > STEP2_SECONDS:
        session.clear()
        return jsonify({"error": "Took too long. Start again."}), 403
    code = re.sub(r"\D", "", str(request.get_json(force=True).get("code", "")))
    with store.locked("users"):
        users = load_users()
        user = users.get(uid)
        if not user or _locked_out(user):
            return jsonify({"error": "This account is locked for 15 minutes after too many wrong tries."}), 429
        totp = pyotp.TOTP(user["totp"])
        step = int(time.time() // 30)
        ok = len(code) == 6 and totp.verify(code, valid_window=1) and step > user.get("last_step", 0)
        if not ok:
            user["fails"] = [t for t in user.get("fails", []) if time.time() - t < FAIL_WINDOW] + [time.time()]
            store.write(USERS_FILE, users)
            _ip_fail()
            return jsonify({"error": "Wrong code. Use the newest code in your authenticator app."}), 403
        user["last_step"], user["fails"], user["last_login"] = step, [], _now()
        store.write(USERS_FILE, users)
    session.clear()
    session.permanent = True
    session["uid"], session["sv"] = uid, user["sv"]
    return jsonify({"next": "/"})


# --- sign-up: GPID on the list + email + password, then link an authenticator app --------------

@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    if _ip_blocked():
        return jsonify({"error": "Too many tries from here. Wait 15 minutes."}), 429
    data = request.get_json(force=True)
    gpid = re.sub(r"\D", "", str(data.get("gpid", "")))
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    entry = load_gpids().get(gpid)
    if not entry:
        _ip_fail()
        return jsonify({"error": "That GPID isn't on the list. Ask Brandon or the scheduler to add it."}), 403
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"error": "Enter a real email address."}), 400
    if len(password) < MIN_PASSWORD:
        return jsonify({"error": f"Password must be at least {MIN_PASSWORD} characters."}), 400
    with store.locked("users"):
        users = load_users()
        for u in list(users.values()):
            if u["gpid"] == gpid and u["status"] != "enrolling":
                return jsonify({"error": "There's already an account for this GPID. If it isn't yours, tell Brandon."}), 409
            if u["email"] == email and u["gpid"] != gpid:
                return jsonify({"error": "That email is already used by another account."}), 409
        for uid in [k for k, u in users.items() if u["gpid"] == gpid]:  # restart an unfinished sign-up
            del users[uid]
        uid = secrets.token_hex(8)
        users[uid] = {"uid": uid, "gpid": gpid, "name": entry["name"], "email": email,
                      "pw": generate_password_hash(password), "totp": pyotp.random_base32(),
                      "role": entry.get("role", "member"), "status": "enrolling", "sv": 1,
                      "created": _now(), "last_login": None, "fails": []}
        store.write(USERS_FILE, users)
    session.clear()
    session["enroll_uid"], session["enroll_t"] = uid, time.time()
    uri = pyotp.TOTP(users[uid]["totp"]).provisioning_uri(name=email, issuer_name=ISSUER)
    svg = io.BytesIO()
    qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2).save(svg)
    return jsonify({"name": entry["name"], "qr_svg": svg.getvalue().decode(), "secret": users[uid]["totp"]})


@app.route("/api/auth/enroll", methods=["POST"])
def auth_enroll():
    uid = session.get("enroll_uid")
    if not uid or time.time() - session.get("enroll_t", 0) > 30 * 60:
        return jsonify({"error": "Sign-up timed out. Start again."}), 403
    code = re.sub(r"\D", "", str(request.get_json(force=True).get("code", "")))
    with store.locked("users"):
        users = load_users()
        user = users.get(uid)
        if not user or user["status"] != "enrolling":
            return jsonify({"error": "Sign-up timed out. Start again."}), 403
        if not (len(code) == 6 and pyotp.TOTP(user["totp"]).verify(code, valid_window=1)):
            _ip_fail()
            return jsonify({"error": "That code didn't match. Check the app shows 'Case Tracker' and try the newest code."}), 403
        # The owner (seeded admin GPID) is approved straight away; everyone else waits for approval.
        user["status"] = "active" if user["role"] == "admin" else "pending"
        user["last_step"] = int(time.time() // 30)
        store.write(USERS_FILE, users)
    session.clear()
    return jsonify({"status": user["status"]})


# --- admin: approve people, manage the GPID list -----------------------------------------------

def _staff():
    return request.role in ("admin", "scheduler")


@app.route("/api/admin/state")
def admin_state():
    if not _staff():
        return jsonify({"error": "Not allowed"}), 403
    users = sorted((_public(u) for u in load_users().values()), key=lambda u: (u["status"] != "pending", u["name"]))
    gpids = load_gpids()
    registered = {u["gpid"] for u in users}
    return jsonify({"me": request.role, "users": users,
                    "gpids": [{"gpid": g, **v, "has_account": g in registered} for g, v in sorted(gpids.items(), key=lambda x: x[1]["name"])]})


@app.route("/api/admin/user", methods=["POST"])
def admin_user():
    if not _staff():
        return jsonify({"error": "Not allowed"}), 403
    data = request.get_json(force=True)
    action, uid = data.get("action"), data.get("uid")
    with store.locked("users"):
        users = load_users()
        u = users.get(uid)
        if not u:
            return jsonify({"error": "No such account"}), 404
        if u["role"] == "admin" and request.role != "admin":
            return jsonify({"error": "Only an admin can change an admin"}), 403
        if uid == request.uid and action in ("disable", "deny", "role"):
            return jsonify({"error": "You can't do that to your own account"}), 400
        if action == "approve":
            u["status"] = "active"
        elif action == "deny":
            del users[uid]
        elif action == "disable":
            u["status"], u["sv"] = "disabled", u["sv"] + 1
        elif action == "enable":
            u["status"] = "active"
        elif action == "reset":  # lost phone / forgot password: they sign up again with their GPID
            del users[uid]
        elif action == "role":
            role = data.get("role")
            if role not in ROLES or (role == "admin" and request.role != "admin"):
                return jsonify({"error": "Not allowed"}), 403
            u["role"], u["sv"] = role, u["sv"] + 1
        else:
            return jsonify({"error": "Unknown action"}), 400
        store.write(USERS_FILE, users)
    return jsonify({"status": "done"})


@app.route("/api/admin/gpids", methods=["POST"])
def admin_gpids():
    """Add lines like '12345678, Jane Doe' (one person per line), or remove one GPID."""
    if not _staff():
        return jsonify({"error": "Not allowed"}), 403
    data = request.get_json(force=True)
    with store.locked("gpids"):
        gpids = load_gpids()
        if data.get("remove"):
            g = re.sub(r"\D", "", str(data["remove"]))
            if gpids.get(g, {}).get("role") == "admin":
                return jsonify({"error": "Can't remove the admin's GPID"}), 400
            gpids.pop(g, None)
            store.write(GPIDS_FILE, gpids)
            return jsonify({"status": "removed"})
        added, bad = 0, []
        for line in str(data.get("text", "")).splitlines():
            m = re.match(r"\s*(\d{6,10})\s*[,;\t ]\s*(.+?)\s*$", line)
            if not line.strip():
                continue
            if not m:
                bad.append(line.strip())
                continue
            g, name = m.group(1), " ".join(w.capitalize() if w.isupper() or w.islower() else w for w in m.group(2).split())
            if g not in gpids:
                gpids[g] = {"name": name, "schedule_name": schedule_name(name), "role": "member", "added": _now()}
                added += 1
        store.write(GPIDS_FILE, gpids)
    return jsonify({"status": f"Added {added}", "bad_lines": bad})
