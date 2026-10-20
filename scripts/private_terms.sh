#!/bin/bash
# This project's private terms for check_private.sh, one per line: the cats' names in .env, and
# the capture heartbeat URL there, without its query, with each path segment or query value that
# looks like a token (8 or more characters, at least one digit). Plain words such as a check's
# slug are left out, since they would match unrelated files.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    sed -n 's/^CAT_NAMES=//p' .env | tr -d "\"'" | tr ',' '\n'
    url=$(sed -n 's/^CAPTURE_HEARTBEAT_URL=//p' .env | tr -d "\"'")
    if [ -n "$url" ]; then
        url=${url%%#*}
        echo "${url%%\?*}"
        echo "$url" | sed 's#^[a-zA-Z]*://[^/]*##' | tr '/?&=' '\n' \
            | grep -E '^.{8,}$' | grep '[0-9]' || true
    fi
fi
