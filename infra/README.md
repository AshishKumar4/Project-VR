> 🤖 **AI-authored & maintained — presented as-is.** This document is written and maintained by
> Claude (Anthropic) as part of this project. When in doubt, trust the code.

# infra — durable, self-healing G2 stack management

One manifest + one orchestrator + one event hook. No daemons, no polling, no
external deps (`tomllib` is stdlib). The heal logic lives in `g2ctl`; the
manifest is data only.

## Files
- `g2.manifest.toml` — single source of truth (repos, branches, trigger packages, holds).
- `g2ctl` — `status` / `verify` / `heal [--auto]` / `doctor`. Idempotent.
- `hooks/99-g2ctl` — apt `Post-Invoke`: touches the drift flag after package changes.
- `hooks/g2-heal.{path,service}` — user systemd: flag → `g2ctl heal --auto`.
- `install.sh` — idempotent installer (symlink + apt hook + systemd units).

## How it stays durable
1. A system update changes nvidia / mutter / kernel packages → apt hook touches
   `~/g2-studio/var/drift`.
2. The user `g2-heal.path` unit fires `g2ctl heal --auto`.
3. `heal` re-applies our patch set per component, rebuilds, installs, verifies —
   skipping anything already in sync (idempotent).
4. If re-applying patches hits a **merge/rebase conflict**, `g2ctl` invokes Claude
   Code headless to resolve it on a backup ref, then rebuilds and re-verifies;
   it never pushes and stops for review if it can't verify.

## g2ctl
```
g2ctl status     # read-only drift report per component
g2ctl verify     # smoke checks
g2ctl heal       # bring drifted components back in sync (self-heals conflicts)
g2ctl doctor     # status -> heal -> verify
g2ctl heal --component nvidia    # limit to one
```
Reuses `scripts/` for NVIDIA (fetch/rebase/build/sign/install/hold). Monado heal builds **both**
products of the one repo: the OpenXR `monado-service` (`ninja -C build install` → `~/.local`) AND the
SteamVR tracking driver `driver_monado.so` (`ninja -C build-cmake driver_monado.so`, then a backed-up,
md5-verified `cp` to the `external_drivers` path). `status` reports the deployed driver's md5 and whether
it is STALE vs the built one. mutter = `dpkg-buildpackage` → apt install + hold.

## Privilege model (security)
`g2ctl` runs as the user. Monado heals fully unattended. **nvidia** and **mutter**
heal steps need `sudo` (module install / `apt install`) and **nvidia needs a
reboot** to load new modules — so the unattended service heals what it can and
logs the rest; run `sudo g2ctl heal` to finish, then reboot if nvidia changed.

*Optional full automation:* add a scoped `sudoers` rule for the specific install
scripts (`scripts/nvidia-install-modules.sh`, `apt-get install/apt-mark`). This
is a deliberate security trade-off — left opt-in, not installed by default.

## Logs & safety
Each heal run logs to `~/g2-studio/var/heal/<timestamp>/`. NVIDIA rebases create
safety tags; mutter/monado build from clean trees. Backups of the pre-consolidation
state live in `../backups/`.
