# How RatOS 2.1 actually updates, and where the fork cuts in

Understanding the fork requires understanding one thing: **RatOS updates do not
flow through the `RatOS` repository.** That repo builds the OS image. On a
running machine, everything flows through the *configurator*.

## The pieces on a live printer

```
~/ratos-configurator/                  <- Moonraker pulls this
    configuration/                     <- symlinked to ...
        klippy/*.py                    <- 13 extensions + kinematics
        scripts/*.sh                   <- the whole update pipeline
        moonraker.conf                 <- update_manager entries
        printers/, macros/, z-probe/   <- the config library

~/printer_data/config/RatOS  ---symlink---> ~/ratos-configurator/configuration
~/klipper/                                  <- Rat-OS/klipper, not stock Klipper
    klippy/extras/<ratos modules>           <- symlinks into the checkout
```

`~/printer_data/config/RatOS` is a **symlink**. RatOS' installer `rm -rf`s
whatever is there and links it to `configuration/`. So editing the configurator
repo edits the printer's live config directory.

## The update path

Three ignition points, one destination:

1. Moonraker's `update_manager` pulls `~/ratos-configurator` from its
   deployment branch.
2. That `git pull` fires `.git/hooks/post-merge`, which RatOS has symlinked to
   `src/scripts/post-merge.sh`.
3. That runs `sudo update.sh`, then
   `sudo configuration/scripts/ratos-update.sh` — the master update.

(`ratos doctor` runs the same two scripts directly.)

`ratos-update.sh` runs eighteen steps under `set +e`. **Step one is
`ensure_klipper_fork_migration`.**

## The chokepoint

`klipper-fork-migration.sh` exists because RatOS 2.1 does not run on stock
Klipper — it runs on `Rat-OS/klipper`, branch `ratos/v2.1.x`. The script's job
is to drag `~/klipper` onto that fork on every update.

It decides whether a checkout is acceptable by **exact string comparison** of
`git remote get-url origin` against a hardcoded allowlist. No normalization at
all — no `.git` stripping, no trailing slash, no case folding. Anything not on
the list logs `UNSUPPORTED_REPOSITORY_SOURCE` and returns 2.

`TARGET_COMMIT` is not a constant: it is awk-parsed at runtime out of the
`[update_manager klipper]` section of `configuration/moonraker.conf`, and must
be exactly 40 hex characters. So `moonraker.conf` and the migration script are
coupled — the same value drives Moonraker's updater and a `git reset --hard`.

**This is the whole reason a fork is necessary.** Everything else is a
consequence.

## Where the fork cuts in

```
                upstream Kalico          upstream RatOS-configurator
                       |                            |
                       v                            v
        kalico/0001-*.patch          configurator/patch_configurator.py
                       |                            |
                       v                            v
            ratos-kalico/v2.1.x            v2.1.x-kalico
             + master (recovery)                    |
                       |                            v
                       |                   (CI build: src/ -> app/)
                       |                            |
                       v                            v
                  ~/klipper               v2.1.x-kalico-deployment
                                                    |
                                                    v
                                          ~/ratos-configurator
```

Two derived branches, both re-derivable from upstream at any time. This repo
holds the derivation, not the result.

## Why not vendor the forks here

`RatOS-configurator`'s working tree is 416 MB, 282 MB of it board firmware.
Copying that into a third repository would produce something nobody can review
and that diverges the moment upstream moves.

Deriving instead means:

- the fork's actual delta is 17 files, readable in one sitting
- following a new RatOS release is a re-run, not a merge
- a moved upstream anchor is a **loud failure at build time**, not a silent
  behaviour change on a printer

## What Kalico changes that matters here

Kalico is not "Klipper plus features". It is a hard fork from an *older* Klipper
base with a restructured host, so RatOS hits incompatibilities in both
directions:

- **`klippy/` is a package now.** A generic meta-path hook (`klippy/compat.py`)
  rewrites any bare top-level import that resolves under `klippy/`, so RatOS'
  and Beacon's flat `import pins` / `from mcu import MCU` still work. Nothing to
  do — but only because that hook is generic.
- **The kinematics contract changed.** `homing_axes` is a string;
  `clear_homing_state` and `supports_dual_carriage` are mandatory.
- **`probe.py` is the pre-2024 API.** No `ProbeSessionHelper`, no
  `start_probe_session`. Beacon happens to implement both protocols, which is
  the only reason this project is viable at all.
- **`bed_mesh` predates the `ProbeManager` refactor**, so RatOS' patches to it
  have to be re-implemented rather than cherry-picked.
- **Unknown config options are fatal by default**
  (`error_on_unused_config_options`), which is why `log_points` had to become a
  real option rather than be deleted.

## What deliberately stays upstream's

The fork changes as little as it can get away with. Untouched: `klipper.service`,
`klipper.env`, `flash-path.sh`, `klipper-compile.sh`, every post-merge shim, the
whole extension registration and symlink machinery, and all board definitions —
Kalico preserves Klipper's directory layout, so none of it needs to know.
