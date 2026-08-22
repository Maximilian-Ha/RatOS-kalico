# Bringing a printer over, and proving it works

Ordered so the things that can crash a toolhead are checked before the things
that can only waste filament. Do not reorder. Do not skip to printing.

Have the emergency stop within reach for every step in stage 3.

---

## Stage 0 — before you touch anything

```bash
scripts/preflight.sh
```

Fix every `[FAIL]` first. Then, independently of the script:

1. **Back up the SD card**, or at minimum
   `tar czf ~/ratos-backup.tgz ~/printer_data/config ~/klippy-env`.
2. **Rescue the hand-edits in `RatOS_4.1.cfg`.** It is a generated file with at
   least four hand edits, including the include that makes this a 600 mm
   machine. Copy them into `printer.cfg`. See `docs/RISKS.md` §7.
3. Record the current `~/klipper` commit and branch. Write them down; that is
   your rollback target.
4. **The fork's deployment branch has to exist.** `build-configurator-fork.sh`
   pushes the *source* branch; the fork's own `publish-kalico.yml` then builds
   the app and publishes the deployment branch. Wait for that run to go green —
   see `docs/MAINTENANCE.md`, "The deployment branch". Do not start stage 1
   until this returns a ref:

   ```bash
   git ls-remote <fork-url> v2.1.x-kalico-deployment
   ```

   Pointing Moonraker at a source branch leaves the configurator service with
   nothing to serve.

5. **Record the symlinks in `klippy/extras`, and check the one that can brick
   the update.** Third-party modules live outside `~/klipper` and are linked
   into it; the migration repoints that repository underneath them.

   ```bash
   ls -la ~/klipper/klippy/extras/ > ~/extras.pre-kalico.txt
   cp ~/klipper/.git/info/exclude ~/git-exclude.pre-kalico.txt
   grep -n gcode_shell ~/klipper/.git/info/exclude
   ls -l ~/klipper/klippy/extras/gcode_shell_command.py
   ```

   Preflight `[FAIL]`s if `gcode_shell_command.py` is a symlink that is **not**
   in `.git/info/exclude`. Fix that before anything else: Kalico tracks that
   exact path, so `git checkout` refuses to overwrite the link and the
   migration exits 6 — on every update, permanently. `docs/RISKS.md` §12.

---

## Stage 1 — switch the firmware, do not move anything

Point `~/ratos-configurator` at the fork's deployment branch. The **local
branch name must equal** the `primary_branch` in the fork's `moonraker.conf`,
or Moonraker can never pull the configurator again:

RatOS clones the configurator **single-branch**, so `remote.origin.fetch` names
one branch only and `git fetch origin <other-branch>` writes nothing but
`FETCH_HEAD` — no `origin/<other-branch>` ref is created, and the checkout then
fails with *"is not a commit and a branch cannot be created from it"*. Widen the
refspec first. Moonraker needs that tracking ref too, once `primary_branch`
becomes the fork's branch, so this is a fix rather than a workaround:

```bash
git -C ~/ratos-configurator remote set-url origin https://github.com/Maximilian-Ha/RatOS-configurator.git
git -C ~/ratos-configurator config --get-all remote.origin.fetch   # note it down, for rollback
git -C ~/ratos-configurator config --unset-all remote.origin.fetch
git -C ~/ratos-configurator config --add remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
git -C ~/ratos-configurator fetch origin
git -C ~/ratos-configurator checkout -B v2.1.x-kalico-deployment origin/v2.1.x-kalico-deployment
```

Check before restarting anything — if this still names `Rat-OS/klipper`, the
checkout did not take and `ratos-update.sh` would just run upstream's migration
again and report success:

```bash
git -C ~/ratos-configurator branch --show-current
grep -n 'RATOS_FORK_URL=' ~/printer_data/config/RatOS/scripts/klipper-fork-migration.sh
```

```bash
sudo systemctl restart ratos-configurator moonraker
```

Then run RatOS' own updater:

```bash
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
```

**Watch its output.** The first thing it runs is `klipper-fork-migration.sh`.
You want to see it recognise the old origin as deprecated and migrate forward.
If you see `UNSUPPORTED_REPOSITORY_SOURCE`, stop — the fork's allowlist did not
take.

Then check, before starting Klipper:

```bash
git -C ~/klipper log --oneline -1            # the fork's Kalico commit
grep APP_NAME ~/klipper/klippy/__init__.py   # must say Kalico
~/klippy-env/bin/pip check
~/klippy-env/bin/python -c "import numpy,scipy,jinja2,pygam;print(numpy.__version__,scipy.__version__,jinja2.__version__,pygam.__version__)"
```

And confirm the symlinks came through:

```bash
diff ~/extras.pre-kalico.txt <(ls -la ~/klipper/klippy/extras/)
```

Exactly one difference is expected: `gcode_shell_command.py` is now a regular
file instead of a symlink. That is deliberate — Kalico ships its own, the fork
cedes it, and the source under `printer_data` is untouched. Everything else,
`autotune_tmc.py` and `beacon.py` above all, must still be a symlink and must
still resolve:

```bash
for f in autotune_tmc motor_constants beacon; do
  [ -e ~/klipper/klippy/extras/$f.py ] && echo "ok   $f" || echo "GONE $f"
done
```

If Klippy later reports `Unknown config object 'autotune_tmc stepper_x'`, the
links did not survive. Re-create them with `~/klipper_tmc_autotune/install.sh`
— do **not** regenerate the config to make the error go away.

**Do not upgrade the venv unless something above actually failed to import.**

Nothing on the printer installs Kalico's requirements as part of the switch —
`klipper-fork-migration.sh` has no pip step, and Moonraker's klipper entry only
installs on a requirements *delta*, which the out-of-band repo switch never
produces. So the venv you already have is the venv Kalico will run on, and that
is usually fine: jinja2 2.11.3 does run Kalico's template engine, and numpy is
already present (the image installs it, and pygam raises it further).

**If numpy is 2.x, stop.** RatOS pins `pygam==0.9.1`, which caps scipy below
1.12, and no such scipy supports numpy 2 — so `import pygam` fails and Kalico
takes the printer down at config load over `[beacon_adaptive_heat_soak]`. The
fork pins `numpy>=1.26.4,<2` in both requirements files it owns for exactly
this reason. `docs/RISKS.md` §1 has the full graph.

> **Never run a bare `pip install -r ~/klipper/scripts/klippy-requirements.txt`.**
> An earlier version of this document said to. On the fork's Kalico branch that
> file is already pinned correctly and there is nothing to do; against upstream
> Kalico it installs numpy 2 and breaks the machine, exiting 0 while it does.

If you do have to touch the venv, resolve before installing — one dry run over
*all four* requirements files turns a silent downgrade into a loud
`ResolutionImpossible`, without putting the venv at risk:

```bash
~/klippy-env/bin/python -m pip download --only-binary=:all: -d /tmp/wheels \\
  -r ~/klipper/scripts/klippy-requirements.txt \\
  -r ~/ratos-configurator/configuration/klippy/requirements.txt \\
  -r ~/beacon/requirements.txt \\
  -r ~/klipper_linear_movement_analysis/requirements.txt
```

Snapshot by **copying**, never by moving: three `moonraker.conf` entries declare
`virtualenv: ~/klippy-env`, and Moonraker raises a config error if that path is
absent.

```bash
sudo systemctl stop klipper moonraker
tar -C "$HOME" -cf "$HOME/klippy-env.pre-kalico.tar" klippy-env
```

### Two things that happen by themselves, and one that does not

- **The C helper rebuilds itself.** `klippy/chelper` compares source mtimes
  against `c_helper.so` and recompiles on the first start after the tree
  changes. Expect a slower first boot; it needs `gcc`, which RatOS has.
- **MCU firmware is not reflashed by the migration.** RatOS reflashes MCUs from
  a git *post-merge* hook, and the migration uses `checkout`/`reset`, which do
  not fire it. Kalico's own migration guide does not call for reflashing, and
  nothing here found a hard host/MCU version gate — but if anything
  MCU-related misbehaves, reflash deliberately from the Kalico tree through
  RatOS' normal flashing flow before looking anywhere else.

**Klippy must start and report ready.** If it does not, the log names the
section. Nothing below matters until this is green.

Then confirm it is really Kalico that started, not just that something did:

```bash
scripts/verify-kalico.sh
```

It separates three questions that can disagree — is the code in `~/klipper`
Kalico, is the *running process* that code, and did it load cleanly. The second
is the one that matters: `printer.py:684` writes `App Name: Kalico` into
`klippy.log` on every process start, and stock Klipper never writes that line at
all, so its absence is an answer rather than an ambiguity.

Read the **last** such block, not the first. Rotation happens only when klippy
runs with `-r` (`printer.py:662`), so the log can hold several blocks from
several starts. `RESTART` and `FIRMWARE_RESTART` reload the config inside the
existing process and write no block at all; only a service restart does.

If there is no block and you believe the update ran, the problem is upstream of
klipper: check which configurator the machine is on, because RatOS' own
migration script will happily report success while keeping the printer on
Klipper.

```bash
git -C ~/ratos-configurator remote -v
git -C ~/ratos-configurator branch --show-current
grep -n 'RATOS_FORK_URL=' ~/printer_data/config/RatOS/scripts/klipper-fork-migration.sh
```

That last line is decisive. If it names `Rat-OS/klipper`, the fork never
reached the machine and what ran was upstream's migration.

Expected new behaviour at this point: Klippy reports itself as **Kalico** in the
web UI. Mainsail may show an "unofficial remote url" anomaly for klipper. Both
are normal.

---

## Stage 2 — the config loads, and every module loaded with it

Kalico imports all of `klippy/extras/` at startup and **swallows import errors
silently**. A clean boot does not mean the modules are alive.

```
QUERY_ENDSTOPS
BEACON_QUERY
```

Then confirm in the console that these respond at all: `RatOS`,
`beacon_adaptive_heat_soak`, `named_offsets`, `beacon_mesh`. Any that were
silently dead at import will fail here rather than mid-print.

### Does the venv survive an update?

This is the check that decides whether the fix holds or has to be re-applied
forever. `ratos-update.sh` pip-installs the configurator's requirements on every
configurator merge, and moonraker re-installs three more files with `-U`:

```bash
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh 2>&1 | tee /tmp/ratos-update.log
~/klippy-env/bin/pip check
~/klippy-env/bin/pip freeze | grep -Ei 'numpy|scipy|pygam|jinja|markupsafe'
```

numpy must still be 1.x. If it moved, the pins are being overridden by another
installer and `docs/RISKS.md` §1 is where to look — do not paper over it on the
printer, fix it in the fork.

---

## Stage 3 — motion. Hand on the E-stop.

Each step, one at a time, watching the machine.

| # | Command | Watching for |
|---|---|---|
| 1 | `M84` | No error. This is the `clear_homing_state` path — it is called on *every* M84 and raises `AttributeError` on an unpatched kinematics. |
| 2 | `G28 X` | Homes, stops at the endstop. |
| 3 | `G28 Y` | Same. |
| 4 | `G28 Z` | **The risky one**, and it *will* fail unless `[stepper_z] homing_retract_dist` is set — see below. Be ready to stop. `docs/RISKS.md` §3. |

**X and Y first is mandatory, not a nicety.** RatOS' `HOME_Z` calls a real
`action_emergency_stop` when X/Y are not homed, because it homes Z in the middle
of the bed and cannot resolve that position otherwise.

**Before the first `G28 Z`, confirm the retract is bounded:**

```bash
grep -n -A8 '^\[stepper_z\]' ~/printer_data/config/printer.cfg
```

`homing_retract_dist` must be there. Without it Kalico's default of 5.0 applies,
the post-homing sample is taken ~7 mm up, outside beacon's model, and homing
aborts with the misleading `Toolhead stopped below model range`. Add
`homing_retract_dist: 1` to that block if it is missing. The fork ships the same
line in `z-probe/beacon.cfg`, but a machine that has not pulled since will not
have it yet.

**What the first `G28 Z` should look like, hand on the e-stop:** a fast descent
to roughly 2 mm above the bed, a short lift, a slow second descent to the same
point, a second short lift of about 1 mm, then a pause of a few seconds while
beacon samples. Stop it if the nozzle keeps descending past the point where it
first stopped, or if it touches the bed at all — neither belongs in a proximity
home.

**Do not trust the resulting Z for a print while the machine is cold.** The saved
beacon model was calibrated hot; homing cold makes beacon's temperature
compensation extrapolate. Home cold to prove the motion is right, then re-home
at printing temperature before the first layer.
| 5 | `G28` (cold, Z unhomed) | Exercises `ratos_homing`'s z-hop path with `z_hop: 15` — the `set_position(homing_axes="z")` fix. |
| 6 | `SET_KINEMATIC_POSITION` | The `force_move` → `clear_homing_state` path RatOS' own belt-tension and shaper macros use. |
| 7 | `T0`, `T1`, then COPY and MIRROR | IDEX. Confirm **both carriages move the direction you expect** before anything else. |

If step 4 or 7 misbehaves, stop and go back. Nothing below is worth a crash.

---

## Stage 4 — probing and mesh

```
PROBE
PROBE_ACCURACY
Z_TILT_ADJUST
BED_MESH_CALIBRATE
```

`Z_TILT_ADJUST` on this machine runs with `retries: 15` and
`retry_tolerance: 0.006` — give it time.

For `BED_MESH_CALIBRATE`, **watch the log for `Timer too close`**. That is the
exact failure the reactor-yield port exists to prevent; if it appears, the port
is incomplete and the failure mode is an MCU shutdown mid-mesh, not a clean
error.

Then the RatOS-specific paths, which are where the `ZMesh` reactor argument
actually gets used:

```
BED_MESH_PROFILE LOAD=compensation_bed_65C
BEACON_APPLY_SCAN_COMPENSATION PROFILE=auto
SAVE_CONFIG        # with a 58x58 mesh pending — this is the save_profile path
```

`SAVE_CONFIG` with a large mesh is the second CPU-hog case the port addresses.

---

## Stage 5 — the rest of the RatOS surface

- `BEACON_RATOS_CALIBRATE` (contact and scan)
- `PROBE PROBE_METHOD=contact`
- `GENERATE_RESONANCES AXIS=X FREQ_START=10 FREQ_END=100` — the only command
  that reaches the `run_test` arity shim. Unpatched, this is a straight
  `TypeError` from Kalico's five-parameter `ResonanceTestExecutor.run_test`.
- `OSCILLATE AXIS=X FREQ=60 TIME=1` — the other patched path in that module,
  and what the configurator's Analysis page drives.
- `GENERATE_SHAPER_GRAPHS` — RatOS' own graph macro. Note this one does *not*
  go through `resonance_generator.py`.
- `MEASURE_COREXY_BELT_TENSION`
- Adaptive heat soak — the pygam path
- A short print with `START_PRINT` / `END_PRINT`

Note that shaper results will not be directly comparable to your old ones unless
`sweeping_period` is pinned; the fork pins it to Klipper's `1.2` precisely so
they are.

---

## Rolling back

There is no rollback script, on purpose. The steps are:

Mirror the stage 1 switch exactly — including the **branch name**. A
`reset --hard` alone leaves the printer on the fork's local branch with
`branch.<name>.merge` pointing at a ref upstream does not have, and Moonraker
can then never pull the configurator again:

```bash
git -C ~/ratos-configurator remote set-url origin https://github.com/Rat-OS/RatOS-configurator.git
git -C ~/ratos-configurator fetch origin
git -C ~/ratos-configurator checkout -B v2.1.x-deployment-2 origin/v2.1.x-deployment-2
sudo systemctl restart ratos-configurator moonraker
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
```

A plain `git fetch origin` works here only because stage 1 widened
`remote.origin.fetch`. If you are rolling back on a machine that never had that
done, widen it the same way first, or the tracking ref will not exist.

Never paste `exit` into an interactive SSH session, in either direction — it
terminates the login shell, and the error you needed to read scrolls away with
the window. Wrap the sequence in `bash -c '...' 2>&1 | tee ~/switch.log`
instead.

The last command runs the *upstream* migration script again, which pulls
`~/klipper` back to `Rat-OS/klipper` at its pinned commit.

### If someone pressed Hard Recover

Moonraker's Hard Recover deletes `~/klipper` outright — every symlink in
`klippy/extras` and `.git/info/exclude` with it. `ratos-update.sh` rebuilds the
links RatOS knows about, `beacon.py` included. It does **not** rebuild
`klipper_tmc_autotune`'s three, because that addon does not register with RatOS:

```bash
sudo systemctl stop klipper
~/klipper_tmc_autotune/install.sh
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
sudo systemctl restart klipper
```

Until those three are back, Klippy will not start: `printer.cfg` declares
`[autotune_tmc]` on seven steppers. `docs/RISKS.md` §11 and §12.

If you changed the venv, restore it from the snapshot — and restore it
*together with* the checkout, so the pair cannot drift. RatOS' klipper pins
`Jinja2==2.11.3` / `markupsafe==1.1.1` where Kalico's file pins `3.1.6` /
`2.1.5`, and Moonraker's klipper entry reinstalls whichever tree is checked
out. Moonraker must be stopped across the whole window, because it validates
that the venv path exists:

```bash
sudo systemctl stop klipper moonraker
rm -rf "$HOME/klippy-env"
tar -C "$HOME" -xf "$HOME/klippy-env.pre-kalico.tar"
~/klippy-env/bin/python ~/klipper/klippy/chelper/__init__.py   # rebuild the C helper
sudo systemctl start moonraker klipper
```

---

## About the 600 printer type

The fork ships **V-Core 4.1 IDEX 600** as a real printer type, and it arrives
with the firmware — both come from the same fork switch in stage 1.

**It changes nothing about this test plan.** The running 600 mm geometry comes
from `printer.cfg`, which wins over the generated config, not from the
configurator; Klippy never reads the printer definition, and nothing regenerates
by itself. So every stage above runs on the machine's existing, correct config.

Regenerating with the new printer type is a separate, deliberate step — do it
after the firmware is validated, and then re-run stage 3 and the stage 4 mesh
checks, because the generated config is not the one you just validated. See
[`PRINTER-600.md`](PRINTER-600.md).

---

## What "it works" means

All of stage 3 and 4 clean, one successful print, and no `Timer too close` in
the log. Until then this is an experiment, and the printer is the experiment.
