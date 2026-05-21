#!/usr/bin/env bash
# Build patched NVIDIA kernel modules from source, against the running kernel.
# Version-agnostic: works on any nvidia-N-open branch.
set -euo pipefail

SRC=${SRC:-$HOME/g2-linux-research/src/open-gpu-kernel-modules}
KVER=${KVER:-$(uname -r)}
SYSSRC=/lib/modules/$KVER/build

[ -d "$SRC" ] || { echo "ERR: $SRC not found" >&2; exit 1; }
[ -d "$SYSSRC" ] || { echo "ERR: kernel headers missing: $SYSSRC" >&2; exit 1; }
[ -f "$SRC/version.mk" ] || { echo "ERR: $SRC/version.mk missing" >&2; exit 1; }

NV_VERSION=$(awk -F= '/^NVIDIA_VERSION/ {gsub(/ /,"",$2); print $2}' "$SRC/version.mk")
echo "Building NVIDIA $NV_VERSION modules for kernel $KVER..."

cd "$SRC"
make -j"$(nproc)" modules SYSSRC="$SYSSRC" 2>&1 | tail -10

echo
echo "Produced:"
for m in nvidia nvidia-modeset nvidia-drm nvidia-uvm; do
    ko="$SRC/kernel-open/$m.ko"
    if [ -f "$ko" ]; then
        printf "  %-20s %8d bytes  srcversion=%s\n" "$m.ko" "$(stat -c%s "$ko")" \
            "$(modinfo -F srcversion "$ko" 2>/dev/null || echo \?)"
    else
        echo "  MISSING: $ko"
    fi
done

echo
echo "Next: scripts/nvidia-install-modules.sh"
