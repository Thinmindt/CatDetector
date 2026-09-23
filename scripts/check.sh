#!/bin/bash
# The five gates every commit must pass, in the order that fails fastest.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff check .
uv run ruff format --check .
uv run mypy .
npx --yes eslint@10 src/static
uv run pytest "$@"
