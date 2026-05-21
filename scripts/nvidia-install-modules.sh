#!/usr/bin/env bash
# Install patched NVIDIA kernel modules: backup originals, MOK-sign, install,
# depmod, hold apt packages. Version-agnostic: auto-detects nvidia-N-open
# from the source tree's NVIDIA_VERSION.
set -euo pipefail

RESEARCH=$HOME/g2-linux-research
SRC=${SRC:-$RESEARCH/src/open-gpu-kernel-modules}
KVER=${KVER:-$(uname -r)}
SIGN_TOOL=/usr/src/linux-headers-$KVER/scripts/sign-file
MOK_KEY=/var/lib/shim-signed/mok/MOK.priv
MOK_CERT=/var/lib/shim-signed/mok/MOK.der

[ -d "$SRC" ] || { echo "ERR: $SRC not found" >&2; exit 1; }
[ -f "$SIGN_TOOL" ] || { echo "ERR: $SIGN_TOOL missing" >&2; exit 1; }
[ -f "$MOK_KEY" ]   || { echo "ERR: $MOK_KEY missing" >&2; exit 1; }
[ -f "$MOK_CERT" ]  || { echo "ERR: $MOK_CERT missing" >&2; exit 1; }

NV_VERSION=$(awk -F= '/^NVIDIA_VERSION/ {gsub(/ /,"",$2); print $2}' "$SRC/version.mk")
NV_MAJOR=$(echo "$NV_VERSION" | cut -d. -f1)
TARGET=/lib/modules/$KVER/kernel/nvidia-${NV_MAJOR}-open
[ -d "$TARGET" ] || {
    echo "ERR: $TARGET missing — does Ubuntu ship linux-modules-nvidia-${NV_MAJOR}-open?" >&2
    echo "If on a brand-new NVIDIA series, you may need to bootstrap via 'sudo apt install linux-modules-nvidia-${NV_MAJOR}-open-${KVER}' first." >&2
    exit 1
}

BACKUP=$RESEARCH/module-backup-${NV_VERSION}-$(date +%Y%m%d-%H%M%S)

echo "NVIDIA version:  $NV_VERSION (series $NV_MAJOR)"
echo "Kernel:          $KVER"
echo "Target dir:      $TARGET"
echo "Backup dir:      $BACKUP"
echo

# Verify all .ko files present
for m in nvidia nvidia-modeset nvidia-drm nvidia-uvm; do
    [ -f "$SRC/kernel-open/$m.ko" ] || {
        echo "ERR: $SRC/kernel-open/$m.ko missing (run nvidia-build-modules.sh)" >&2
        exit 1
    }
done

# Backup originals
mkdir -p "$BACKUP"
sudo cp -a "$TARGET"/*.ko "$BACKUP"/
echo "Backed up to $BACKUP/:"
ls -la "$BACKUP" | tail -n +2

# Sign + install
for m in nvidia nvidia-modeset nvidia-drm nvidia-uvm; do
    ko="$SRC/kernel-open/$m.ko"
    sudo "$SIGN_TOOL" sha256 "$MOK_KEY" "$MOK_CERT" "$ko"
    sudo install -m 0644 "$ko" "$TARGET/$m.ko"
    echo "  installed: $m.ko (srcversion=$(modinfo -F srcversion "$ko"))"
done

sudo depmod -a "$KVER"

# Hold apt packages so updates don't blow these away
echo
echo "Holding apt packages for series $NV_MAJOR..."
for pkg in \
    "linux-modules-nvidia-${NV_MAJOR}-open-${KVER}" \
    "linux-modules-nvidia-${NV_MAJOR}-open-generic" \
    "linux-modules-nvidia-${NV_MAJOR}-open-generic-hwe-26.04" \
    "nvidia-kernel-source-${NV_MAJOR}-open" \
    "nvidia-driver-${NV_MAJOR}-open"; do
    dpkg -l "$pkg" >/dev/null 2>&1 && sudo apt-mark hold "$pkg" 2>/dev/null && \
        echo "  hold: $pkg" || true
done

cat <<EOF

DONE. Reboot to activate the new modules.

After reboot, verify:
  modinfo -F srcversion nvidia_drm
    -> $(modinfo -F srcversion "$SRC/kernel-open/nvidia-drm.ko")
  modinfo -F srcversion nvidia_modeset
    -> $(modinfo -F srcversion "$SRC/kernel-open/nvidia-modeset.ko")

Rollback:
  scripts/nvidia-rollback.sh $BACKUP
EOF
