#!/usr/bin/env bash
# bootstrap_vps.sh — runs as root, one-shot setup for a fresh Ubuntu 26.04 Hetzner CPX22.
# Idempotent: safe to re-run.
#
# What this does (Phase A — does NOT lock you out yet):
#   1. apt update + upgrade
#   2. install: curl git sqlite3 ffmpeg restic rsync ufw fail2ban chrony python3 build tools
#   3. create non-root `tars` user with sudo
#   4. copy SSH key from root -> tars so you can log in as tars
#   5. create directory tree (~/.tars, ~/logs, ~/vault, ~/backups, ~/snapshots, ~/tars)
#   6. install uv (as tars)
#   7. install Tailscale (not yet `up`-ed; you do that interactively in Step 7c)
#   8. set timezone Asia/Jerusalem
#   9. UFW: deny incoming, allow ssh + tailscale0
#  10. enable fail2ban + chrony
#  11. logrotate config for tars logs
#
# What this does NOT do yet (Phase B, done after we verify tars@ SSH works):
#   - disable root SSH login
#   - disable password authentication
# Those go in scripts/harden_ssh.sh, run only after you confirm `ssh tars-vps-user` works.

set -euo pipefail
trap 'echo "ERROR on line $LINENO. Aborting bootstrap." >&2' ERR

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "=== 1/11 apt update + upgrade ==="
apt-get update -y
apt-get upgrade -y

echo "=== 2/11 install packages ==="
apt-get install -y \
    curl wget git ca-certificates gnupg lsb-release \
    ufw fail2ban chrony \
    sqlite3 ffmpeg restic rsync \
    build-essential pkg-config \
    python3 python3-venv python3-pip python3-dev \
    htop tmux jq unzip vim less

echo "=== 3/11 create tars user (if not exists) ==="
if ! id -u tars >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" tars
  usermod -aG sudo tars
fi
# Allow tars to restart the tars service without sudo password (used by deploy.sh)
cat > /etc/sudoers.d/tars <<'SUDO'
tars ALL=(ALL) NOPASSWD: /bin/systemctl restart tars, /bin/systemctl status tars, /bin/systemctl stop tars, /bin/systemctl start tars
SUDO
chmod 440 /etc/sudoers.d/tars

echo "=== 4/11 copy SSH authorized_keys from root to tars ==="
mkdir -p /home/tars/.ssh
cp /root/.ssh/authorized_keys /home/tars/.ssh/authorized_keys
chown -R tars:tars /home/tars/.ssh
chmod 700 /home/tars/.ssh
chmod 600 /home/tars/.ssh/authorized_keys

echo "=== 5/11 directory tree ==="
sudo -u tars mkdir -p \
  /home/tars/.tars \
  /home/tars/logs \
  /home/tars/vault \
  /home/tars/backups \
  /home/tars/snapshots \
  /home/tars/tars
chmod 700 /home/tars/.tars

echo "=== 6/11 install uv as tars user ==="
sudo -u tars bash -c 'if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi'

echo "=== 7/11 install Tailscale ==="
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "=== 8/11 set timezone Asia/Jerusalem ==="
timedatectl set-timezone Asia/Jerusalem

echo "=== 9/11 UFW firewall ==="
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow in on tailscale0 comment 'Tailnet'
ufw --force enable
ufw status verbose

echo "=== 10/11 enable fail2ban + chrony ==="
systemctl enable --now fail2ban
systemctl enable --now chrony

echo "=== 11/11 logrotate config for tars logs ==="
cat > /etc/logrotate.d/tars <<'LR'
/home/tars/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 tars tars
}
LR

echo ""
echo "=============================================================="
echo "  Bootstrap Phase A complete."
echo ""
echo "  Next: from a NEW PowerShell window on your Windows box,"
echo "  verify you can SSH as the 'tars' user (NOT root):"
echo ""
echo "      ssh -i \$HOME\\.ssh\\tars_ed25519 tars@$(hostname -I | awk '{print $1}')"
echo ""
echo "  Should land at 'tars@tars-prod:~\$' (note: \$ not #)."
echo ""
echo "  Once that works, run scripts/harden_ssh.sh as root to lock"
echo "  out root SSH and disable password auth."
echo "=============================================================="
