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

---

## Stage 1 — switch the firmware, do not move anything

Point `~/ratos-configurator` at the fork's deployment branch, restart the
configurator and Moonraker, then run RatOS' own updater:

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

**numpy must be 1.x here, not 2.x.** RatOS pins `pygam==0.9.1`, which caps
scipy below 1.12, and no such scipy supports numpy 2 — so numpy 2 makes
`import pygam` fail, and Kalico takes the printer down at config load over
`[beacon_adaptive_heat_soak]`. The fork pins `numpy>=1.26.4,<2` in both of the
requirements files it owns for exactly this reason. `docs/RISKS.md` §1 has the
full graph.

> **Do not run a bare `pip install -r ~/klipper/scripts/klippy-requirements.txt`.**
> An earlier version of this document said to. On the fork's Kalico branch that
> file is already pinned correctly, so there is nothing to do; against upstream
> Kalico it installs numpy 2 and breaks the machine, exiting 0 while it does.

If you do need to touch the venv, resolve before you install — one dry run over
*all four* requirements files turns a silent downgrade into a loud
`ResolutionImpossible`, without putting the venv at risk:

```bash
~/klippy-env/bin/python -m pip download --only-binary=:all: -d /tmp/wheels \
  -r ~/klipper/scripts/klippy-requirements.txt \
  -r ~/ratos-configurator/configuration/klippy/requirements.txt \
  -r ~/beacon/requirements.txt \
  -r ~/klipper_linear_movement_analysis/requirements.txt
```

Snapshot by **copying**, never by moving: three `moonraker.conf` entries declare
`virtualenv: ~/klippy-env`, and Moonraker raises a config error if that path is
absent.

```bash
sudo systemctl stop klipper moonraker
tar -C "$HOME" -cf "$HOME/klippy-env.pre-kalico.tar" klippy-env
```

**Klippy must start and report ready.** If it does not, the log names the
section. Nothing below matters until this is green.

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
| 4 | `G28 Z` | **The risky one.** Kalico adds a second retract, after the second homing pass, that upstream Klipper does not do. Beacon's model is only valid in a narrow band. Be ready to stop. See `docs/RISKS.md` §3. |
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

```bash
git -C ~/ratos-configurator remote set-url origin https://github.com/Rat-OS/RatOS-configurator.git
git -C ~/ratos-configurator fetch origin v2.1.x-deployment-2
git -C ~/ratos-configurator reset --hard origin/v2.1.x-deployment-2
sudo systemctl restart ratos-configurator moonraker
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
```

The last command runs the *upstream* migration script again, which pulls
`~/klipper` back to `Rat-OS/klipper` at its pinned commit.

The klippy venv does **not** roll back with the repos. Restore it from the
snapshot, and restore it *together with* the checkout — a Kalico tree cannot
boot on the old venv (jinja2 2.11.3, no numpy), and Moonraker must be stopped
across the whole window because it validates that the venv path exists:

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
