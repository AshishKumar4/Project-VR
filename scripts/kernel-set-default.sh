#!/usr/bin/env bash
# Promote a kernel to the persistent GRUB default. Uses GRUB_DEFAULT=saved so it
# SURVIVES new stock-kernel installs (which otherwise shift the numeric index).
#
#   scripts/kernel-set-default.sh <KVER>     e.g. 7.0.12-tkg-bore
set -euo pipefail
KVER="${1:?Usage: $0 <KVER>}"
[ -f "/boot/vmlinuz-$KVER" ] || { echo "ERR: /boot/vmlinuz-$KVER not found" >&2; exit 1; }

sudo sed -i 's/^#\?GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/' /etc/default/grub
sudo update-grub
# Find the (non-recovery) menuentry id matching this kernel
ENTRY=$(awk -F\' "/menuentry / && /$KVER/ && !/recovery/ {print \$2; exit}" /boot/grub/grub.cfg)
[ -n "$ENTRY" ] || { echo "ERR: no GRUB entry matches $KVER; set default manually" >&2; exit 1; }
sudo grub-set-default "Advanced options for Ubuntu>$ENTRY"
echo "GRUB default -> '$ENTRY' (saved; survives stock-kernel updates). Reboot to confirm."
echo "Revert to stock default later:  sudo grub-set-default 0   (or re-run with the stock KVER)"
