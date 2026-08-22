# Risk register

Ordered by what can hurt the machine, not by likelihood. Every entry says what
was actually checked and what was not.

---

## 1. The klippy virtualenv — pygam forbids numpy 2

**This was originally written up as a jinja2 risk. That was wrong.** The jinja2
jump is close to a non-risk (see below). The real problem is a dependency graph
with no solution, and on this printer it is a boot blocker.

### The conflict

RatOS pins `pygam==0.9.1`. pygam's own metadata caps `scipy>=1.11.1,<1.12`, and
every scipy in that window declares `numpy>=1.21.6,<1.28`. So **pygam
transitively forbids numpy 2.** Kalico asks for `numpy~=2.0`.

There is no version pair that satisfies both. pygam 0.9.1 is the newest tag the
project has ever published.

### Why it takes the printer down rather than degrading

- Kalico imports numpy at module level in `webhooks.py`, which `printer.py`
  imports — so numpy is boot-critical for the first time.
- Kalico eagerly imports **every** module in `klippy/extras/` at startup.
  `beacon_adaptive_heat_soak.py` imports pygam at module scope.
- The import error is *stashed*, not raised — and then re-raised the moment the
  section is loaded. `[beacon_adaptive_heat_soak]` is declared unconditionally
  in `z-probe/beacon.cfg` and this printer includes it.

So Klippy never reaches ready. And pip will have exited 0, printing the
conflict only as a trailing warning.

### What the fork does about it

Pins numpy below 2 **in both files it owns**, because they otherwise fight:

| File | Pin |
|---|---|
| the Kalico branch's `scripts/klippy-requirements.txt` and `pyproject.toml` | `numpy>=1.26.4,<2` |
| `configuration/klippy/requirements.txt` | `numpy>=1.26.4,<2`, `scipy>=1.11.1,<1.12`, `pygam==0.9.1` |

Holding numpy at 1.26 is safe for Kalico: its entire numpy surface — `array`,
`std`, `mean`, `interp`, `cumsum`, `outer`, `zeros`, `maximum`, `float64`,
`linalg.solve`, `linalg.lstsq`, `lib.stride_tricks.as_strided`, `bool_`,
`kaiser` and friends — behaves identically on 1.26, and a sweep of Kalico,
the RatOS klippy modules, beacon and autotune for numpy-2-only APIs
(`np.trapezoid`, `np.isdtype`, `np.vecdot`, `np.strings`, `np.exceptions`, …)
returns nothing. Kalico's numpy 2 pin is what its lockfile resolved, not what
its code needs.

Both sides have to move together. Pinning only one reintroduces the ping-pong.

### Why pinning matters more than a one-off fix

**Four** pip runs write this venv, none with `--no-deps` or a constraint file:
`ratos-update.sh` on every configurator merge, and moonraker's update_manager
entries for klipper, beacon and LinearMovementAnalysis — the latter three with
`-U -r`. Whichever ran last wins.

Note beacon's own `requirements.txt` asks for unbounded `numpy>=1.16.6` and
`scipy>=1.2.3`. That means **this can break a stock RatOS 2.1 box today**, with
Kalico nowhere in the picture: a beacon requirements delta, or a Recover with
dependencies, can pull numpy past what pygam tolerates. The fork does not own
beacon's file, so that hazard remains.

A hand-fix on the printer is therefore not durable. The pins have to be in the
repo, which is why they are.

### The way out, once it exists

pygam's `main` branch declares version 0.10.1 with `scipy>=1.11.1,<1.17` and
`numpy>=1.5.0`, which would dissolve the conflict entirely and allow numpy 2.
But **no 0.10.1 tag exists** in the repository, and PyPI could not be reached
from the build environment to confirm a release. Check from the printer:

```bash
~/klippy-env/bin/pip index versions pygam
```

If 0.10.1 or later is really published, bump `configuration/klippy/requirements.txt`
and drop the numpy ceiling on both sides. Its `LinearGAM.__init__` signature is
unchanged from 0.9.1, so the call in `beacon_adaptive_heat_soak.py` is a drop-in.

### Python version — read it, do not assume it

Every marker-gated pin above branches on the interpreter. RatOS 2.1 images are
built on **Raspberry Pi OS / Armbian Bullseye**, i.e. **Python 3.9**, and the
venv is created with a bare `virtualenv -p python3` at image build. There is no
dist-upgrade anywhere in the update scripts.

*Upstream* Kalico's markers select numpy 2.0.2 on 3.9 rather than the 2.2.2 they
select on 3.10+ — still numpy 2, still the same conflict. The fork's branch
replaces that line with `numpy>=1.26.4,<2 ; python_full_version < '3.13'`, so a
printer built from this fork resolves to 1.26.x. Note also that on the
recommended 32-bit armhf image numpy 2.x may have no wheel at all.
`scripts/preflight.sh` now prints the OS, architecture and interpreter first,
because every number here depends on them.

### What about jinja2?

Close to a non-risk, and worth stating so nobody spends effort there:

- Exactly **two** files in the whole tree import jinja2 — the two
  `gcode_macro.py` implementations. Beacon, autotune and all thirteen RatOS
  klippy extensions import zero jinja2 symbols; they go through
  `load_template()`. Since Kalico is what runs, only Kalico's own needs matter,
  and it uses a tiny surface that 3.1.6 preserves verbatim.
- Nobody registers custom filters, tests, globals or extensions anywhere.
- All 270 `gcode:` templates in the RatOS config tree and this printer's own
  config were parsed under both 2.11.3 and 3.1.6: **identical AST inventories,
  identical filter and test counts, zero render differences**, including
  character-identical error messages.

The one caveat: **never upgrade markupsafe without jinja2**. markupsafe 2.x
removes `soft_unicode`, which jinja2 2.11.3 needs. A single `pip install -r`
moves them together; hand-picking lines does not.

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
- **Module name collisions.** Exactly one, `gcode_shell_command.py`, handled;
  `ratos_hybrid_corexy` does not collide with Kalico's kinematics. The state
  of that one symlink turned out to matter more than the collision itself —
  see §12.
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

**Hard Recover destroys more than the checkout.** It clones into a backup
location and then `shutil.rmtree`s `~/klipper` — which takes with it every
symlink in `klippy/extras` *and* `.git/info/exclude`. RatOS rebuilds the links
for extensions registered with it, `beacon.py` among them, on the next
`ratos-update.sh`. It does not rebuild `klipper_tmc_autotune`'s three, because
that addon does not register with RatOS: `autotune_tmc.py`, `motor_constants.py`
and `motor_database.cfg` have to be re-created by hand, or by re-running
`~/klipper_tmc_autotune/install.sh`. Until they are, Klippy will not start —
`printer.cfg` declares `[autotune_tmc]` on seven steppers. See §12.

---

## 12. Symlinks in `klippy/extras`, and the one path Kalico takes over

RatOS and every third-party addon install their klippy modules by symlinking a
file from their own checkout into `~/klipper/klippy/extras`. The migration
repoints that whole directory's repository. What happens to the links is not
uniform, and the difference matters more than it looks.

### The mechanism

`klipper-fork-migration.sh` touches the working tree in exactly four places —
`checkout -b "$temp_branch"` (:590), `checkout "$TARGET_BRANCH"` (:601),
`checkout -b "$TARGET_BRANCH" "$RATOS_FORK_REMOTE/$TARGET_BRANCH"` (:608) and
`reset --hard "$TARGET_COMMIT"` (:650). There is no `git clean`, no `stash`, no
`rm`, `mv` or `cp` anywhere in the script. Git deletes an untracked path only
when a **tracked** path in the target tree collides with it.

So the question reduces to: which basenames does Kalico track that RatOS or an
addon also links in? Of the 185 files Kalico tracks under `klippy/extras` and
`klippy/kinematics`, **two**:

| Path | Who else supplies it |
|---|---|
| `gcode_shell_command.py` | RatOS, as a registered extension |
| `belay.py` | a standalone Belay install — Kalico integrated the module natively |

`belay.py` is the reason this list is derived rather than written down once.
Nothing about the fork suggested it; it surfaced only when a real printer's
`printer.cfg` turned out to declare `[belay my_belay]`.
`tests/check_collisions.py` re-derives the set from the built forks and fails
if it ever changes, because `scripts/preflight*.sh` has to hardcode it.

`autotune_tmc.py`, `motor_constants.py`, `motor_database.cfg`, `led_effect.py`
and `beacon.py` do not collide, and therefore survive both the checkout and the
`reset --hard`. Their targets are outside `~/klipper`, so repointing the
repository cannot dangle them either.

### The inversion: being *excluded* is the dangerous state

For a path that does collide, everything depends on whether it appears in
`~/klipper/.git/info/exclude` — which RatOS writes when it registers an
extension. Reproduced on git 2.43:

| in `.git/info/exclude` | `git checkout -b` does |
|---|---|
| yes | silently replaces the symlink with Kalico's file |
| no | `error: The following untracked working tree files would be overwritten by checkout … Aborting` |

The second case is not a one-off. It surfaces as `GIT_CHECKOUT_REMOTE_FAILED`,
`checkout_target_branch` returns 1 and the migration returns 6 — and since the
migration never repoints `origin` (§11), it re-runs and fails identically on
**every** subsequent update. The printer never reaches Kalico, and no amount of
retrying changes that.

This inverts the intuition. A symlink listed in the exclude file is the one
that gets destroyed; a symlink *not* listed is the one that blocks the
migration. Neither state is visible without looking.

### What the fork does about it

Two things, because the preflight alone cannot fix a machine.

`configurator/patch_configurator.py` adds `t_migration_yield_kalico_owned`,
which removes those symlinks — guarded on `-L`, so it can only ever remove a
link and never a real file or the source under `printer_data` — immediately
before the checkout. Both states then converge on the good one. After the first
migration the path is a regular tracked file and the guard makes it a no-op,
which matters because this script runs on every update.

`scripts/preflight*.sh` reports every symlink under `klippy/`, fails on a
dangling one, and fails specifically on a colliding-but-unexcluded one with the
two commands that fix it. The fork cedes `gcode_shell_command.py` to Kalico
deliberately — see `t_drop_gcode_shell_extension` — so the replacement
itself is expected, and is reported as a note rather than a failure.

### What happens on the updates after that — checked, and it holds

Dropping the extension from `expected_extensions` does not unregister it. The
verify loop in `ratos-common.sh` reaches `[[ ! -v expected_extensions[...] ]]`,
prints `WARNING: Unexpected klipper extension found`, and `continue`s — the
entry stays in the persisted registry, so `symlinkExtensions` keeps iterating it
on every update. The obvious worry is that it re-links the symlink over Kalico's
now-tracked file, which would make `git diff-index` see a modified path and
abort every later migration with `KLIPPER_UNCOMMITTED_CHANGES`.

It does not. `extensions.ts:44` computes `existsSync(destination)`, which is
**true** for Kalico's regular file, and `:50` only creates the link when that is
false. The sweep reports "already exists. Skipping." and leaves the tracked file
alone. RatOS' own verifier is satisfied too, because it inspects the source path
in `printer_data`, which the fork still ships.

One side effect is worth knowing, because it explains why the missing-exclude
failure above is rare rather than routine: `:58` appends the exclude line
whenever it is missing, whether or not it created a link. So any machine that
has completed a configurator update tends to have the line already. It does not
help on the run that matters, though — `ratos-update.sh` invokes
`klipper-fork-migration.sh` before the configurator's symlink sweep, so a
machine that lost its exclude file aborts in the migration before anything can
repair it. Hence the guard in the migration script rather than reliance on this.
