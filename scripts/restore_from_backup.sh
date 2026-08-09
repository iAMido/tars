#!/usr/bin/env bash
# restore_from_backup.sh — pull the latest snapshot from B2 to /tmp/tars-restore.
#
# Verifies the backup pipeline is actually restorable. Run this monthly as a
# disaster-recovery dry run, or for real if the box dies.
#
# Does NOT touch the live /home/tars/.tars/ data. The restored files land in
# /tmp/tars-restore for inspection.

set -euo pipefail

ENV_FILE="/etc/tars/restic.env"
TARGET="${TARGET:-/tmp/tars-restore}"
SNAPSHOT="${SNAPSHOT:-latest}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Run scripts/setup_backups.sh first." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source <(sudo cat "$ENV_FILE")
set +a

mkdir -p "$TARGET"
echo "Restoring snapshot=$SNAPSHOT -> $TARGET"
restic restore "$SNAPSHOT" --target "$TARGET"

echo
echo "Restored files:"
find "$TARGET" -type f -printf '  %s\t%p\n' | sort

# If the restored snapshot dir exists, verify the latest .db opens cleanly.
RESTORED_DB="$(find "$TARGET" -name 'tars-*.db' -type f -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2-)"
if [[ -n "$RESTORED_DB" ]]; then
    echo
    echo "Validating restored DB: $RESTORED_DB"
    sqlite3 "$RESTORED_DB" "PRAGMA integrity_check;" | head -1
    note_count=$(sqlite3 "$RESTORED_DB" "SELECT COUNT(*) FROM notes;")
    msg_count=$(sqlite3 "$RESTORED_DB" "SELECT COUNT(*) FROM messages;")
    fu_count=$(sqlite3 "$RESTORED_DB" "SELECT COUNT(*) FROM follow_ups;")
    echo "  notes:      $note_count"
    echo "  messages:   $msg_count"
    echo "  follow_ups: $fu_count"
fi

echo
echo "Restore done. Inspect $TARGET; then rm -rf $TARGET when satisfied."
