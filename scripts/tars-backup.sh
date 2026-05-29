#!/usr/bin/env bash
# tars-backup.sh — daily backup script.
#
# Runs as the `tars` user via systemd timer. Steps:
#   1. SQLite online .backup of tars.db -> ~/.tars/snapshots/tars-<TS>.db
#      (uses SQLite Online Backup API; safe to run while the bot is live)
#   2. restic backup -> B2 (and optional second destination if RESTIC_REPO_2 set)
#      Includes: the .db snapshot, vault, config.toml, google_token.json,
#                client_secret.json. Excludes: the WAL/SHM files (not portable).
#   3. forget+prune: keep 14 daily, 8 weekly, 12 monthly
#   4. local snapshot dir pruned to 14 days
#   5. On failure: exit non-zero so systemd-emails-on-failure (or the optional
#      Telegram hook) lights up.
#
# Required env (from /etc/tars/restic.env):
#   RESTIC_REPOSITORY      e.g. b2:tars-prod-backups-XXXX:tars/
#   RESTIC_PASSWORD        the repo encryption password (NEVER lose this)
#   B2_ACCOUNT_ID          B2 keyID
#   B2_ACCOUNT_KEY         B2 applicationKey
# Optional:
#   RESTIC_REPOSITORY_2    second restic repo url (Storage Box etc.) — runs same backup if set
#   RESTIC_PASSWORD_2      password for second repo (defaults to RESTIC_PASSWORD)

set -euo pipefail
shopt -s nullglob

# --- config ---
TARS_HOME="${TARS_HOME:-/home/tars}"
DB_PATH="${DB_PATH:-$TARS_HOME/.tars/tars.db}"
SNAP_DIR="${SNAP_DIR:-$TARS_HOME/.tars/snapshots}"
LOG_FILE="${LOG_FILE:-$TARS_HOME/logs/backup.log}"

# --- helpers ---
log() {
    # systemd already captures stderr to backup.err and stdout to backup.log
    # (configured in tars-backup.service via StandardOutput/StandardError).
    # Don't duplicate-write here — that hits permission-denied because the
    # files are pre-created by systemd as root.
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') backup: $*" >&2
}
die() { log "ERROR: $*"; exit 1; }

mkdir -p "$SNAP_DIR"
log "=== tars-backup start ==="

# --- 1. SQLite online backup ---
TS=$(date -u '+%Y%m%dT%H%M%SZ')
SNAP_FILE="$SNAP_DIR/tars-$TS.db"
log "snapshotting $DB_PATH -> $SNAP_FILE"
sqlite3 "$DB_PATH" ".backup '$SNAP_FILE'" || die "sqlite3 .backup failed"

# Verify the snapshot opens.
sqlite3 "$SNAP_FILE" "PRAGMA integrity_check;" | head -1 | grep -q "^ok$" \
    || die "snapshot integrity_check failed"
SNAP_BYTES=$(stat -c %s "$SNAP_FILE")
log "snapshot ok ($SNAP_BYTES bytes)"

# --- 2. restic backup ---
declare -a INCLUDES=(
    "$SNAP_FILE"
    "$TARS_HOME/.tars/config.toml"
    "$TARS_HOME/.tars/google_token.json"
    "$TARS_HOME/.tars/client_secret.json"
)
# vault may or may not exist depending on phase
if [ -d "$TARS_HOME/vault" ]; then
    INCLUDES+=("$TARS_HOME/vault")
fi

backup_to_repo() {
    local repo="$1" pw="$2"
    [ -z "$repo" ] && return 0
    log "restic -> $repo"
    RESTIC_REPOSITORY="$repo" RESTIC_PASSWORD="$pw" \
        restic backup --tag "daily" --tag "host:$(hostname)" \
        --quiet \
        "${INCLUDES[@]}" \
        || die "restic backup to $repo failed"

    log "restic forget+prune on $repo"
    RESTIC_REPOSITORY="$repo" RESTIC_PASSWORD="$pw" \
        restic forget \
        --keep-daily 14 --keep-weekly 8 --keep-monthly 12 \
        --prune --quiet \
        || die "restic forget on $repo failed"
}

# Required B2 destination.
[ -z "${RESTIC_REPOSITORY:-}" ] && die "RESTIC_REPOSITORY not set; check /etc/tars/restic.env"
backup_to_repo "$RESTIC_REPOSITORY" "$RESTIC_PASSWORD"

# Optional second destination (Storage Box etc.)
if [ -n "${RESTIC_REPOSITORY_2:-}" ]; then
    backup_to_repo "$RESTIC_REPOSITORY_2" "${RESTIC_PASSWORD_2:-$RESTIC_PASSWORD}"
fi

# --- 3. local snapshot retention: keep 14 days ---
log "pruning local snapshots older than 14 days"
find "$SNAP_DIR" -name 'tars-*.db' -mtime +14 -delete 2>/dev/null || true

log "=== tars-backup ok ==="
