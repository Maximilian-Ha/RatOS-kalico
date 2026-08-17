# RatOS-Kalico

RatOS 2.1, forked to run on [Kalico](https://github.com/KalicoCrew/kalico)
instead of Rat-OS/klipper.

> ### Status: builds green, **never run on a printer**
>
> Every patch in here applies to today's upstream and passes the behavioural
> tests in `tests/`. Those tests cover the kinematics contract and prove the
> firmware port does not change the computed mesh; they do **not** cover every
> patched hunk, and `tests/` says which. None of it has touched hardware. Two
> pieces of infrastructure are also still missing — see
> [What is not done](#what-is-not-done).
>
> Do not put this on a printer you need this week.

---

## Why a fork, and not a patch

The earlier attempt patched a live RatOS install. That cannot work, for one
specific reason:

`ratos-update.sh` runs `klipper-fork-migration.sh` as the **first step of every
update**. That script decides whether `~/klipper` is acceptable by comparing
`git remote get-url origin` against a hardcoded allowlist — four spellings of
`Klipper3d/klipper`, plus `Rat-OS/klipper`. A Kalico checkout matches nothing,
falls through to `UNSUPPORTED_REPOSITORY_SOURCE`, and returns 2. That failure
propagates and the whole RatOS update fails. Forever, on every update.

You cannot fix that by editing the file in place either: it lives in the
configurator's git checkout, which Moonraker pulls and resets.

So the fork's job is to own that decision. Everything else follows.

## What the fork actually consists of

Three parts, of which this repository is the definition of all three:

| Part | What it becomes | Delta |
|---|---|---|
| **Kalico fork** | `~/klipper` on the printer | one derived commit, +131/−42 in two files |
| **Configurator fork** | `~/ratos-configurator`, and via symlink `~/printer_data/config/RatOS` | 17 files |
| **Third-party** | beacon, `klipper_tmc_autotune` | nothing — both already support Kalico |

This repo holds no vendored copies. `scripts/build-*.sh` fetch pristine
upstream and derive the fork branches, so following a new RatOS or Kalico
release is a re-run, not a merge.

## Setting it up

**1. Check the fork still builds against today's upstream.** Needs nothing but
git, python3 and network:

```bash
tests/run-all.sh
```

**2. Create the two fork repositories.** This is the one manual step — on
github.com, press *Fork* on each:

| Fork this | into | keep the default name |
|---|---|---|
| `KalicoCrew/kalico` | your account | `kalico` |
| `Rat-OS/RatOS-configurator` | your account | `RatOS-configurator` |

Then check the URLs in [`fork.conf`](fork.conf) match. They are pre-filled for
`Maximilian-Ha`; nothing else in the repo hardcodes them.

**3. Build and publish both fork branches.** Order matters — `moonraker.conf`
has to pin the Kalico commit, so the firmware fork goes first:

```bash
scripts/build-kalico-fork.sh --push        # ratos-kalico/v2.1.x + master alias
scripts/build-configurator-fork.sh --push  # v2.1.x-kalico
```

**4. Let CI build the deployment branch.** The push in step 3 triggers the
fork's own `publish-kalico.yml`, which builds the Next.js app, renames `src/` to
`app/` and publishes `v2.1.x-kalico-deployment`. Wait for it to go green — the
printer is pointed at *that* branch, not at the source branch. If Actions are
disabled on a new fork, enable them once under the repository's Actions tab.

```bash
git ls-remote <your-fork> v2.1.x-kalico-deployment   # must return a ref
```

**5. On the printer, before changing anything:**

```bash
scripts/preflight.sh
```

Then work through [`docs/TESTPLAN.md`](docs/TESTPLAN.md). Do not skip it — it is
ordered so that the things which can crash a toolhead are checked before the
things that can only waste filament.

## What is solved

The hard blockers, each verified against the real sources:

- **The migration script.** Allowlist moved to Kalico; the pre-fork Klipper
  origins listed as deprecated so already-deployed machines *migrate forward*
  instead of aborting; URLs compared normalized instead of byte-exactly.
- **`bed_mesh` on Kalico.** `ZMesh` gains its reactor parameter (three RatOS
  call sites pass it, including the core `ratos.py`), `split_delta_z` accepts
  the `0.001` six V-Core 4 profiles ship, and `log_points` becomes a real
  option — without which Klippy simply does not start. Plus the reactor
  yielding those RatOS commits exist for. Proven not to change the computed
  mesh: `tests/test_bed_mesh_port.py`, 13/13.
- **Kinematics.** `supports_dual_carriage`, axis-name `set_position`,
  `clear_homing_state`. Proven: `tests/test_kinematics.py`, 12/12 patched —
  and the same suite run against unpatched RatOS fails, which `run-all.sh`
  asserts so the test cannot decay into a tautology.
- **`resonance_generator`.** Kalico widened `ResonanceTestExecutor.run_test`
  from 3 to 5 parameters, so `GENERATE_RESONANCES` raises `TypeError` today.
- **`gcode_shell_command.py`.** Kalico ships and git-tracks its own; RatOS'
  symlink is silently clobbered by the migration's `reset --hard`, and *both*
  RatOS verifiers report success anyway. The fork cedes the file deliberately.
- **`sweeping_period`.** Kalico defaults it to `0.0` where Klipper uses `1.2`,
  silently degrading every sweep to plain vibration pulses. Pinned.

Two risks that looked fatal turned out not to be, and are worth stating because
the earlier analysis got them wrong in both directions:

- **Beacon works.** Kalico's `probe.py` is the *pre-2024* Klipper probe API.
  But `BeaconProbeWrapper` implements both protocols, and its
  `run_probe(self, gcmd, *args, **kwargs)` absorbs the extra `retry_session`
  argument Kalico passes. Nothing to do.
- **`klipper_tmc_autotune` works.** It already carries
  `from klippy.extras import tmc  # Kalico`, and reads `get_current()` by index
  rather than unpacking, so Kalico's 5-tuple is harmless.

## What is not done

1. **The deployment branch has never been built.** The fork ships its own
   `publish-kalico.yml` — forked from RatOS' workflow, with the
   `last-successful-commit-action` dependency removed because it breaks on a
   fork's first run — and `build-configurator-fork.sh` installs it and removes
   upstream's. But no CI run has ever executed it here, so the pnpm build is
   unproven. Expect to babysit the first run.
2. **No install/rollback script.** Deliberately. Preflight is read-only;
   switching a printer over is written up in `docs/TESTPLAN.md` as steps you
   run and check, because an unattended script that half-migrates a printer is
   worse than no script.
3. **The klippy venv.** Kalico needs numpy 2.x and jinja2 ≥3.1.6; RatOS' image
   pins numpy ≤1.23.4 and jinja2 2.11.3. That jinja2 jump changes template
   semantics for *every* RatOS macro. Untested. This is the largest unquantified
   risk in the project — [`docs/RISKS.md`](docs/RISKS.md).
4. **Nothing is hardware-verified.** Kalico's own `bed_mesh` regression tests
   could not be run here either: this build environment blocks PyPI, so numpy
   and jinja2 could not be installed.

## Layout

```
fork.conf                     repo URLs and branches — the only place they live
kalico/                       the firmware port + its provenance
configurator/                 the RatOS delta, as anchored transforms
scripts/                      build the forks; preflight a printer
tests/                        offline proofs; run-all.sh does everything
docs/                         architecture, risks, test plan, maintenance
```

## Licence

The patches derive from GPLv3 works (Klipper, Kalico, RatOS) and carry those
terms. The port in `kalico/` is co-authored by Tom Glastonbury, whose six
commits it re-implements — see [`kalico/PROVENANCE.md`](kalico/PROVENANCE.md).
