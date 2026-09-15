#!/bin/bash
# Installs the cat detector as a systemd service and (re)starts it. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo" >&2
    exit 1
fi

# The camera takes one process at a time, so a copy started by hand must exit first.
if ! systemctl is-active --quiet catdetector.service; then
    for pid in $(pgrep -f '^/home/butler/sw/CatDetector/\.venv/bin/python3? main\.py' || true); do
        echo "stopping the detector started by hand (pid $pid)"
        kill -TERM "$pid"
        timeout 60 tail --pid="$pid" -f /dev/null || true
    done
fi

install -m 0644 catdetector.service /etc/systemd/system/catdetector.service
systemctl daemon-reload
systemctl enable catdetector.service
systemctl restart catdetector.service

systemctl --no-pager --lines=5 status catdetector.service
