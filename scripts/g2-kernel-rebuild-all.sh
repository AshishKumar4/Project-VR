#!/usr/bin/env bash
# Top-level orchestrator: build+sign the custom BORE kernel, then rebuild the
# g2-patched NVIDIA open modules against it. Mirrors g2-nvidia-rebuild-all.sh.
#
#   scripts/g2-kernel-rebuild-all.sh              # build current tkg config + nvidia
#   scripts/g2-kernel-rebuild-all.sh --bump 7.1   # bump tkg _version, then build
#
# The stock kernel and its NVIDIA install are never touched -> always-available fallback.
set -euo pipefail
DIR=$(dirname "$(readlink -f "$0")")
RESEARCH=$HOME/g2-linux-research
TKG=$RESEARCH/src/linux-tkg
CFG=~/.config/frogminer/linux-tkg.cfg

if [ "${1:-}" = "--bump" ]; then
    NEWV="${2:-}"; [ -n "$NEWV" ] || { echo "Usage: $0 --bump <x.y[-latest]>" >&2; exit 2; }
    echo "== bump tkg _version -> $NEWV =="
    sed -i -E "s/^_version=.*/_version=\"$NEWV\"/" "$CFG"
fi

echo "== Step 1/3: build + sign kernel =="
"$DIR/kernel-build.sh"
KVER=$(cat "$TKG/DEBS/.last-kver")

echo "== Step 2/3: build NVIDIA modules for $KVER =="
KVER="$KVER" "$DIR/nvidia-build-modules.sh"

echo "== Step 3/3: sign + install NVIDIA modules into $KVER =="
KVER="$KVER" "$DIR/nvidia-install-modules.sh"

cat <<EOF

ALL DONE — kernel $KVER + g2-NVIDIA built & signed.
  1. Reboot, pick "$KVER" under GRUB > "Advanced options for Ubuntu".
  2. Verify: uname -r ; nvidia-smi ; <test G2 VR> ; cat /sys/kernel/sched_ext/state
  3. Happy? Promote to default:  scripts/kernel-set-default.sh $KVER
  4. Trouble? Reboot, pick the stock kernel (untouched). NVIDIA rollback: scripts/nvidia-rollback.sh <backup>
EOF
