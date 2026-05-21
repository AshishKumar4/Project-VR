#!/usr/bin/env bash
# Fetch NVIDIA open-gpu-kernel-modules upstream; list tags newer than HEAD-base.
set -euo pipefail

SRC=${SRC:-$HOME/g2-linux-research/src/open-gpu-kernel-modules}
[ -d "$SRC/.git" ] || { echo "ERR: not a git repo: $SRC" >&2; exit 1; }
cd "$SRC"

echo "Fetching tags from $(git remote get-url origin)..."
git fetch --tags --prune origin 2>&1 | tail -5

# NVIDIA upstream uses per-version branches (515, 520, ..., 580) and tags, no "main".
# Find the most recent tag in HEAD's ancestry as our base.
BASE_TAG=$(git describe --tags --abbrev=0 HEAD 2>/dev/null || echo "unknown")

# Major-line of our current base, e.g. 595 from 595.71.05
MAJOR=$(echo "$BASE_TAG" | cut -d. -f1)

echo
echo "Our branch:        $(git rev-parse --abbrev-ref HEAD)"
echo "Our local commits: $(git rev-list --count "$BASE_TAG"..HEAD 2>/dev/null || echo \?)"
echo "Base tag:          $BASE_TAG"

echo
echo "=== All upstream tags newer than $BASE_TAG (semantic sort) ==="
# Append base to the sorted list so awk can find the cut point, then print
# everything strictly after it.
all_tags=$(git tag | sort -V)
if [ "$BASE_TAG" != "unknown" ]; then
    printf '%s\n%s\n' "$all_tags" "$BASE_TAG" | sort -V | awk -v b="$BASE_TAG" '
        $0 == b { seen = 1; next }
        seen && $0 != b { print }'
else
    echo "$all_tags" | tail -10
fi

echo
echo "=== Same-major-line ($MAJOR.x) point releases ==="
echo "$all_tags" | grep "^$MAJOR\." | tail -10

echo
echo "=== Next-major-line candidates (if any) ==="
next_major=$((MAJOR + 5))   # NVIDIA cadence: 580 -> 585 -> 590 -> 595 -> 600
for try in $((MAJOR + 5)) $((MAJOR + 10)) 600 605 610; do
    found=$(echo "$all_tags" | grep "^$try\." | tail -3 || true)
    [ -n "$found" ] && echo "$try.x: $found"
done

echo
echo "Note: Vulkan beta branches (VK*) often lag production; skip unless"
echo "explicitly testing a new Vulkan extension."
