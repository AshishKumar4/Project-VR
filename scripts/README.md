> Note: the NVIDIA commit list referenced below is 2 behind (omits commits 23/24). The authoritative patch/commit set is [`../patches/INDEX.md`](../patches/INDEX.md).

> 🤖 **AI-authored & maintained — presented as-is.** This document is written and maintained by
> Claude (Anthropic) as part of this project. When in doubt, trust the code.

# NVIDIA driver porting + maintenance scripts

Version-agnostic tooling for keeping our patched NVIDIA open-source kernel
modules current across upstream releases. Every script auto-detects the
NVIDIA version from `version.mk`, so they work for 580/590/595/600/...

## Workflow

### Daily: rebuild against current kernel (same NVIDIA version)
After a kernel update, or to refresh modules on disk:
```bash
scripts/nvidia-build-modules.sh
scripts/nvidia-install-modules.sh
# reboot
```

Or one-shot via the orchestrator:
```bash
scripts/g2-nvidia-rebuild-all.sh
```

### Periodic: check for a new NVIDIA release
```bash
scripts/nvidia-fetch-upstream.sh          # lists tags newer than ours
# or
scripts/g2-nvidia-rebuild-all.sh --check  # same, plus build/install hint
```

### Bump to a new NVIDIA tag
After fetch-upstream shows a newer tag (e.g. `600.05.04`):
```bash
scripts/nvidia-rebase-onto.sh --dry-run 600.05.04   # preview
scripts/nvidia-rebase-onto.sh 600.05.04             # actually do it
scripts/nvidia-build-modules.sh
scripts/nvidia-install-modules.sh
# reboot
```

Or atomic via the orchestrator:
```bash
scripts/g2-nvidia-rebuild-all.sh --bump 600.05.04
```

If cherry-pick conflicts during rebase, the script drops you into
`git cherry-pick --continue` flow with all our standalone patches
in `patches/` available as a reference.

### Recover from a bad install
```bash
scripts/nvidia-rollback.sh --latest                 # most recent backup
# or
scripts/nvidia-rollback.sh ~/g2-linux-research/module-backup-595.71.05-...
```

## What the scripts do

| Script | Action |
|---|---|
| `nvidia-fetch-upstream.sh` | `git fetch --tags` on open-gpu-kernel-modules; lists tags newer than our base, same-major point releases, and next-major candidates. |
| `nvidia-rebase-onto.sh <tag>` | Creates `g2-patches-on-<tag>` branch off `<tag>`, cherry-picks our local commits. Safety tag saved before any change. |
| `nvidia-build-modules.sh` | `make modules SYSSRC=/lib/modules/$(uname -r)/build`. Verifies all four .ko emitted. |
| `nvidia-install-modules.sh` | Auto-detects `nvidia-<MAJOR>-open` family. Backs up originals, MOK-signs each .ko with `sha256`, installs into `/lib/modules/.../nvidia-<MAJOR>-open/`, `depmod -a`, holds the relevant apt packages. |
| `nvidia-rollback.sh <dir>` | Restores .ko files from a backup dir. |
| `g2-nvidia-rebuild-all.sh` | Orchestrator: `--check`, `--bump <tag>`, or no-arg rebuild. |

## Our patches (committed on the branch)

```
git -C src/open-gpu-kernel-modules log --oneline <BASE_TAG>..HEAD
```

As of 2026-05-20 on `g2-patches-on-595.71.05`:
- `a5df8568` G2 VR headset patches (the bundle: DP wardatabase HP G2,
  DisplayID Type-7 parser fix, NVKMS preferred-mode-for-VR, MSFT VSDB
  use-case parser).
- `16983cfd` nvidia-drm: mark VR HMD connectors `non_desktop=1`.
- `18f87cba` nvidia-drm/nvkms: bridge DRM-lease to NVKMS flip permissions
  (the patch 22 work — unblocks `vkQueuePresentKHR` on Wayland direct mode).

Standalone `.diff` mirrors of every patch live in `../patches/`.

## What's NOT here

- Userspace VR stack rebuild (Monado, Basalt, OpenComposite, OpenXR loader): no single script — see the
  project `CLAUDE.md` ("Build & test" + "Run & capture"). In short: the SteamVR tracking driver =
  `ninja -C ../src/monado-thaytan/build-cmake driver_monado.so` → `cp` to
  `~/.local/share/steamvr-monado/bin/linux64/`; the standalone OpenXR `monado-service` =
  `ninja -C ../src/monado-thaytan/build install` → `~/.local`.
- mutter Debian-package rebuild: separate `../src/mutter-patch/` workflow.

## Notes on driver availability

NVIDIA's `open-gpu-kernel-modules` repo HEAD == 595.71.05 as of fetch.
- Per-version branches exist (515, 520, ..., 580, 595) but there's no
  development branch ahead of the leading tag.
- `VK*` branches (VK516_10, VK526_25, VK535_87, VK551_06) are Vulkan
  beta forks that typically LAG production; ignore unless testing a
  specific new VK extension.
- For an "absolute latest" beyond what's in this repo, NVIDIA's
  proprietary Vulkan beta driver at developer.nvidia.com is sometimes
  ahead — but it has no public source so our patches cannot apply.
