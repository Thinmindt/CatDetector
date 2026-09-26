#!/bin/bash
# Fail if a private term appears in a file git tracks or would add, or in the author, committer
# or message of a commit no remote has yet.
# The terms are each line of .private-terms (gitignored; # starts a comment) and whatever
# scripts/private_terms.sh prints, if the project has one. With no terms, as on CI, it passes.
set -euo pipefail
cd "$(dirname "$0")/.."

terms=$(mktemp)
trap 'rm -f "$terms"' EXIT
{
    if [ -f .private-terms ]; then
        grep -v '^[[:space:]]*#' .private-terms || true
    fi
    if [ -f scripts/private_terms.sh ]; then
        bash scripts/private_terms.sh
    fi
} | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; /^$/d' >"$terms"

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
    echo "private terms found above; move them to a gitignored file" >&2
    exit 1
fi
echo "private terms: none in tracked files or unpushed commits"
