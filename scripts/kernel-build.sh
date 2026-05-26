#!/usr/bin/env bash
# Build + install the custom BORE kernel via linux-tkg, then sbsign the image with
# the kernel-MOK so it boots under Secure Boot. Non-interactive (driven by
# ~/.config/frogminer/linux-tkg.cfg). Run as your normal user (uses sudo internally).
#
#   scripts/kernel-build.sh
#
# GRUB default is left on your STOCK kernel (fallback). After verifying the new
# kernel, promote it with scripts/kernel-set-default.sh <KVER>.
set -euo pipefail
RESEARCH=$HOME/g2-linux-research
TKG=${TKG:-$RESEARCH/src/linux-tkg}
MOK_KEY=$RESEARCH/infra/mok-kernel/MOK-kernel.key
MOK_CERT=$RESEARCH/infra/mok-kernel/MOK-kernel.crt

[ -d "$TKG" ] || { echo "ERR: linux-tkg not at $TKG (git clone Frogging-Family/linux-tkg)" >&2; exit 1; }
[ -f ~/.config/frogminer/linux-tkg.cfg ] || { echo "ERR: ~/.config/frogminer/linux-tkg.cfg missing" >&2; exit 1; }
command -v sbsign >/dev/null || { echo "ERR: sbsign missing (sudo apt install sbsigntool)" >&2; exit 1; }
[ -f "$MOK_KEY" ] && [ -f "$MOK_CERT" ] || { echo "ERR: kernel-MOK missing in $RESEARCH/infra/mok-kernel/" >&2; exit 1; }

echo "== Building + installing BORE kernel via tkg (long; ~20-60 min) =="
( cd "$TKG" && ./install.sh install )

KVER=$(ls -1 "$TKG"/DEBS/linux-image-*_*_amd64.deb 2>/dev/null | sort -V | tail -1 \
        | sed -E 's#.*/linux-image-([^_]+)_.*#\1#')
[ -n "$KVER" ] || { echo "ERR: couldn't detect built kernel version in $TKG/DEBS" >&2; exit 1; }
IMG=/boot/vmlinuz-$KVER
[ -f "$IMG" ] || { echo "ERR: $IMG not installed by tkg" >&2; exit 1; }
echo "$KVER" > "$TKG/DEBS/.last-kver"
echo "Built kernel: $KVER"

echo "== Secure Boot: signing $IMG with kernel-MOK =="
if sbverify --cert "$MOK_CERT" "$IMG" >/dev/null 2>&1; then
    echo "  already signed with this MOK."
else
    sudo sbsign --key "$MOK_KEY" --cert "$MOK_CERT" --output "$IMG.signed" "$IMG"
    sudo mv "$IMG.signed" "$IMG"
    echo "  signed OK."
fi
sudo update-grub

if ! mokutil --test-key "$MOK_CERT" >/dev/null 2>&1; then
    echo
    echo "!! kernel-MOK NOT enrolled — signed kernel won't boot under Secure Boot until you run:"
    echo "     sudo mokutil --import $MOK_CERT   (set a one-time password, reboot, choose Enroll MOK)"
fi

cat <<EOF

DONE: kernel $KVER built + signed. GRUB default unchanged (stock = fallback).
Next: scripts/g2-kernel-rebuild-all.sh handles NVIDIA; or manually:
  KVER=$KVER scripts/nvidia-build-modules.sh && KVER=$KVER scripts/nvidia-install-modules.sh
EOF
