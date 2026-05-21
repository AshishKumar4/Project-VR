#!/usr/bin/env bash
# Install the latest patched (+g2~) mutter runtime debs and hold them.
# Run as root (g2ctl invokes the root-owned copy via sudo). Idempotent.
set -euo pipefail
RESEARCH=${RESEARCH:-/home/mrwhite0racle/g2-linux-research}
DEB_DIR=$RESEARCH/src/mutter-patch
PKGS=(mutter libmutter-18-0 mutter-common mutter-common-bin gir1.2-mutter-18)

debs=()
for p in "${PKGS[@]}"; do
    f=$(ls -1 "$DEB_DIR/${p}_"*+g2~*_amd64.deb "$DEB_DIR/${p}_"*+g2~*_all.deb 2>/dev/null \
        | sort -V | tail -1 || true)
    [ -n "$f" ] && debs+=("$f")
done
[ ${#debs[@]} -gt 0 ] || { echo "ERR: no +g2~ mutter debs in $DEB_DIR" >&2; exit 1; }

apt-get install -y --allow-downgrades "${debs[@]}"
apt-mark hold "${PKGS[@]}"
echo "installed + held ${#debs[@]} patched mutter debs"
