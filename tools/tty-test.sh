#!/usr/bin/env bash
# TTY test: run monado-service with no X running, see if patches work
# Run from a TTY (Ctrl+Alt+F3) after stopping gdm

set +e  # Don't exit on first error — we want to capture everything
LOG=~/g2-linux-research/logs/tty-test-$(date +%Y%m%d-%H%M%S).log
mkdir -p ~/g2-linux-research/logs

# Tee everything to log file
exec > >(tee -a "$LOG") 2>&1

echo "==============================="
echo " G2 TTY Test — $(date)"
echo "==============================="
echo

# Sanity checks
echo "=== Sanity ==="
echo "User: $(whoami)"
echo "TTY:  $(tty)"
echo "X running? $(pgrep -x Xorg && echo YES || echo no)"
echo "GDM:  $(systemctl is-active gdm)"
echo

if pgrep -x Xorg >/dev/null; then
    echo "ERROR: X is still running. Run 'sudo systemctl stop gdm' first."
    exit 1
fi

echo "=== Kernel module verification ==="
modinfo nvidia-modeset | grep -E "^filename|^version|^signer" | head -5
echo

echo "=== Current display state ==="
echo "DRM connectors:"
for d in /sys/class/drm/card*-* ; do
    name=$(basename "$d")
    st=$(cat "$d/status" 2>/dev/null)
    sz=$(stat -c %s "$d/edid" 2>/dev/null)
    echo "  $name: $st  edid=${sz}B"
done
echo

echo "=== HMD USB present? ==="
for id in "03f0:0580" "04b4:6504" "04b4:6506" "045e:0659" "0bda:4c15"; do
    if lsusb | grep -q "$id"; then echo "  OK $id"; else echo "  MISSING $id"; fi
done
echo

echo "=== Snapshot dmesg position ==="
DMESG_BEFORE=$(sudo dmesg | wc -l)
echo "dmesg lines before: $DMESG_BEFORE"
echo

echo "=== Running monado-service for 30 seconds (NO X) ==="
echo "Watching for: acquire success, DP-WAR, mode list, render init"
echo "----- monado-service output starts -----"

# Use full env, force logging
rm -f /run/user/1000/monado_comp_ipc

# Keep stdin alive so monado's epoll doesn't bail
( tail -f /dev/null & echo $! >/tmp/tail.pid ) | \
    LD_LIBRARY_PATH="$HOME/.local/lib" \
    XRT_LOG=debug \
    XRT_COMPOSITOR_PRINT_MODES=1 \
    XRT_COMPOSITOR_USE_PRESENT_WAIT=1 \
    XRT_COMPOSITOR_FORCE_NVIDIA=1 \
    XRT_COMPOSITOR_FORCE_NVIDIA_DISPLAY="HP Inc." \
    ~/.local/bin/monado-service 2>&1 &

MSPID=$!
echo "monado-service pid=$MSPID"

# Capture for 30 seconds
sleep 30

echo "----- monado-service stopping -----"
kill $MSPID 2>/dev/null
kill $(cat /tmp/tail.pid 2>/dev/null) 2>/dev/null
wait 2>/dev/null

echo
echo "=== NEW dmesg lines during monado run ==="
sudo dmesg | tail -n +$DMESG_BEFORE | head -50
echo

echo "=== DP-WAR fired? ==="
sudo dmesg | tail -n +$DMESG_BEFORE | grep -iE "DP-WAR|HP HMD|Force maximum|wardatabase" | head -10
echo

echo "=== Connector EDID status post-run ==="
for d in /sys/class/drm/card*-* ; do
    name=$(basename "$d")
    sz=$(stat -c %s "$d/edid" 2>/dev/null)
    [ "$sz" -gt 0 ] && echo "  $name: edid=${sz}B (CAPTURED!)"
done
echo

echo "==============================="
echo " TEST COMPLETE — $(date)"
echo "==============================="
echo "Full log saved to: $LOG"
echo
echo "Bring back the desktop with:"
echo "    sudo systemctl start gdm"
echo
echo "Then log in normally and resume Claude Code."
