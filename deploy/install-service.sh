#!/bin/bash
# Installs the cat detector as a systemd service and (re)starts it. Safe to re-run.
# The unit is built from catdetector.service.in for this checkout: its owner runs
# the service, and the share named by NETWORK_SHARE_DIR in .env (default
# /mnt/nas) becomes the mount unit it waits for.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo" >&2
    exit 1
fi

checkout=$(cd .. && pwd -P)
user=$(stat -c %U "$checkout")
share=$(grep -s '^NETWORK_SHARE_DIR=' "$checkout/.env" | tail -n1 | cut -d= -f2- | tr -d "\"'" || true)
share_mount=$(systemd-escape --path --suffix=mount "${share:-/mnt/nas}")

# The camera takes one process at a time, so a copy started by hand must exit first.
if ! systemctl is-active --quiet catdetector.service; then
    for pid in $(pgrep -f "^$checkout/\.venv/bin/python3? main\.py" || true); do
        echo "stopping the detector started by hand (pid $pid)"
        kill -TERM "$pid"
        timeout 60 tail --pid="$pid" -f /dev/null || true
    done
fi

sed -e "s|@USER@|$user|g" -e "s|@CHECKOUT@|$checkout|g" -e "s|@SHARE_MOUNT@|$share_mount|g" \
    catdetector.service.in > /etc/systemd/system/catdetector.service
chmod 0644 /etc/systemd/system/catdetector.service
systemctl daemon-reload
systemctl enable catdetector.service
systemctl restart catdetector.service

systemctl --no-pager --lines=5 status catdetector.service
