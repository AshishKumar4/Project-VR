#!/usr/bin/env bash
# Top-level orchestrator: fetch upstream NVIDIA, optionally bump version,
# build, sign, install. Safe to run idempotently.
#
# Modes:
#   $0                  - rebuild current branch against running kernel
#   $0 --check          - fetch upstream + show what's available; no changes
#   $0 --bump TAG       - rebase patches onto TAG, then build + install
set -euo pipefail

DIR=$(dirname "$(readlink -f "$0")")
RESEARCH=$HOME/g2-linux-research
SRC=$RESEARCH/src/open-gpu-kernel-modules

MODE=rebuild
TAG=""
case "${1:-}" in
    --check) MODE=check ;;
    --bump)  MODE=bump; TAG="${2:-}"; [ -n "$TAG" ] || { echo "Usage: $0 --bump <tag>" >&2; exit 2; } ;;
    "")      MODE=rebuild ;;
    *)       echo "Usage: $0 [--check | --bump <tag>]" >&2; exit 2 ;;
esac

echo "== Step 1: fetch upstream =="
"$DIR/nvidia-fetch-upstream.sh"

if [ "$MODE" = check ]; then
    echo
    echo "(--check: stopping here)"
    exit 0
fi

if [ "$MODE" = bump ]; then
    echo
    echo "== Step 2: rebase onto $TAG =="
    "$DIR/nvidia-rebase-onto.sh" "$TAG"
fi

echo
echo "== Step $([ "$MODE" = bump ] && echo 3 || echo 2): build modules =="
"$DIR/nvidia-build-modules.sh"

echo
echo "== Step $([ "$MODE" = bump ] && echo 4 || echo 3): install (sign + MOK + apt-hold) =="
"$DIR/nvidia-install-modules.sh"

echo
echo "All done. Reboot to load the new modules."
