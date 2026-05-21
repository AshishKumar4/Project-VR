#!/usr/bin/env bash
# Restore NVIDIA kernel modules from a backup directory created by
# nvidia-install-modules.sh.
# Usage: nvidia-rollback.sh <backup-dir>
#        nvidia-rollback.sh --latest          (use most recent backup)
set -euo pipefail

RESEARCH=$HOME/g2-linux-research
KVER=${KVER:-$(uname -r)}

if [ "${1:-}" = "--latest" ]; then
    BACKUP=$(ls -dt "$RESEARCH"/module-backup-* 2>/dev/null | head -1)
    [ -n "$BACKUP" ] || { echo "ERR: no module-backup-* found" >&2; exit 1; }
elif [ $# -eq 1 ]; then
    BACKUP=$1
else
    echo "Usage: $0 <backup-dir>" >&2
    echo "       $0 --latest" >&2
    echo
    echo "Available backups:" >&2
    ls -dt "$RESEARCH"/module-backup-* 2>/dev/null | head -10 >&2 || true
    exit 2
fi
[ -d "$BACKUP" ] || { echo "ERR: $BACKUP not a directory" >&2; exit 1; }

# Detect series from a backed-up nvidia-drm.ko if possible; else infer from target dir
DRM_KO=$(ls "$BACKUP"/nvidia-drm.ko 2>/dev/null || echo "")
if [ -n "$DRM_KO" ]; then
    NV_VERSION=$(modinfo -F version "$DRM_KO")
    NV_MAJOR=$(echo "$NV_VERSION" | cut -d. -f1)
else
    NV_MAJOR=$(ls -d /lib/modules/"$KVER"/kernel/nvidia-*-open 2>/dev/null | head -1 | grep -oE 'nvidia-[0-9]+-open' | grep -oE '[0-9]+')
    [ -n "$NV_MAJOR" ] || { echo "ERR: can't detect nvidia series" >&2; exit 1; }
    NV_VERSION=unknown
fi
TARGET=/lib/modules/$KVER/kernel/nvidia-${NV_MAJOR}-open

echo "Restoring:"
echo "  from:    $BACKUP"
echo "  series:  $NV_MAJOR (version $NV_VERSION)"
echo "  to:      $TARGET"
echo

for m in nvidia nvidia-modeset nvidia-drm nvidia-uvm; do
    src="$BACKUP/$m.ko"
    dst="$TARGET/$m.ko"
    if [ -f "$src" ]; then
        sudo install -m 0644 "$src" "$dst"
        echo "  restored: $m.ko"
    else
        echo "  WARN: $src missing in backup; leaving $dst alone"
    fi
done

sudo depmod -a "$KVER"

cat <<EOF

Restored. Reboot to activate the rolled-back modules.

If you want to revert to a clean apt-shipped state instead, also:
  sudo apt-mark unhold linux-modules-nvidia-${NV_MAJOR}-open-${KVER}
  sudo apt --reinstall install linux-modules-nvidia-${NV_MAJOR}-open-${KVER}
EOF
