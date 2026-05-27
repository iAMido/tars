#!/usr/bin/env bash
# first_deploy.sh — one-shot initial deploy of TARS to a freshly-bootstrapped VPS.
#
# Run as the `tars` user on the VPS (NOT as root). The bootstrap_vps.sh script
# already created the user, dirs, deps, Tailscale, and firewall. This script
# layers the application code on top.
#
# Expects /home/tars/.tars/config.toml to already exist (copy from your dev
# machine via scp before running this).
#
# Usage:
#   ssh tars-vps
#   # On the VPS:
#   /home/tars/tars/scripts/first_deploy.sh <git-repo-url>

set -euo pipefail

REPO_URL="${1:-}"
if [[ -z "$REPO_URL" ]]; then
  echo "usage: $0 <git-repo-url>" >&2
  echo "  e.g. $0 git@github.com:idomosseri/tars.git" >&2
  exit 1
fi

if [[ "$(whoami)" != "tars" ]]; then
  echo "Must run as the 'tars' user, not $(whoami)." >&2
  exit 1
fi

cd "$HOME"

# 1. Clone (or update if it already exists).
if [[ -d "$HOME/tars/.git" ]]; then
  echo "==> repo already present, pulling"
  cd "$HOME/tars"
  git fetch --all --prune
  git reset --hard origin/main
else
  echo "==> cloning $REPO_URL to ~/tars"
  # tars/ already exists from bootstrap; clone into it.
  if [[ -d "$HOME/tars" ]] && [[ -z "$(ls -A "$HOME/tars" 2>/dev/null)" ]]; then
    rmdir "$HOME/tars"
  fi
  git clone "$REPO_URL" "$HOME/tars"
  cd "$HOME/tars"
fi

# 2. Verify config is in place.
if [[ ! -f "$HOME/.tars/config.toml" ]]; then
  echo "ERROR: ~/.tars/config.toml not found." >&2
  echo "Copy it from your dev machine first:" >&2
  echo "  scp \$HOME/.tars/config.toml tars-vps:/home/tars/.tars/config.toml" >&2
  echo "Then edit it (paths must be /home/tars/.tars/... not C:/...)" >&2
  exit 2
fi
chmod 600 "$HOME/.tars/config.toml"

# 3. uv sync (production deps only).
echo "==> uv sync --frozen"
"$HOME/.local/bin/uv" sync --frozen

# 4. Phase 1 smoke: migrate + verify schema.
echo "==> Phase 1 smoke test"
"$HOME/.local/bin/uv" run python -m tars

# 5. Install the systemd unit.
echo "==> installing systemd unit (requires sudo)"
sudo cp "$HOME/tars/systemd/tars.service" /etc/systemd/system/tars.service
sudo systemctl daemon-reload
sudo systemctl enable tars
sudo systemctl restart tars

# 6. Wait a few seconds, then show status.
sleep 4
sudo systemctl --no-pager status tars | head -30

echo
echo "✓ first_deploy done. Tail logs with:"
echo "    tail -f ~/logs/tars.log ~/logs/tars.err"
echo "Or via journald:"
echo "    sudo journalctl -u tars -f"
