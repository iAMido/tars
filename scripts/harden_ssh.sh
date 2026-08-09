#!/usr/bin/env bash
# harden_ssh.sh — Phase B of VPS setup.
# Run as root ONLY AFTER you have verified you can SSH in as the `tars` user.
# This disables root SSH login and password authentication. If you skip the
# verification step and tars@ doesn't work, you will lock yourself out.
#
# Recovery if locked out: Hetzner Cloud console -> server -> Console (browser
# tty) -> log in as root via emergency password from Hetzner -> re-enable SSH.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root." >&2
  exit 1
fi

# Backup current sshd config
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.backup-$(date +%Y%m%d-%H%M%S)

# Drop a hardening file in sshd_config.d (preferred over editing main file)
cat > /etc/ssh/sshd_config.d/99-tars-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
AllowUsers tars
EOF

# Test config before reloading
sshd -t

# Reload (not restart — keeps your current session alive)
systemctl reload ssh || systemctl reload sshd

echo "SSH hardened. Open a fresh PowerShell window and confirm:"
echo "  ssh tars-vps    (should still work)"
echo "  ssh root@<ip>   (should now be refused)"
echo ""
echo "If anything is broken, your CURRENT session is still alive — fix it from here."
echo "Backup of original config: /etc/ssh/sshd_config.backup-*"
