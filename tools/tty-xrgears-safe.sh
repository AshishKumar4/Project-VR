#!/usr/bin/env bash
# TTY VR test with xrgears + bulletproof display recovery
#
# Run from TTY (Ctrl+Alt+F3, sudo systemctl stop gdm, then this).
# After test, this script forcibly restarts X via gdm.

set +e  # we want to see every step happen, even if some fail

LOG=~/g2-linux-research/logs/tty-xrgears-$(date +%Y%m%d-%H%M%S).log
mkdir -p ~/g2-linux-research/logs
exec > >(tee -a "$LOG") 2>&1

XRGEARS=~/g2-linux-research/src/xrgears/build/src/xrgears
MONADO=~/.local/bin/monado-service
FIFO=/tmp/m_fifo

# === Trap for forcible cleanup ===
cleanup() {
    echo
    echo "=== CLEANUP (always runs, even on errors) ==="
    pkill -9 xrgears 2>/dev/null
    pkill -9 monado-service 2>/dev/null
    sleep 1
    rm -f $FIFO /run/user/1000/monado_comp_ipc 2>/dev/null
    echo "killed monado/xrgears"

    echo "Restarting gdm to restore display engine..."
    sudo systemctl restart gdm
    sleep 4
    if systemctl is-active gdm | grep -q active; then
        echo "gdm OK"
    else
        echo "gdm not back — trying nvidia_drm reload as fallback"
        sudo modprobe -r nvidia_drm 2>/dev/null
        sudo modprobe nvidia_drm modeset=1
        sudo systemctl start gdm
    fi
    echo "Cleanup done at $(date)"
    echo
    echo "=========================================="
    echo " You can now Ctrl+Alt+F1 to return to GUI"
    echo "=========================================="
}
trap cleanup EXIT INT TERM

echo "==============================="
echo " G2 xrgears VR test — $(date)"
echo "==============================="

# Sanity
if pgrep -x Xorg >/dev/null; then
    echo "ERROR: X is running. Run 'sudo systemctl stop gdm' first."
    exit 1
fi

modinfo nvidia-modeset | grep "^signer" | head -1

echo
echo "=== Start monado-service with FIFO stdin (stays alive, render mode 1=4320x2160@90) ==="
mkfifo $FIFO
exec 9<>$FIFO

LD_LIBRARY_PATH="$HOME/.local/lib" \
XRT_LOG=info \
XRT_COMPOSITOR_PRINT_MODES=1 \
XRT_COMPOSITOR_DESIRED_MODE=1 \
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2 \
$MONADO <&9 > /tmp/mon.log 2>&1 &
MS_PID=$!
echo "monado-service pid=$MS_PID"

# Wait for IPC socket
for i in 1 2 3 4 5 6 7 8 9 10; do
    [ -S /run/user/1000/monado_comp_ipc ] && break
    sleep 1
done
[ -S /run/user/1000/monado_comp_ipc ] || { echo "monado never started!"; exit 1; }
echo "IPC socket up after ${i}s"
sleep 2  # let initialization settle

echo
echo "=== Launch xrgears for 25 seconds ==="
echo "PUT THE HEADSET ON NOW."
LD_LIBRARY_PATH="$HOME/.local/lib" \
XR_RUNTIME_JSON="$HOME/.config/openxr/1/active_runtime.json" \
$XRGEARS > /tmp/xrgears.log 2>&1 &
XR_PID=$!
echo "xrgears pid=$XR_PID"

# Run for 25 seconds, then forcibly stop
sleep 25
echo
echo "=== Stopping xrgears ==="
kill -INT $XR_PID 2>/dev/null
sleep 2
kill -9 $XR_PID 2>/dev/null

echo
echo "=== xrgears output ==="
tail -30 /tmp/xrgears.log

echo
echo "=== monado log (relevant lines) ==="
grep -E "Selected|render_resources|swapchain|VK_ERROR|XRT_ERROR|App is ready|Frame.*present|Pacing|client" /tmp/mon.log | head -30

# cleanup() trap runs on exit
