#!/usr/bin/env bash
# O-5: Pre-pin GPU clocks for a VR session on RTX 4080.
# Hard-coded to this card: floor 2820 MHz (P0 base), ceiling 3105 MHz (boost
# max), memory 11201 MHz (only active memory P-state), power 320 W.
#
# Usage: vr-gpu-pin.sh {on|off|verify}
set -euo pipefail

LOG=/run/user/$(id -u)/vr-gpu.log
PIDFILE=/run/user/$(id -u)/vr-gpu-watch.pid

SM_FLOOR=2820
SM_CEIL=3105
MEM_FIXED=11201
PWR_W=320

verify() {
    nvidia-smi \
        --query-gpu=clocks.gr,clocks.mem,pstate,clocks_throttle_reasons.active,power.draw \
        --format=csv,noheader
}

# Background watchdog: warn if clocks fall below floor while pinned.
watchdog() {
    while :; do
        line=$(nvidia-smi --query-gpu=clocks.gr,clocks_throttle_reasons.active \
                          --format=csv,noheader,nounits 2>/dev/null) || break
        sm=$(echo "$line" | cut -d, -f1 | tr -d ' ')
        thr=$(echo "$line" | cut -d, -f2 | tr -d ' ')
        if [ -n "$sm" ] && [ "$sm" -lt "$SM_FLOOR" ] 2>/dev/null; then
            echo "$(date -Iseconds) WARN clock below floor: sm=${sm}MHz throttle=${thr}" >>"$LOG"
        fi
        sleep 2
    done
}

case "${1:-}" in
on)
    echo "[vr-gpu] pinning sm=${SM_FLOOR}-${SM_CEIL}MHz mem=${MEM_FIXED}MHz pwr=${PWR_W}W"
    sudo nvidia-smi -pm 1
    sudo nvidia-smi -pl "$PWR_W"
    sudo nvidia-smi -lgc "${SM_FLOOR},${SM_CEIL}"
    sudo nvidia-smi -lmc "${MEM_FIXED},${MEM_FIXED}"
    sudo nvidia-smi -c DEFAULT
    sleep 1
    verify | tee -a "$LOG"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
    fi
    ( watchdog ) &
    echo $! >"$PIDFILE"
    ;;
off)
    echo "[vr-gpu] releasing clocks"
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
    fi
    sudo nvidia-smi -rgc
    sudo nvidia-smi -rmc
    sudo nvidia-smi -pl "$PWR_W"
    sudo nvidia-smi -pm 0
    verify | tee -a "$LOG"
    ;;
verify)
    verify
    ;;
*)
    echo "usage: $0 {on|off|verify}" >&2
    exit 1
    ;;
esac
