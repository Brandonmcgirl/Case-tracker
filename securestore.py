"""Encrypted JSON files for anything about people (accounts, GPID list, schedules).

Each file is one Fernet token (AES-128-CBC + HMAC-SHA256) made with DATA_KEY, which lives only in the
hub's root-only /etc/northbridge/secrets/case-tracker.env. Without that key the files are unreadable;
losing the key means re-entering the data (people sign up again, schedules are re-loaded from photos).
"""
import contextlib
import fcntl
import json
import os

from cryptography.fernet import Fernet

DATA_DIR = "data"


def _fernet():
    key = os.environ.get("DATA_KEY")
    if not key:
        raise RuntimeError("DATA_KEY is not set (hub secrets file)")
    return Fernet(key)


def read(path, default):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return default
    with open(path, "rb") as f:
        return json.loads(_fernet().decrypt(f.read()))


def write(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    token = _fernet().encrypt(json.dumps(obj, separators=(",", ":")).encode())
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(name):
    """Serialise read-modify-write across the gunicorn workers."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f".lock-{name}"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
