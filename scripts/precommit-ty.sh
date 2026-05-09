#!/usr/bin/env bash
# Run ty against the staged Python files only — never the whole project.
# Pre-commit feeds us only the paths in the index, so we just hand them
# straight to ty.
set -euo pipefail

if [ $# -eq 0 ]; then
    exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root/air-stacker-gui"

args=()
for f in "$@"; do
    args+=("${f#air-stacker-gui/}")
done

exec uv run ty check "${args[@]}"
