#!/usr/bin/env bash
# setup_backups.sh — one-shot installer for Phase 9b backups.
#
# Run as the `tars` user on the VPS. Idempotent.
#
# Prereqs:
#   - restic installed (bootstrap_vps.sh already did this)
#   - You have B2 keyID + applicationKey + bucket name ready
#   - You have already decided on a RESTIC_PASSWORD (or will generate one)
#
# What this does:
#   1. Prompts for B2 credentials interactively (never written to history)
#   2. Generates a strong RESTIC_PASSWORD if you don't supply one
#   3. Writes /etc/tars/restic.env (root-owned, 0440) — uses sudo
#   4. Initializes the restic repo on B2
#   5. Installs the systemd service + timer units
#   6. Enables and starts the timer
#   7. Runs one backup immediately to validate the whole pipeline
#
# Restore later via scripts/restore_from_backup.sh.

set -euo pipefail

if [[ "$(whoami)" != "tars" ]]; then
    echo "Must run as the 'tars' user. Got: $(whoami)" >&2
    exit 1
fi

REPO_ROOT="${REPO_ROOT:-/home/tars/tars}"
ENV_FILE="/etc/tars/restic.env"

echo "=== TARS backup setup ==="
echo

# --- 1. Gather B2 creds (no echo of secrets) ---
read -p "B2 bucket name (e.g. tars-prod-backups-abcd): " B2_BUCKET
read -p "B2 keyID: " B2_ACCOUNT_ID
read -s -p "B2 applicationKey (will not echo): " B2_ACCOUNT_KEY
echo
echo

# Path inside the bucket — keeps room for other backups later.
B2_PATH="${B2_PATH:-tars}"

# --- 2. RESTIC_PASSWORD ---
echo "RESTIC_PASSWORD encrypts the entire repo. WITHOUT IT, BACKUPS ARE UNRECOVERABLE."
read -s -p "Restic password (leave blank to auto-generate strong 32-char): " RESTIC_PASSWORD
echo
if [[ -z "$RESTIC_PASSWORD" ]]; then
    RESTIC_PASSWORD="$(openssl rand -base64 24)"
    echo "Generated RESTIC_PASSWORD: $RESTIC_PASSWORD"
    echo "  ^^^ SAVE THIS IN YOUR PASSWORD MANAGER NOW. Press Enter when done."
    read -r
fi

# --- 3. Write /etc/tars/restic.env ---
echo "Writing $ENV_FILE (requires sudo)..."
sudo mkdir -p "$(dirname "$ENV_FILE")"
sudo tee "$ENV_FILE" > /dev/null <<EOF
# /etc/tars/restic.env — secrets for tars-backup.service. 0440 root:tars.
RESTIC_REPOSITORY=b2:${B2_BUCKET}:${B2_PATH}/
RESTIC_PASSWORD=${RESTIC_PASSWORD}
B2_ACCOUNT_ID=${B2_ACCOUNT_ID}
B2_ACCOUNT_KEY=${B2_ACCOUNT_KEY}
EOF
sudo chown root:tars "$ENV_FILE"
sudo chmod 0440 "$ENV_FILE"

# --- 4. Init the restic repo (idempotent) ---
echo "Initializing restic repo (skipped if already exists)..."
set +e
RESTIC_REPOSITORY="b2:${B2_BUCKET}:${B2_PATH}/" \
RESTIC_PASSWORD="$RESTIC_PASSWORD" \
B2_ACCOUNT_ID="$B2_ACCOUNT_ID" \
B2_ACCOUNT_KEY="$B2_ACCOUNT_KEY" \
restic snapshots 2>/dev/null >/dev/null
existing=$?
set -e
if [[ $existing -ne 0 ]]; then
    RESTIC_REPOSITORY="b2:${B2_BUCKET}:${B2_PATH}/" \
    RESTIC_PASSWORD="$RESTIC_PASSWORD" \
    B2_ACCOUNT_ID="$B2_ACCOUNT_ID" \
    B2_ACCOUNT_KEY="$B2_ACCOUNT_KEY" \
    restic init
else
    echo "  repo already initialized — reusing"
fi

# --- 5. Install systemd units ---
echo "Installing systemd units..."
sudo cp "$REPO_ROOT/systemd/tars-backup.service" /etc/systemd/system/
sudo cp "$REPO_ROOT/systemd/tars-backup.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tars-backup.timer
sudo systemctl start tars-backup.timer

# --- 6. Run one backup right now to validate ---
echo
echo "Triggering an immediate backup as smoke test..."
chmod +x "$REPO_ROOT/scripts/tars-backup.sh"
sudo systemctl start tars-backup.service
sleep 3
sudo systemctl status tars-backup.service --no-pager | head -20

echo
echo "Recent log:"
tail -20 /home/tars/logs/backup.log 2>/dev/null || echo "(no log yet)"

echo
echo "=== setup_backups.sh done ==="
echo "Verify next scheduled run:"
echo "    systemctl list-timers tars-backup.timer"
echo
echo "List snapshots in B2:"
echo "    sudo systemctl start tars-backup.service && sudo journalctl -u tars-backup.service -n 30"
echo "    OR: sudo -E env-file $ENV_FILE restic snapshots"
