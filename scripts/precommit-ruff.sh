#!/usr/bin/env bash
# Auto-fix what ruff can on staged Python files, re-stage the fixes,
# then re-check. Fails the commit only if errors remain after the fix
# pass — i.e. don't bother the user about anything ruff already cleaned
# up by itself.
set -euo pipefail

if [ $# -eq 0 ]; then
    exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root/air-stacker-gui"

# Pre-commit passes paths relative to repo root; strip the project prefix.
args=()
for f in "$@"; do
    args+=("${f#air-stacker-gui/}")
done

# Fix what we can. Ignore exit code; we just want fixes applied to disk.
uv run ruff check --fix --exit-zero "${args[@]}"

# Re-stage anything ruff modified (paths relative to repo root for git).
cd "$repo_root"
git add -- "$@"

# Re-check; this is the gate.
cd "$repo_root/air-stacker-gui"
exec uv run ruff check "${args[@]}"
