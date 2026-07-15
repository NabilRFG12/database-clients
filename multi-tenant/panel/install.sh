#!/usr/bin/env bash
#
# install.sh — install the admin panel as a systemd service.
#
#   bash panel/install.sh
#
# The panel binds to 127.0.0.1:8800 (never public). Access it via SSH tunnel:
#
#   ssh -L 8800:127.0.0.1:8800 root@<vps>
#   open http://localhost:8800     (user: admin, password printed below)

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

PANEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$PANEL_DIR")"
CONFIG="$ROOT/config.env"
[ -f "$CONFIG" ] || { echo "error: $CONFIG missing — run 'tenantctl init' first" >&2; exit 1; }

if ! grep -q '^PANEL_PASSWORD=' "$CONFIG"; then
    echo "PANEL_PASSWORD=$(openssl rand -hex 16)" >> "$CONFIG"
fi
PASSWORD=$(sed -n 's/^PANEL_PASSWORD=//p' "$CONFIG" | head -1)

cat > /etc/systemd/system/supabase-mt-panel.service <<EOF
[Unit]
Description=Supabase multi-tenant admin panel
After=docker.service
Wants=docker.service

[Service]
ExecStart=/usr/bin/python3 $PANEL_DIR/panel.py
WorkingDirectory=$ROOT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now supabase-mt-panel

cat <<EOF

============================================================
 Admin panel installed and running (localhost only).

 From your own machine:

   ssh -L 8800:127.0.0.1:8800 root@<this-server>

 then open:  http://localhost:8800
   user:     admin
   password: $PASSWORD

 Manage:  systemctl status|restart supabase-mt-panel
 Logs:    journalctl -u supabase-mt-panel -f
============================================================
EOF
