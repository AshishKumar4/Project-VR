#!/usr/bin/env bash
# One-time: build + install the sched_ext schedulers (scx_lavd + scx_loader + scxctl)
# from source (not packaged in Ubuntu 26.04). Kernel-agnostic: needs sched_ext only
# (CONFIG_SCHED_CLASS_EXT=y), which both your stock and BORE kernels have.
#
#   scripts/scx-setup.sh
#
# Model: BORE stays the default scheduler; scx_lavd is loaded ON-DEMAND for games
# (it can regress CPU-light titles, so it is situational — see KERNEL-BUILD-PLAN.md).
set -euo pipefail
RESEARCH=$HOME/g2-linux-research
SCX=$RESEARCH/src/scx
[ -d /sys/kernel/sched_ext ] || { echo "ERR: running kernel lacks sched_ext" >&2; exit 1; }

echo "== deps =="
sudo apt install -y libbpf-dev llvm clang meson ninja-build cargo rustc pkg-config

echo "== fetch scx =="
if [ -d "$SCX/.git" ]; then git -C "$SCX" pull --ff-only || true
else git clone https://github.com/sched-ext/scx.git "$SCX"; fi

echo "== build + install =="
cd "$SCX"
meson setup build --prefix=/usr -Dbuildtype=release 2>/dev/null || meson setup --reconfigure build --prefix=/usr -Dbuildtype=release
meson compile -C build
sudo meson install -C build

echo
echo "== verify =="
command -v scx_lavd scxctl >/dev/null && echo "  scx_lavd + scxctl installed" || echo "  WARN: binaries not on PATH"

cat <<'EOF'

DONE. Use it:
  Quick test (foreground):   sudo scx_lavd        # Ctrl+C to stop; check /sys/kernel/sched_ext/state
  Via the loader:            scxctl switch -s scx_lavd -m gaming   /   scxctl stop
  Persistent loader service: sudo systemctl enable --now scx_loader   (BORE stays default)

AUTO-SWITCH per game (recommended) — add to ~/.config/gamemode.ini under [custom]:
  start=scxctl switch -s scx_lavd -m gaming
  end=scxctl stop
(Then launch games with `gamemoderun mangohud %command%`. MangoHud's `gamemode` field
 confirms it's active; watch 1% lows — drop scx for any title it regresses.)
EOF
