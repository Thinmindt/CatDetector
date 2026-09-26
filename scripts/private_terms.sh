#!/bin/bash
# This project's private terms for check_private.sh, one per line: the cats' names in .env,
# and the capture heartbeat URL there with its last path segment, the part that is the secret.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    sed -n 's/^CAT_NAMES=//p' .env | tr -d "\"'" | tr ',' '\n'
    url=$(sed -n 's/^CAPTURE_HEARTBEAT_URL=//p' .env | tr -d "\"'")
    if [ -n "$url" ]; then
        echo "$url"
        basename "$url"
    fi
fi
