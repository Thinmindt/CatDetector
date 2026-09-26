#!/bin/bash
# This project's private terms for check_private.sh, one per line: the cats' names in .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    sed -n 's/^CAT_NAMES=//p' .env | tr -d "\"'" | tr ',' '\n'
fi
