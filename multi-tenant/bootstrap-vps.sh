#!/usr/bin/env bash
#
# bootstrap-vps.sh — turn a fresh Ubuntu/Debian VPS into a multi-tenant
# Supabase host in one command. Hardens the server FIRST, then installs
# Docker, then initializes the platform (shared Caddy + mt-proxy network).
#
# Run as root from inside the cloned repository:
#
#   git clone --depth 1 -b <branch> <repo-url>
#   cd database-clients/multi-tenant
#   bash bootstrap-vps.sh --base-domain api.example.com --email you@example.com
#
# Optional:
#   --ssh-pubkey "ssh-ed25519 AAAA..."   install the key for root and DISABLE
#                                        password login (only with a key!)
#   --skip-harden                        skip firewall/fail2ban/updates setup
#
# What it does, in order:
#   1. Firewall (ufw): deny inbound except SSH/80/443
#   2. fail2ban + unattended security updates
#   3. (optional) SSH key install + disable password authentication
#   4. Docker Engine + Compose (via get.docker.com)
#   5. tenantctl init  (shared Caddy, mt-proxy network, config.env)
#   6. Nightly backup cron for all tenants (03:00, via tenantctl backup-all)

set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash bootstrap-vps.sh ...)"
command -v apt-get >/dev/null || die "this script supports Ubuntu/Debian (apt) only"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -x "$SCRIPT_DIR/tenantctl" ] || die "tenantctl not found next to this script — run from the cloned repo's multi-tenant/ directory"

BASE_DOMAIN="" EMAIL="" SSH_PUBKEY="" SKIP_HARDEN=false
while [ $# -gt 0 ]; do
    case $1 in
        --base-domain) BASE_DOMAIN=$2; shift 2 ;;
        --email) EMAIL=$2; shift 2 ;;
        --ssh-pubkey) SSH_PUBKEY=$2; shift 2 ;;
        --skip-harden) SKIP_HARDEN=true; shift ;;
        *) die "unknown option: $1" ;;
    esac
done
[ -n "$BASE_DOMAIN" ] || die "--base-domain is required (e.g. api.example.com)"
[ -n "$EMAIL" ] || die "--email is required (for Let's Encrypt)"

export DEBIAN_FRONTEND=noninteractive

echo "==> [1/6] Installing base packages"
apt-get update -q
apt-get install -yq ca-certificates curl git openssl ufw fail2ban unattended-upgrades

if ! $SKIP_HARDEN; then
    echo "==> [2/6] Firewall: allow SSH/80/443, deny everything else inbound"
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow OpenSSH
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable

    echo "==> [3/6] fail2ban + automatic security updates"
    systemctl enable --now fail2ban
    dpkg-reconfigure -f noninteractive unattended-upgrades

    if [ -n "$SSH_PUBKEY" ]; then
        echo "==> SSH: installing key and disabling password login"
        mkdir -p /root/.ssh && chmod 700 /root/.ssh
        grep -qF "$SSH_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null \
            || echo "$SSH_PUBKEY" >> /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
        cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
EOF
        systemctl restart ssh || systemctl restart sshd
        echo "    Password login is now DISABLED — keep your key safe."
    else
        echo "==> SSH: no --ssh-pubkey given; password login stays ENABLED."
        echo "    Re-run later with --ssh-pubkey to lock it down, or do it manually."
    fi
else
    echo "==> [2-3/6] Hardening skipped (--skip-harden)"
fi

echo "==> [4/6] Docker Engine + Compose"
if ! command -v docker >/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker
docker compose version >/dev/null || die "docker compose plugin missing after install"

echo "==> [5/6] Initializing the multi-tenant platform"
"$SCRIPT_DIR/tenantctl" init --base-domain "$BASE_DOMAIN" --email "$EMAIL"

echo "==> [6/6] Nightly backups (03:00) for all tenants"
cat > /etc/cron.d/supabase-mt-backups <<EOF
0 3 * * * root $SCRIPT_DIR/tenantctl backup-all >> /var/log/supabase-mt-backups.log 2>&1
EOF
chmod 644 /etc/cron.d/supabase-mt-backups

SERVER_IP=$(curl -fsS -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
cat <<EOF

============================================================
 Bootstrap complete.

 1. DNS — add this record at your DNS provider (once):

      *.$BASE_DOMAIN    A    $SERVER_IP

 2. Create your first tenant:

      cd $SCRIPT_DIR
      ./tenantctl create myclient --ram 2g --cpus 2 --services storage

 3. Smoke test (after DNS propagates, ~1-5 min):

      curl https://myclient.$BASE_DOMAIN/auth/v1/health

    Expected: {"version":"...","name":"GoTrue",...}

 Backups: nightly 03:00 -> $SCRIPT_DIR/backups/
          (copy them off this server — that part is on you!)
============================================================
EOF
