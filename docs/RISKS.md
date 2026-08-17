# Risk register

Ordered by what can hurt the machine, not by likelihood. Every entry says what
was actually checked and what was not.

---

## 1. The klippy virtualenv — jinja2 2.11.3 → 3.1.6

**Unquantified. Largest open risk in the project.**

Kalico's `pyproject.toml` requires `Jinja2>=3.1.6` and its exported
`klippy-requirements.txt` pins `jinja2==3.1.6`, `markupsafe==2.1.5`,
`python-can==4.6.1` and numpy 2.x. RatOS' Klipper base pins `Jinja2==2.11.3`,
`markupsafe==1.1.1`, `python-can==3.3.4` and no numpy at all; the RatOS image
separately pins `numpy<=1.23.4` into `~/klippy-env`.

Every RatOS macro is a jinja2 template. The 2.x → 3.x jump changes template
semantics. Nobody has run RatOS' macro set against jinja2 3.1.6.

Worse, the upgrade happens **out of band**: the repo switch is done by
`klipper-fork-migration.sh`, not by Moonraker's updater, so Moonraker never
sees a requirements delta and never installs Kalico's dependencies. Something
has to do it explicitly or Klippy fails to import.

`pygam==0.9.1` — which `beacon_adaptive_heat_soak.py` needs — is old and its
numpy-2 build path on Debian Bookworm is unverified.

**Check before migrating:** `scripts/preflight.sh` reports the installed numpy
and jinja2 versions and whether pygam imports. Note that a bare
`import numpy, jinja2, pygam` passing means nothing here — the versions RatOS
ships today import perfectly well and are exactly the ones Kalico cannot use.
Read the reported versions, do not just check for a clean exit.

---

## 2. Kalico imports every klippy extra at startup

Klipper imported a module only when its config section appeared. Kalico walks
`klippy/extras/` at startup and imports **everything** — all fourteen symlinked
RatOS modules, `beacon.py`, numpy and pygam included — regardless of what is in
`printer.cfg`.

Two consequences:

- pygam must be installable in the klippy venv for *every* RatOS-on-Kalico
  user, not just those using adaptive heat soak.
- An import failure is **silent at startup**. It is stashed on the module object
  and only surfaces later, when something tries to use it.

Worth adding a boot-time check that walks `printer.printer_modules` for a
non-`None` `.exception`.

---

## 3. Kalico retracts a second time after homing

Kalico's `home_rails` retracts twice — once before the second homing pass
(`homing.py:339`, which upstream Klipper also does at `homing.py:201`) and once
*after* it (`homing.py:382`, `# Retract (again)`), which upstream does not.
Upstream retracts once; Kalico retracts twice.

The extra one is gated only on `hi.retract_dist` — there is no dedicated
option — so the only lever is `homing_retract_dist: 0`, which also removes the
accuracy-improving second pass.

The gating is subtler than it looks: the *outer* gate uses a widened
`retract_dist` that `min_home_dist` can raise, so an explicitly set
`min_home_dist` can re-enable the second pass even with
`homing_retract_dist: 0`. RatOS' sensorless templates are safe only because
`min_home_dist` *defaults* to `homing_retract_dist`.

For this printer X/Y home on physical endstops and Z homes on the Beacon
virtual endstop. Beacon's model is valid only in a narrow approach band. Whether
the extra move breaks `G28 Z` was **not** determined — only that the code
difference is real.

**This is why `G28 Z` is the first item in the test plan.**

---

## 4. Homing current switching versus `[autotune_tmc]`

Kalico calls `_set_homing_current` on *every* `home_rails`, even with no
`home_current` configured. It tracks driver current in its own `actual_current`
state. `[autotune_tmc]` writes IRUN/IHOLD registers directly, out of band, which
leaves that state stale — so Kalico can decide a current change is needed when
it is not, and rewrite currents mid-home.

The user has seven `[autotune_tmc]` sections including `dual_carriage`. There is
a reported Kalico bug in this area
([kalico#829](https://github.com/KalicoCrew/kalico/issues/829)), but it was
filed against TMC2209 drivers on **CAN** toolboards, which this printer is not:
its Orbitool O2s toolboards are USB serial and run TMC2240
(`RatOS_4.1.cfg:565`, `:586`). The TMC2209s here are the three Z steppers on the
Octopus. Listed as prior art, not as a match.

Not a blocker — autotune loads and runs on Kalico — but the interaction is
unverified and it is a *motion* risk, not a cosmetic one.

---

## 5. `ProbePointsHelper` co-opts option names

Kalico's `ProbePointsHelper` unconditionally builds a `RetrySession` →
`RetryPolicy` → `GcodeNozzleScrubber` chain, which reads seven extra options out
of **whatever section RatOS passes it** — `[beacon_mesh]`,
`[beacon_true_zero_correction]`, `[z_tilt]`.

Note `speed` is read twice in the same section with different defaults: `5.0` by
`RetryPolicy`, `50.0` by `ProbePointsHelper` itself. Any RatOS option named
`speed`, `retry_speed`, `pattern_spacing`, `bad_probe_retries` or
`scrubbing_frequency` in those sections now has a second, different consumer.

Not audited. Worth grepping before the first mesh.

---

## 6. `METHOD=` routing changed

Kalico's `ProbePointsHelper.start_probe` routes to **manual** probing for any
`METHOD` value that is not exactly `automatic`; Klipper only did so for
`METHOD=manual`.

RatOS is safe today because it uses `PROBE_METHOD=`, not `METHOD=`. But any user
macro passing `METHOD=proximity`, `METHOD=scan` or `METHOD=contact` to
`BED_MESH_CALIBRATE` or `Z_TILT_ADJUST` now silently drops into
`ManualProbeHelper` instead of erroring.

---

## 7. Regeneration destroys this machine's config

`RatOS_4.1.cfg` says it is generated and will be overwritten. It has been
hand-edited in at least four places:

- `[include Custom_settings/600_idex.cfg]`, with the stock `500.cfg` include
  commented out — this is what makes it a 600 mm machine at all
- `run_current: 1.2` (from 1.4)
- `variable_parking_position: -73` (from −55) and `672` (from 670)
- `variable_has_front_arm_nozzle_wiper: True`, where the generator emits `False`

The header still reads *"Config generated for Rat Rig V-Core 4.1 IDEX 500"*.

**Any** regeneration reverts the machine to a 500 with the wrong parking
positions. This is not a Kalico risk — it is true today — but a migration is
exactly the moment someone re-runs the configurator.

Copy those edits into `printer.cfg` first.

---

## 8. `pinned_commit` is a one-way door

If the fork's Kalico branch is force-pushed and a previously pinned commit
disappears, Moonraker does not error. It leaves `upstream_commit ==
current_commit` and reports klipper **"up to date" forever**, with only a small
anomaly note.

`build-kalico-fork.sh` uses `--force-with-lease` and warns about this. Never
force-push away a commit a printer has already pinned.

---

## 9. Unrelated git histories

Migrating an existing box rewrites `~/klipper` from Klipper history to Kalico
history. These are unrelated trees. `checkout -b <branch> <remote>/<branch>`
works, but leaves a large orphaned object set behind, and the old local
`ratos/v2.1.x` branch lingers.

A fresh clone would be cleaner than an in-place migration for this one jump.
Not implemented.

---

## 10. Two upstream bugs the fork inherits

Found while reading, not caused by us, and not fixed here:

- **`fix_klipper_ownership` never chowns anything.** Its detection uses
  `find ... -quit` without `-print`, so the variable it tests is always empty.
  The function always logs success and does nothing. Harmless today only
  because all git work runs through `sudo -u`. Do not "fix" it casually —
  enabling it turns a live `chown -R` loose on `~/klipper` on every update.
- **`.augment/env/setup.sh` cannot read `pinned_commit`.** It uses `grep -A1`
  where the value is on the third line, so `KLIPPER_COMMIT` is empty and the dev
  checkout silently stays on upstream. Already broken before any fork.

---

## What was checked and found fine

Recorded so nobody re-litigates them:

- **Beacon.** `BeaconProbeWrapper` implements both the legacy protocol Kalico
  drives and the newer session API, and `run_probe(self, gcmd, *args, **kwargs)`
  absorbs Kalico's extra positional argument.

  With one caveat worth stating: `[update_manager beacon]` is `channel: dev`
  with **no `pinned_commit`**, so beacon.py is auto-updated on the printer by an
  updater this fork does not control. On stock RatOS that is harmless — its
  Klipper has the session API beacon prefers. Here the *legacy* path is
  load-bearing, so a future beacon release that drops it would break probing
  with no warning. Consider pinning it.
- **`klipper_tmc_autotune`.** Ships `from klippy.extras import tmc  # Kalico`
  and indexes `get_current()` rather than unpacking it.
- **Flat imports.** Kalico's `klippy/compat.py` is a *generic* meta-path hook —
  it rewrites any top-level module name that exists under `klippy/`, not a fixed
  list. `import chelper`, `import pins`, `from mcu import MCU`,
  `import configfile` and friends all resolve.
- **Module name collisions.** Exactly one, `gcode_shell_command.py`, handled.
  `ratos_hybrid_corexy` does not collide with Kalico's kinematics.
- **Config option coverage.** Among sections Kalico implements, `log_points` was
  the only unknown option in the whole resolved include tree — and the port
  introduces it.

---

## 11. The Moonraker recovery alias does not fully work

`fork.conf` publishes a `master` branch alongside the fork's Kalico branch so
Moonraker's Recover buttons have something to check out. That helps, but it does
not fully close the hole.

The migration script never repoints `origin` — it adds a second remote and
resets the branch. So after migration `~/klipper` still has
`origin = https://github.com/Klipper3d/klipper.git` and a local `master` left
over from the image, pointing at vanilla Klipper.

Consequences: **Hard Recover** resolves its clone URL from `origin`, so it would
reinstall stock Klipper. **Soft Recover** checks out the stale local `master`.
The alias branch only helps a machine that was cloned from the fork in the first
place.

Fixing it properly means repointing `origin` in the migration script, which
changes what Moonraker's klipper entry sees — an interaction that has not been
verified. Until then: do not use Mainsail's Recover buttons on a migrated
machine. Re-run `ratos-update.sh` instead.
