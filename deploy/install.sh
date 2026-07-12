#!/bin/bash
# Installs pxe-map-api on a RHEL 9.4 PXE VM: system user, /var/www/pxe-map
# with correct ownership and SELinux labeling, app + venv under /opt, and
# the systemd unit. Air-gapped: pip installs from a local package mirror,
# not PyPI - set LOCAL_MIRROR below before running.
set -euo pipefail

SERVICE_USER="pxe-map-api"
APP_DIR="/opt/pxe-map-api"
PXE_MAP_DIR="/var/www/pxe-map"
CONFIG_DIR="/etc/pxe-map-api"
LOCAL_MIRROR="/path/to/local/pip-mirror"   # <-- set this to your air-gapped mirror
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$PXE_MAP_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$PXE_MAP_DIR"
chmod 0755 "$PXE_MAP_DIR"
find "$PXE_MAP_DIR" -maxdepth 1 -type f -exec chmod 0644 {} \;

# One-time persistent SELinux label so nginx (httpd_sys_content_t reader)
# can serve files this service writes. The per-write restorecon in
# app/mapping.py relies on this fcontext rule already being registered.
semanage fcontext -a -t httpd_sys_content_t "${PXE_MAP_DIR}(/.*)?"
restorecon -Rv "$PXE_MAP_DIR"

mkdir -p "$APP_DIR"
cp -r "$REPO_DIR/app" "$REPO_DIR/requirements.txt" "$APP_DIR/"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --no-index --find-links="$LOCAL_MIRROR" \
    -r "$APP_DIR/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/env" ]]; then
    cp "$REPO_DIR/deploy/env.example" "$CONFIG_DIR/env"
fi
chmod 0640 "$CONFIG_DIR/env"
chown "root:$SERVICE_USER" "$CONFIG_DIR/env"

cp "$REPO_DIR/deploy/pxe-map-api.service" /etc/systemd/system/pxe-map-api.service
systemctl daemon-reload
systemctl enable pxe-map-api

cat <<EOF

Install complete. Before starting the service:

1. Edit $CONFIG_DIR/env and set PXE_MAP_TOKEN to a strong random value, e.g.:
     PXE_MAP_TOKEN=\$(openssl rand -hex 32)

2. Check PXE_MAP_BIND in $CONFIG_DIR/env - it defaults to 127.0.0.1:8000.
   Only widen it if the central BMH-creation endpoint cannot reach this
   host via localhost/tunnel; never bind the interface booting hosts use.

3. Start it:
     systemctl start pxe-map-api
     systemctl status pxe-map-api
EOF
