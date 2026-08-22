# RatOS-Kalico

RatOS 2.1, forked to run on [Kalico](https://github.com/KalicoCrew/kalico)
instead of Rat-OS/klipper.

> ### Status: builds green, preflight green on one real printer, **not yet run**
>
> The forks are built and published, and the deployment branch CI is green.
> Every patch applies to today's upstream and passes the behavioural tests in
> `tests/` — those cover the kinematics contract and prove the firmware port
> does not change the computed mesh, but they do **not** cover every patched
> hunk.
>
> `scripts/preflight.sh` has now been run on a real V-Core 4.1 IDEX 600 and
> passes. That machine turned out to be Raspberry Pi OS **bullseye, aarch64,
> Python 3.9.2**, with **numpy 1.26.4 / scipy 1.11.4 / jinja2 2.11.3 /
> pygam 0.9.1** — exactly the combination the fork pins to, so no venv work is
> needed there. Its beacon was too old and had to be updated first; preflight
> caught that. See [What is not done](#what-is-not-done).
>
> No firmware has been switched yet. Do not put this on a printer you need
> this week.

**Published state**

| | |
|---|---|
| `Maximilian-Ha/kalico` | `ratos-kalico/v2.1.x`, plus a `master` alias at the same commit |
| `Maximilian-Ha/RatOS-configurator` | `v2.1.x-kalico` (source) and `v2.1.x-kalico-deployment` (CI-built, what the printer pulls) |

Commit hashes are deliberately not listed here — they change on every rebuild
and a stale hash is worse than none. Read the live state instead:

```bash
git ls-remote https://github.com/Maximilian-Ha/kalico.git ratos-kalico/v2.1.x
git ls-remote https://github.com/Maximilian-Ha/RatOS-configurator.git v2.1.x-kalico-deployment
```

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
| **Third-party** | beacon, `klipper_tmc_autotune` | nothing, *provided both are current* — see below |

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
- **A real 600 printer type.** RatOS offers this machine at 300/400/500 only,
  so today it is generated as a 500 and hand-patched — which is why the
  generated config had to be renamed to stop the configurator overwriting it.
  The fork ships **V-Core 4.1 IDEX 600** as its own printer type, with this
  frame's real `bedMargin`. It has to be a printer type rather than a size,
  because `bedMargin` is per-printer and is what every derived axis limit and
  parking position comes from. See [`docs/PRINTER-600.md`](docs/PRINTER-600.md)
  — switching the machine over is a separate step from the firmware migration,
  and should be done after it.

Two risks that looked fatal turned out not to be, and are worth stating because
the earlier analysis got them wrong in both directions:

- **Beacon works — if it is current.** Kalico's `probe.py` is the *pre-2024*
  Klipper probe API. Current `beacon_klipper` carries a `BeaconProbeWrapper`
  that implements both protocols, and its
  `run_probe(self, gcmd, *args, **kwargs)` absorbs the extra `retry_session`
  argument Kalico passes. **An older beacon does not**, and then nothing can
  probe: every mesh, Z-tilt and contact routine goes through it. The first
  real printer this was run against had exactly that, so preflight checks the
  installed file rather than trusting upstream — `git -C ~/beacon pull` fixed
  it.
- **`klipper_tmc_autotune` works.** It already carries
  `from klippy.extras import tmc  # Kalico`, and reads `get_current()` by index
  rather than unpacking, so Kalico's 5-tuple is harmless.

## What is not done

1. **No install/rollback script.** Deliberately. Preflight is read-only;
   switching a printer over is written up in `docs/TESTPLAN.md` as steps you
   run and check, because an unattended script that half-migrates a printer is
   worse than no script.
2. **The klippy venv is pinned, not upgraded.** Upstream Kalico asks for
   `numpy~=2.0`, but RatOS pins `pygam==0.9.1`, which caps scipy below 1.12,
   and no such scipy supports numpy 2 — the graph has no solution, and on this
   printer it is a *boot* blocker, not a nuisance. So the fork holds numpy
   below 2 in both requirements files it owns rather than following Kalico's
   pin. Kalico's numpy surface is identical on 1.26; that was audited, not
   assumed. jinja2, which an earlier analysis called the main risk, is close to
   a non-risk. Full graph and reasoning: [`docs/RISKS.md`](docs/RISKS.md) §1.
   What remains untested is whether the pins *hold* across updates — four pip
   runs write that venv. `docs/TESTPLAN.md` stage 2 is the check.
3. **Nothing is hardware-verified.** Kalico's own `bed_mesh` regression tests
   could not be run here either: this build environment blocks PyPI, so numpy
   and jinja2 could not be installed.

## Layout

```
fork.conf                     repo URLs and branches — the only place they live
kalico/                       the firmware port + its provenance
configurator/                 the RatOS delta, as anchored transforms
scripts/                      build the forks; preflight a printer
tests/                        offline proofs; run-all.sh does everything
docs/                         architecture, risks, test plan, maintenance,
                              and the 600 printer type
```

## Licence

The patches derive from GPLv3 works (Klipper, Kalico, RatOS) and carry those
terms. The port in `kalico/` is co-authored by Tom Glastonbury, whose six
commits it re-implements — see [`kalico/PROVENANCE.md`](kalico/PROVENANCE.md).
