#!/bin/bash
# Fail if a private term appears in a file git tracks or would add, or in the author, committer
# or message of a commit no remote has yet.
# The terms are the names in .env's CAT_NAMES and each line of .private-terms (# starts a
# comment). Both files are gitignored, so a clone without them has nothing to check.
set -euo pipefail
cd "$(dirname "$0")/.."

terms=$(mktemp)
trap 'rm -f "$terms"' EXIT
if [ -f .env ]; then
    sed -n 's/^CAT_NAMES=//p' .env | tr -d "\"'" | tr ',' '\n' >>"$terms"
fi
if [ -f .private-terms ]; then
    grep -v '^[[:space:]]*#' .private-terms >>"$terms" || true
fi
sed -i 's/^[[:space:]]*//; s/[[:space:]]*$//; /^$/d' "$terms"

if [ ! -s "$terms" ]; then
    echo "private terms: none configured, skipped"
    exit 0
fi
found=0
if git grep --untracked -I -n -i -w -F -f "$terms" -- .; then
    found=1
fi
if git log HEAD --not --remotes --format='%h %an <%ae> committed by %cn <%ce>%n%B' |
    grep -i -w -F -f "$terms"; then
    echo "(in an unpushed commit; see git log HEAD --not --remotes)"
    found=1
fi
if [ "$found" -eq 1 ]; then
    echo "private terms found above; they belong in CLAUDE.local.md or .env (CLAUDE.md, Repo hygiene)" >&2
    exit 1
fi
echo "private terms: none in tracked files or unpushed commits"
