#!/bin/bash
# The gates every commit must pass, in the order that fails fastest.
set -euo pipefail
cd "$(dirname "$0")/.."

files() { git ls-files -z --cached --others --exclude-standard "$@"; }

bash scripts/check_private.sh
files '*.sh' | xargs -0 uv run shellcheck
files | xargs -0 uv run codespell
uv run ruff check .
uv run ruff format --check .
uv run mypy .
npm run --silent lint
uv run pytest "$@"
