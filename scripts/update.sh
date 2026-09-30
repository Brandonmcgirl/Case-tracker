#!/usr/bin/env bash
#
# update.sh — NorthBridge ops contract for this project (the standard every project follows,
# 2026-09-30). Same verbs as northbridge-os/scripts/update.sh, so the NorthBridge OS
# Operations page and the Jarbo dashboard drive every project identically:
#
#   deploy              snapshot -> pull -> install -> migrate -> build -> restart
#   snapshot [tag]      create a tagged recovery point (code tarball + DB dump + .env copy)
#   list                print snapshot ids, one per line, newest first (machine-readable)
#   restore <tag|id>    roll back code + DB to a snapshot (auto-snapshots first)
#   delete  <tag|id>    delete a snapshot
#
# Golden rule: ALWAYS snapshot before pulling/deploying.
# Per-project knobs are the PROJECT SETTINGS block below (filled in at install time).
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

# ── PROJECT SETTINGS ──────────────────────────────────────────────────────────
APP_NAME="${APP_NAME:-case-tracker}"            # systemd unit(s) to restart, space-separated
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
INSTALL_CMD="${INSTALL_CMD:-[ -f requirements.txt ] && venv/bin/pip install -q -r requirements.txt}"   # e.g. "npm ci --include=dev"  |  "venv/bin/pip install -r requirements.txt"  |  ""
MIGRATE_CMD="${MIGRATE_CMD:-}"   # e.g. "npx prisma migrate deploy"  |  ""
BUILD_CMD="${BUILD_CMD:-}"         # e.g. "npm run build"  |  ""
DATA_DIRS="${DATA_DIRS:-}"         # git-ignored runtime data to include in snapshots, space-separated ("" = none)
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$APP_DIR/.snapshots}"
# ─────────────────────────────────────────────────────────────────────────────

log() { printf "\033[1;34m[update]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[error]\033[0m %s\n" "$*" >&2; }
sanitize() { echo "${1//[^A-Za-z0-9._-]/-}"; }
pg_url() { echo "${DATABASE_URL%%\?*}"; }
load_env() { if [ -f .env ]; then set -a; . ./.env; set +a; fi; }

restart_app() {
  local u
  for u in $APP_NAME; do
    sudo -n systemctl restart "$u" || { err "Restart of $u denied: needs the NOPASSWD sudoers entry (see /etc/sudoers.d/nb-ops)."; return 1; }
  done
}

snapshot() {
  local tag dest stamp
  tag="$(sanitize "${1:-snapshot}")"
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="$SNAPSHOT_DIR/${stamp}_${tag}"
  mkdir -p "$dest"
  log "Snapshotting → $dest"
  load_env
  if [ -n "${DATABASE_URL:-}" ] && [[ "${DATABASE_URL:-}" == postgres* ]] && command -v pg_dump >/dev/null 2>&1; then
    pg_dump "$(pg_url)" > "$dest/database.sql" && log "  • database.sql"
  else
    log "  • no Postgres DATABASE_URL — code${DATA_DIRS:+ + data} only"
  fi
  tar --exclude=node_modules --exclude=.next --exclude=.git --exclude=.snapshots --exclude=venv --exclude=.venv --exclude=__pycache__ \
    -czf "$dest/code.tar.gz" . && log "  • code.tar.gz"
  if [ -n "$DATA_DIRS" ]; then
    tar -czf "$dest/data.tar.gz" --ignore-failed-read $DATA_DIRS 2>/dev/null && log "  • data.tar.gz ($DATA_DIRS)" || true
  fi
  cp -f .env "$dest/.env.bak" 2>/dev/null || true
  git rev-parse HEAD > "$dest/commit.txt" 2>/dev/null || true
  log "Snapshot complete: ${stamp}_${tag}"
  echo "${stamp}_${tag}"
}

list_snapshots() { [ -d "$SNAPSHOT_DIR" ] || return 0; ls -1 "$SNAPSHOT_DIR" 2>/dev/null | grep -E "^[0-9]{8}-[0-9]{6}_" | sort -r; }

resolve_snapshot() {
  local ref id
  ref="$(sanitize "$1")"
  if [ -d "$SNAPSHOT_DIR/$ref" ]; then echo "$ref"; return 0; fi
  id="$(list_snapshots | grep -E "_${ref}$" | head -1 || true)"
  [ -n "$id" ] && echo "$id"
}

deploy() {
  log "Deploying $APP_NAME from origin/$DEPLOY_BRANCH"
  log "Taking a pre-deploy snapshot (mandatory recovery point)…"
  snapshot "pre-deploy" >/dev/null
  log "Pulling latest…"
  git pull --ff-only origin "$DEPLOY_BRANCH"
  [ -n "$INSTALL_CMD" ] && { log "Installing dependencies…"; bash -c "$INSTALL_CMD"; }
  load_env
  [ -n "$MIGRATE_CMD" ] && { log "Applying migrations…"; bash -c "$MIGRATE_CMD"; }
  [ -n "$BUILD_CMD" ] && { log "Building…"; bash -c "$BUILD_CMD"; }
  log "Restarting…"; restart_app
  log "Deploy complete."
}

restore() {
  local ref id src
  ref="${1:-}"
  [ -n "$ref" ] || { err "Usage: $0 restore <tag|id>  (ids: $0 list)"; exit 1; }
  id="$(resolve_snapshot "$ref")"
  [ -n "$id" ] || { err "No snapshot matches '$ref'."; exit 1; }
  src="$SNAPSHOT_DIR/$id"
  log "Auto-snapshotting current state before restore…"
  snapshot "pre-restore" >/dev/null
  log "Restoring code from $src/code.tar.gz"
  tar -xzf "$src/code.tar.gz" -C "$APP_DIR"
  [ -f "$src/data.tar.gz" ] && { log "Restoring data folders"; tar -xzf "$src/data.tar.gz" -C "$APP_DIR"; }
  load_env
  if [ -f "$src/database.sql" ] && [ -n "${DATABASE_URL:-}" ]; then
    log "Restoring database (resetting public schema first for a clean load)…"
    psql "$(pg_url)" -v ON_ERROR_STOP=1 -c 'DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;'
    psql "$(pg_url)" -v ON_ERROR_STOP=1 -f "$src/database.sql"
  fi
  [ -n "$INSTALL_CMD" ] && bash -c "$INSTALL_CMD"
  [ -n "$BUILD_CMD" ] && bash -c "$BUILD_CMD"
  restart_app
  log "Restore complete from $id."
}

delete_snapshot() {
  local ref id
  ref="${1:-}"; [ -n "$ref" ] || { err "Usage: $0 delete <tag|id>"; exit 1; }
  id="$(resolve_snapshot "$ref")"; [ -n "$id" ] || { err "No snapshot matches '$ref'."; exit 1; }
  rm -rf "${SNAPSHOT_DIR:?}/$id"; log "Deleted snapshot $id."
}

case "${1:-}" in
  deploy) deploy ;;
  snapshot) snapshot "${2:-snapshot}" ;;
  list) list_snapshots ;;
  restore) restore "${2:-}" ;;
  delete) delete_snapshot "${2:-}" ;;
  *) echo "Usage: $0 {deploy|snapshot [tag]|list|restore <tag|id>|delete <tag|id>}"; exit 1 ;;
esac
