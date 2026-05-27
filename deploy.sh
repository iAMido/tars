#!/usr/bin/env bash
# deploy.sh — push current main to GitHub, pull on VPS, sync deps, restart.
#
# Idempotent. Safe to run anytime.
#
# Prereqs:
#   - 'origin' git remote configured
#   - tars-vps SSH alias points at the box (via ~/.ssh/config or tailnet MagicDNS)
#   - First-time setup already done via scripts/first_deploy.sh
#
# Usage from Windows: run in Git Bash, WSL, or PowerShell (bash via Git for Windows).

set -euo pipefail

VPS_ALIAS="${VPS_ALIAS:-tars-vps}"
BRANCH="${BRANCH:-main}"

echo "==> Pushing $BRANCH to origin"
git push origin "$BRANCH"

echo "==> Pulling + syncing + restarting on $VPS_ALIAS"
ssh "$VPS_ALIAS" bash -se <<'REMOTE'
set -euo pipefail
cd ~/tars
git fetch --all --prune
git reset --hard origin/main
~/.local/bin/uv sync --frozen
sudo systemctl restart tars
sleep 3
systemctl status tars --no-pager | head -25
echo
echo "Tail of stderr:"
tail -n 20 ~/logs/tars.err 2>/dev/null || echo "(no errors yet)"
REMOTE

echo
echo "✓ deploy complete"
