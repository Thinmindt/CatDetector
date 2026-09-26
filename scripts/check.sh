#!/bin/bash
# The gates every commit must pass, in the order that fails fastest.
set -euo pipefail
cd "$(dirname "$0")/.."

bash scripts/check_private.sh
# shellcheck disable=SC2046
uv run shellcheck $(git ls-files '*.sh')
git ls-files -z | xargs -0 uv run codespell
uv run ruff check .
uv run ruff format --check .
uv run mypy .
npm run --silent lint
uv run pytest "$@"
