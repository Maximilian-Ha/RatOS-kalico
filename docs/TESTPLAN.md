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
git -C ~/klipper log --oneline -1        # the fork's Kalico commit
grep APP_NAME ~/klipper/klippy/__init__.py   # must say Kalico
~/klippy-env/bin/python -c "import numpy,jinja2;print('numpy',numpy.__version__,'jinja2',jinja2.__version__)"
~/klippy-env/bin/python -c "import pygam; print('pygam ok')"
```

**Read the versions, do not just check for a clean exit.** RatOS' own numpy 1.x
and jinja2 2.11.3 import perfectly and are exactly what Kalico cannot run on;
Kalico asks for `numpy~=2.2` and `Jinja2>=3.1.6`.

Nothing installs them for you. The repo switch happens inside
`klipper-fork-migration.sh`, not through Moonraker's updater, so Moonraker never
sees a requirements delta. If the versions are wrong you install Kalico's set
yourself — and understand first that this is the jinja2 jump `docs/RISKS.md` §1
calls the largest open risk, because every RatOS macro is a jinja2 template:

```bash
~/klippy-env/bin/pip install -r ~/klipper/scripts/klippy-requirements.txt
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

The klippy venv does **not** roll back — jinja2 and numpy stay upgraded. If
Klipper misbehaves after a rollback, that is where to look; restore
`~/klippy-env` from the stage 0 backup.

---

## After it works: the 600 printer type

The fork also ships **V-Core 4.1 IDEX 600** as a real printer type, so this
machine can be generated instead of hand-patched. Do not do that during the
firmware migration — switching both at once gives every symptom two suspects.
Once the printer is printing on Kalico, see
[`PRINTER-600.md`](PRINTER-600.md).

---

## What "it works" means

All of stage 3 and 4 clean, one successful print, and no `Timer too close` in
the log. Until then this is an experiment, and the printer is the experiment.
