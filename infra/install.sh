#!/usr/bin/env bash
# Idempotent installer for the g2ctl durable infra:
#   1. g2ctl on PATH (user)
#   2. apt drift hook (root) — touches the drift flag after package changes
#   3. user systemd auto-heal units (flag -> g2ctl heal --auto)
#   4. scoped sudoers + root-owned install wrappers (unattended nvidia/mutter heal)
# Safe to re-run.
set -euo pipefail
INFRA=$(dirname "$(readlink -f "$0")")
RESEARCH=$(cd "$INFRA/.." && pwd)
U=$(id -un)
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# 1. g2ctl on PATH (user-local; no sudo)
mkdir -p ~/.local/bin
ln -sf "$INFRA/g2ctl" ~/.local/bin/g2ctl
echo "✓ ~/.local/bin/g2ctl -> $INFRA/g2ctl"

# 2. apt drift hook (root)
sed "s/mrwhite0racle/$U/g" "$INFRA/hooks/99-g2ctl" | sudo tee /etc/apt/apt.conf.d/99-g2ctl >/dev/null
echo "✓ /etc/apt/apt.conf.d/99-g2ctl"

# 3. user systemd auto-heal (no sudo)
mkdir -p ~/.config/systemd/user
cp "$INFRA/hooks/g2-heal.path" "$INFRA/hooks/g2-heal.service" ~/.config/systemd/user/
if systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now g2-heal.path
    echo "✓ g2-heal.path enabled (auto-heal on drift)"
else
    echo "… user systemd not reachable here; enable later: systemctl --user enable --now g2-heal.path"
fi

# 4. scoped sudoers + ROOT-OWNED install wrappers (not user-writable).
#    The research path is baked in so the wrappers work when run as root.
sed "s|\$HOME/g2-linux-research|$RESEARCH|g" "$RESEARCH/scripts/nvidia-install-modules.sh" \
    | sudo tee /usr/local/sbin/g2ctl-nvidia-install >/dev/null
sed "s|/home/mrwhite0racle/g2-linux-research|$RESEARCH|g" "$RESEARCH/scripts/mutter-install-debs.sh" \
    | sudo tee /usr/local/sbin/g2ctl-mutter-install >/dev/null
sudo chown root:root /usr/local/sbin/g2ctl-nvidia-install /usr/local/sbin/g2ctl-mutter-install
sudo chmod 0755 /usr/local/sbin/g2ctl-nvidia-install /usr/local/sbin/g2ctl-mutter-install
sed "s/mrwhite0racle/$U/g" "$INFRA/hooks/g2ctl.sudoers" | sudo tee /etc/sudoers.d/g2ctl >/dev/null
sudo chmod 0440 /etc/sudoers.d/g2ctl
sudo visudo -c -f /etc/sudoers.d/g2ctl
echo "✓ /etc/sudoers.d/g2ctl + root-owned install wrappers"

# 5. scoped VR-performance sudoers (GPU clock/governor/setcap — no broad root).
sed "s/mrwhite0racle/$U/g" "$INFRA/hooks/g2-vr-perf.sudoers" | sudo tee /etc/sudoers.d/g2-vr-perf >/dev/null
sudo chmod 0440 /etc/sudoers.d/g2-vr-perf
sudo visudo -c -f /etc/sudoers.d/g2-vr-perf
echo "✓ /etc/sudoers.d/g2-vr-perf (scoped clock/governor/setcap)"

echo
echo "Done. Try:  g2ctl status   |   g2ctl doctor"
echo "Note: nvidia heal still needs a REBOOT to load new modules."
