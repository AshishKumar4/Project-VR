#!/usr/bin/env bash
# TTY test using Monado's VK_DISPLAY backend (raw KMS/DRM, no X/Wayland)
# This is the one path documented as "tested on NVIDIA proprietary"

set +e
LOG=~/g2-linux-research/logs/tty-vkdisplay-$(date +%Y%m%d-%H%M%S).log
mkdir -p ~/g2-linux-research/logs

exec > >(tee -a "$LOG") 2>&1

echo "==============================="
echo " G2 VK_DISPLAY TTY Test — $(date)"
echo "==============================="

# Sanity
if pgrep -x Xorg >/dev/null; then
    echo "ERROR: X is running. Run 'sudo systemctl stop gdm' first."
    exit 1
fi

echo "=== Environment ==="
echo "User: $(whoami)  TTY: $(tty)"
echo "X: $(pgrep -x Xorg && echo running || echo no)"
echo "GDM: $(systemctl is-active gdm)"
modinfo nvidia-modeset | grep -E "^signer|^version" | head -2
echo

echo "=== Stage 1: List all VK displays (force index 100 → invalid, will list) ==="
DMESG_BEFORE=$(sudo dmesg | wc -l)

# Run monado-service briefly with FORCE_VK_DISPLAY=100 to provoke a list dump
( tail -f /dev/null & echo $! >/tmp/tail.pid ) | \
    LD_LIBRARY_PATH="$HOME/.local/lib" \
    XRT_LOG=info \
    XRT_COMPOSITOR_PRINT_MODES=1 \
    XRT_COMPOSITOR_FORCE_VK_DISPLAY=100 \
    ~/.local/bin/monado-service 2>&1 &
MSPID=$!
sleep 8
kill $MSPID 2>/dev/null
kill $(cat /tmp/tail.pid 2>/dev/null) 2>/dev/null
wait 2>/dev/null
sleep 1

echo
echo "=== Looking for: 'available display' / 'displayName' lines ==="
LOG_LINES=$(tail -200 "$LOG" | grep -nE "available display|displayName|VK_DISPLAY|HP Inc|2880|4320|connector|display[ _]index|comp_window")
echo "$LOG_LINES"
echo

echo "=== Stage 2: try each VK display index 0-5 ==="
for IDX in 0 1 2 3 4 5; do
    echo "--- Trying VK_DISPLAY index $IDX ---"
    rm -f /run/user/1000/monado_comp_ipc

    ( tail -f /dev/null & echo $! >/tmp/tail.pid ) | \
        LD_LIBRARY_PATH="$HOME/.local/lib" \
        XRT_LOG=info \
        XRT_COMPOSITOR_PRINT_MODES=1 \
        XRT_COMPOSITOR_FORCE_VK_DISPLAY=$IDX \
        ~/.local/bin/monado-service 2>&1 > /tmp/vkdisplay-$IDX.log &
    MSPID=$!
    sleep 10
    kill $MSPID 2>/dev/null
    kill $(cat /tmp/tail.pid 2>/dev/null) 2>/dev/null
    wait 2>/dev/null

    # Show outcome for this index
    if grep -q "VK_SUCCESS\|App is ready\|render_resources_init.*New renderer" /tmp/vkdisplay-$IDX.log; then
        echo "*** INDEX $IDX MAY HAVE WORKED ***"
        grep -E "displayName|HP Inc|extents|VK_ERROR|swapchain|render_resources" /tmp/vkdisplay-$IDX.log | head -10
        # Copy this log so we save it
        cp /tmp/vkdisplay-$IDX.log "$LOG.index$IDX-SUCCESS"
    else
        # Just show the failure pattern
        TAIL=$(grep -E "ERROR|displayName|HP Inc|extents" /tmp/vkdisplay-$IDX.log | tail -3)
        echo "  index $IDX: $TAIL"
    fi
done

echo
echo "=== Summary: which indices got past acquire? ==="
for IDX in 0 1 2 3 4 5; do
    f=/tmp/vkdisplay-$IDX.log
    if grep -q "render_resources_init.*New renderer" "$f"; then
        echo "  Index $IDX: REACHED RENDER_RESOURCES_INIT"
    fi
    if grep -q "comp_main_create_system_compositor] Doing init" "$f"; then
        echo "  Index $IDX: compositor init started"
    fi
    HP=$(grep -oE "HP Inc[^,]*" "$f" | head -1)
    [ -n "$HP" ] && echo "  Index $IDX: matched $HP"
done

echo
echo "=== DP-WAR fired this session? ==="
sudo dmesg | tail -n +$DMESG_BEFORE | grep -iE "DP-WAR|HP HMD|Force maximum" | head -10

echo
echo "==============================="
echo " TEST COMPLETE — $(date)"
echo "==============================="
echo
echo "Full log: $LOG"
echo "Per-index logs: /tmp/vkdisplay-*.log"
echo
echo "Bring back the desktop:"
echo "    sudo systemctl start gdm"
echo "Then Ctrl+Alt+F1 to switch back to GUI."
