#!/usr/bin/env bash
# Rebase our local NVIDIA patches onto a new upstream tag.
# Usage: nvidia-rebase-onto.sh <new-tag>          (e.g. 600.05.06)
#        nvidia-rebase-onto.sh --dry-run <tag>    (show plan, don't touch)
set -euo pipefail

SRC=${SRC:-$HOME/g2-linux-research/src/open-gpu-kernel-modules}
cd "$SRC"

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; shift; fi
NEW_TAG=${1:-}
[ -n "$NEW_TAG" ] || { echo "Usage: $0 [--dry-run] <new-tag>" >&2; exit 2; }

# Verify the tag exists
git rev-parse "$NEW_TAG" >/dev/null 2>&1 || {
    echo "ERR: tag $NEW_TAG not found; did you fetch?" >&2
    echo "Try: scripts/nvidia-fetch-upstream.sh" >&2
    exit 1
}

# Working tree must be clean
[ -z "$(git status --porcelain)" ] || {
    echo "ERR: working tree has uncommitted changes." >&2
    git status --short
    exit 1
}

# Current branch + base tag (NVIDIA upstream has no "main"; use closest tag)
CUR_BRANCH=$(git rev-parse --abbrev-ref HEAD)
BASE_TAG=$(git describe --tags --abbrev=0 HEAD)

if [ "$BASE_TAG" = "$NEW_TAG" ]; then
    echo "Already on $NEW_TAG; nothing to do."
    exit 0
fi

# Cherry-pickable local commits
N_COMMITS=$(git rev-list --count "$BASE_TAG"..HEAD)
echo "Plan:"
echo "  branch:      $CUR_BRANCH"
echo "  current base: $BASE_TAG (HEAD has $N_COMMITS local commits on top)"
echo "  target tag:  $NEW_TAG"
echo
echo "  Local commits to replay (oldest first):"
git log --oneline --reverse "$BASE_TAG"..HEAD | sed 's/^/    /'

NEW_BRANCH="${CUR_BRANCH%-on-*}-on-${NEW_TAG}"
[ "$NEW_BRANCH" = "$CUR_BRANCH" ] && NEW_BRANCH="g2-patches-on-${NEW_TAG}"

echo
echo "  New branch will be: $NEW_BRANCH"
SAFETY_TAG="rebase-safety-${BASE_TAG}-to-${NEW_TAG}-$(date +%Y%m%d-%H%M%S)"
echo "  Pre-rebase safety tag: $SAFETY_TAG -> $(git rev-parse --short HEAD)"

[ "$DRY_RUN" = 1 ] && { echo; echo "(dry-run: stopping)"; exit 0; }

# Pre-rebase safety
git tag "$SAFETY_TAG"
echo "Created safety tag $SAFETY_TAG"

# New branch off the new tag, then cherry-pick our commits
git switch -c "$NEW_BRANCH" "$NEW_TAG"
echo "Switched to new branch $NEW_BRANCH at $NEW_TAG."
echo
echo "Cherry-picking $N_COMMITS commits..."
if ! git cherry-pick "$BASE_TAG..$CUR_BRANCH"; then
    cat <<EOF >&2

CHERRY-PICK CONFLICT. Resolve, then:
    git add <resolved files>
    git cherry-pick --continue
Or abort:
    git cherry-pick --abort
    git switch $CUR_BRANCH
    git branch -D $NEW_BRANCH
    git tag -d $SAFETY_TAG

Standalone patches for reference:
    \$RESEARCH/patches/{20,21,22,...}.diff
EOF
    exit 3
fi

echo
echo "OK. Branch $NEW_BRANCH has $(git rev-list --count "$NEW_TAG"..HEAD) commits on top of $NEW_TAG:"
git log --oneline "$NEW_TAG"..HEAD
echo
echo "Next: scripts/nvidia-build-modules.sh"
echo "Rollback: git switch $CUR_BRANCH (and git tag -d $SAFETY_TAG when done)"
