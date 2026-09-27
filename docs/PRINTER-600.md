# The V-Core 4.1 IDEX 600 printer type

RatOS offers this machine at 300, 400 and 500. This one is a 600. Today that is
handled by picking 500 in the configurator and hand-patching the result — which
costs the ability to regenerate at all: the generated `RatOS.cfg` had to be
renamed to `RatOS_4.1.cfg` so the configurator would stop overwriting it.

The fork ships 600 as a real printer type instead.

## Why a printer type and not just a size

`bedMargin` is a **per-printer** field — the definition schema only allows
`x`/`y`/`z` inside a size entry. This frame's margins are not the stock ones:

| | stock | this machine |
|---|---|---|
| `bedMargin.x` | `[60.6, 60.6]` | `[75, 75]` |
| `bedMargin.y` | `[14.35, 33.65]` | `[1, 65]` |

And `bedMargin` is what the configurator derives from. A 600 *size* on the stock
printer would keep generating wrong numbers — including into the
always-overwritten `RatOS.cfg`:

| Value | with stock margins | real |
|---|---|---|
| `variable_bed_margin_x` / `_y` | `[60.6, 60.6]` / `[14.35, 33.65]` | `[75, 75]` / `[1, 65]` |
| T0 parking position | −58.6 | **−73** |
| T1 parking position | 658.6 | **673** |
| `stepper_x.position_min` | −60.6 | **−75** |
| `dual_carriage.position_max` | 660.6 | **675** |
| `stepper_y.position_max` | 633.65 | **665** |

A cross-check that the derivation is right rather than merely self-consistent:
`bedMargin.x[0] = 75` produces T0 parking at `-75 + 2 = -73`, which is exactly
the value running on the machine today.

## Where the files go, and why those places

| File | Why there |
|---|---|
| `configuration/printers/v-core-4-1-idex-600/printer-definition.json` | Definitions are globbed at runtime as `printers/*/printer-definition.json`, and the printer id comes from the directory name. |
| `.../v-core-4-1-idex-600/v-core-4-idex.png` | The configurator expects the image next to the definition. Copied from the stock printer at build time. |
| `configuration/printers/v-core-4-1-idex/600.cfg` | **The stock folder, not the new one.** The shared template hardcodes `[include RatOS/printers/v-core-4-1-idex/${size}.cfg]`. |
| `.../v-core-4-1-idex-600/printer.cfg.overrides` | Documentation that ships to the printer, next to the definition. |
| `.../v-core-4-1-idex-600/maintenance.cfg` | Optional service macros the operator includes from `printer.cfg`. Shipped rather than pasted so they follow updates. |
| `.../v-core-4-1-idex-600/power-sensors.cfg` | The watt sensors. Included by `600.cfg` like the service macros. |
| `configuration/klippy/heater_power.py` | The sensor type those sections use. RatOS symlinks registered extensions from there into `~/klipper/klippy/extras`. |

The definition points `"template"` at the stock `v-core-4-1-idex.ts`, so no new
template has to be bundled — only definitions are read at runtime.

`scripts/build-configurator-fork.sh` cross-checks the definition against
`600.cfg` on every build and refuses to commit if they disagree. If those two
drift apart the printer homes into its own frame, so this is a build gate, not
a lint.

## Three things the definition cannot express

The shared template emits these *after* the size include, so `600.cfg` cannot
win. `printer.cfg` is included at the top of the generated config, so its
sections come last and do win — which is where they belong anyway, being
per-machine calibration.

The full text ships as `printer.cfg.overrides` in the printer's directory. In
short:

| Override | Why |
|---|---|
| `[gcode_macro _VAOC]` camera positions | The template computes Y as `size.y + 30.5`, where the 30.5 is hardcoded against the stock `bedMargin.y[1]` of 33.65. At 65 it lands ~32 mm short. |
| `[stepper_z] position_min: -5` | Template hardcodes `-7`. |
| `[dual_carriage] safe_distance: 60` | Template hardcodes `55`. |

Optionally `[gcode_macro T1] variable_parking_position: 672` — the configurator
computes 673, one millimetre further out than what runs today.

## The service macros

`maintenance.cfg` ships alongside the definition and defines the macros
Mainsail lists as buttons.

### MAINTENANCE_MODE — into the service position

| Step | Where | Why that value |
|---|---|---|
| bed to mid height | `Z327.5` | half of the 655 mm Z travel. Moved **first**: Z only ever increases the gap between nozzles and bed, so every later move starts with clearance. |
| gantry to the front | `Y4` | `axis_minimum.y` is −1, plus a 5 mm margin off the limit. |
| toolheads centred | T0 `X265`, T1 `X335` | both cannot sit on the bed centre at X300 — `safe_distance` is 60 mm — so they straddle it, 70 mm apart. |

It homes what is unhomed (`MAYBE_HOME`), refuses to run while a print is
printing or paused, and drops out of copy/mirror mode first, because the
carriages move as a pair in those and `PARK_TOOLHEAD` is a no-op.

### MAINTENANCE_END — back out of it

`G28` (not `MAYBE_HOME`: hands were on the machine, so the kinematic position
is fiction), bed to `Z20`, gantry to the back at `Y585`, both toolheads back on
their parking positions.

It **refuses to re-home while a hotend is above 60 °C**. `G28 Z` takes its
reference with the nozzle right above the bed, and a hot nozzle oozes into
exactly that — the drop lands in the Z reference and on the bed.
`TEMP_LIMIT=` overrides it for one call when you know the nozzle is clean.

### NOZZLE_CHANGE — present a nozzle, hot

Bed and gantry as in the service position, then the chosen carriage comes
**200 mm in from its own parking position** — T0 to `X127`, T1 to `X473` — while
the other stays parked. Not the bed centre: 200 mm in is at the near corner of
the frame, where a wrench fits. The nozzle is heated to **300 °C**.

`T=` picks the toolhead (default: the active one), `TEMP=` and `DISTANCE=`
override the rest. The machine homes **before** heating starts, for the same
reason `MAINTENANCE_END` refuses to home hot. A loaded filament sensor produces
a warning, not a refusal — a hot pull wants exactly that state.

`NOZZLE_CHANGE_END` turns the heater off and cancels the timeout. If nobody
does, a `delayed_gcode` turns it off after 15 minutes: Klipper's own
`idle_timeout` is two hours in RatOS, far too long to leave 300 °C unattended
because someone walked away mid-change.

### LUBE_X / LUBE_Y / LUBE_Z — guided greasing

Each run walks its axis through **three stations** and stops at every one so
you can apply grease, then sweeps the full travel twice to spread it, ending
where it started:

| Axis | Station 1 | Station 2 | Station 3 |
|---|---|---|---|
| Z | `Z10`, bed at the top | `Z327.5` | `Z645`, bed at the bottom |
| Y | `Y9`, gantry at the front | `Y299.5` | `Y590`, gantry at the back |
| X | `X−65`, both toolheads left | `X265` | `X595`, both toolheads right |

**Every number in that table is derived, not written down.** The ends are the
axis limits minus a 10 mm margin, the middle is the midpoint of the two. Change
the printer size and the stations move with it — which is the point, since the
same file has to work if the frame ever changes.

Three of those derivations are deliberate rather than mechanical:

- **Z counts from 0, not from `axis_minimum`** (−5). Below zero is probe
  territory, and it is not where a hand holding a brush belongs.
- **Y stops at `printable_y_max`**, not at the mechanical limit of 665. The last
  stretch of Y travel is where the VAOC camera sits; that area is entered
  deliberately by the VAOC macros or not at all.
- **An X station is the *left* carriage's position.** The right one follows a
  `safe_distance` + 10 mm behind, so the pair moves as one block and the far
  station is the right limit minus the margin *and* the spacing.

Small Z means the bed is **up** — Z is the nozzle-to-bed distance, and getting
that backwards is the easiest mistake to make here. The dialog therefore says
"bed at the top", not just a number.

**Continuing a run.** Mainsail and Fluidd render `action:prompt_*` as a dialog,
so each stop shows a **Continue** button — which sends nothing more magic than
the command `LUBE_NEXT`. Typing `LUBE_NEXT` in the console does exactly the
same, and on a frontend without dialog support the prompt lines are simply
console output. Nothing depends on the dialog existing. `LUBE_ABORT` drops the
run where it stands.

Before every move the macro echoes what it is about to do and then dwells three
seconds (`variable_move_delay`), because the operator's hands are in the machine
by definition. Before the run starts, whatever is not being greased is moved out
of the way: for Z the gantry goes to the back and both carriages park, for X and
Y the bed drops to mid height.

An X station goes through `PARK_TOOLHEAD` like everything else here. That costs
one traverse per station and buys not having to know where the carriages were —
moving a pair to new positions in the wrong order trips `safe_distance`. On the
sweeps that traverse is not even waste; it spreads grease.

### LUBE_STATUS / LUBE_MARK — the lubrication record

`LUBE_STATUS` answers "when did I last grease this, and how much has been
printed since":

```
LUBE | Print hours on this machine: 412.3
LUBE | X: greased at 375.0 h -- 37.3 print hours ago (62.7 h to go)
LUBE | Y: never greased, or the record was lost. LUBE_Y runs it, LUBE_MARK AXIS=Y records one you did by hand.
LUBE | Z: greased at 300.0 h -- 112.3 print hours ago (OVERDUE by 12.3 h)
last greased x: 2026-09-04 15:12
```

A finished `LUBE_X/Y/Z` run records itself. `LUBE_MARK AXIS=X|Y|Z|ALL` records
a greasing done by hand.

**The unit is print hours, not calendar time.** A machine that stood still for a
month has not worn its rails. `variable_interval_hours` (default 100) is what
"overdue" is measured against — it is a placeholder, not a specification. Set it
to what your rails and your grease actually want.

**Klipper has no clock, and no lifetime print counter.** A macro's whole world
is `printer`, the `action_*` helpers and `math`; `print_stats` counts only the
job in progress. So:

| What | How |
|---|---|
| print hours | a `delayed_gcode` samples `print_stats.print_duration` every 60 s and accumulates the deltas. A sample *below* the last one means the next job started, not time running backwards. |
| persistence | `SAVE_VARIABLE`, into the same `[save_variables]` file RatOS keeps filament state in. Writes are batched — once 6 minutes of print time have piled up, or when printing stops — because each write rewrites the whole file. |
| the calendar date | two `[gcode_shell_command]` sections. One writes `date` into a stamp file when an axis is recorded, the other prints the stamps into the console. The date is **displayed, never computed with**: a shell command's output goes to the console and nowhere a macro can reach. |

The sampling loop does **not** keep the machine awake. Klipper's `idle_timeout`
measures idleness at the toolhead's move queue (`est_print_time - print_time`),
not "some command ran", and nothing in the loop queues a move — so the two-hour
heater timeout still fires.

**The counter starts at zero when you install this.** It is print hours *since
the record existed*, not the machine's life. If you want it to match reality,
read the machine's total print time off Moonraker's history page and seed it
once:

```
SAVE_VARIABLE VARIABLE=lube_print_hours VALUE=812.5
```

Set `variable_show_dates: False` on `LUBE_STATUS` if you drop the two shell
command sections; everything else keeps working without them.

### PID_TUNE_BEDS / PID_TUNE_BED — the four bed zones

The bed is one plate over four heaters: `heater_bed`, which Klipper owns, and
`BED_VR`, `BED_HL` and `BED_HR` as `[heater_generic]` sections. Klipper tunes
one heater at a time, so `PID_TUNE_BEDS` runs them in sequence and ends with
**one** `SAVE_CONFIG` prompt. That is not cosmetic: each `PID_CALIBRATE` parks
its result in Klipper's pending config (`heaters.py` → `save_profile` →
`configfile.set`), so a single save writes all four and the printer restarts
once instead of four times. A save between runs would restart mid-procedure and
drop the rest.

`PID_TUNE_BED ZONE=1..4` (or `ZONE=BED_HL`) tunes a single zone. `TEMP=`
overrides the calibration temperature, `COOL_BELOW=0` skips the waiting.

Two things decide whether the resulting numbers describe anything real:

- **The plate is shared.** A zone tuned while its neighbours are hot is a
  different thermal system than the same zone on a cold plate. Every run
  therefore switches all four zones off and waits for the whole plate to fall
  below `variable_cool_below` (40 °C) first. On a 600 plate that wait, not the
  tuning, is the long part — budget hours for a full run.
- **`PID_CALIBRATE` writes the heater's target directly.** If anything on the
  machine mirrors `heater_bed` onto the other three — and something does, or
  the zones would never heat together — it will copy the calibration onto the
  whole plate, and the numbers then describe a different system. The macro says
  so before every run, and names `bed-zones.cfg` if that file is installed.

The zone names live in `variable_zones` on `PID_TUNE_BEDS` and any that the
printer does not have are skipped, so this degrades to a plain one-heater bed
without editing anything.

One naming detail is load-bearing and easy to get backwards: `PID_CALIBRATE`
and `SET_HEATER_TEMPERATURE` take the **bare** heater name (`BED_VR`), while
`TEMPERATURE_WAIT` matches the **full section name** (`heater_generic BED_VR`)
— `heaters.py` looks the first two up through `lookup_heater`, which strips a
section prefix, and checks the third against `available_sensors`, which holds
`config.get_name()`. Getting it wrong is an error hours into the procedure.

### BLOW_BED — the plate, before the mesh

T0 sweeps the plate at `Z25` with its part fan at full speed, in a serpentine
one line per 50 mm of Y, to get dust and filament crumbs off. Run it **before
the bed mesh and before the heat soak**: the mesh should measure the plate and
not a crumb, and a soaked plate is one you no longer want to reach across.

`Z=`, `SPACING=`, `SPEED=` and `PASSES=` override the defaults for one call.

Three details are deliberate:

- **The fan is addressed by name** (`SET_FAN_SPEED FAN=part_fan_t0`), not
  through `M106`. `M106` routes to whichever toolhead RatOS considers active,
  and the macro explicitly selects carriage 0 for the sweep — addressing the fan
  by name is what guarantees the head that blows is the head that moves.
- **The sweep direction alternates across the whole run, not per pass.**
  Counting per pass would send the second pass's first line to the end the head
  is already parked at, and that line would be travel rather than sweep.
- **The area comes from the printable area**, not the axis limits. Past it is
  frame, and blowing there only moves the dust onto the rails.

A hot nozzle gets a warning rather than a refusal — it drips on the plate you
are cleaning, but the cleaning still works. If the named fan does not exist the
macro says which variable to change instead of sweeping with nothing running.

It leaves T0 as the selected carriage, which is also where a fresh `G28` leaves
the machine, so the mesh and the heat soak can follow directly.

### The watt display

The interface shows each heater's duty cycle as a percentage and has no field
for watts. `power-sensors.cfg` turns the same duty cycle into power and hands it
over as a `[temperature_sensor]`, which the interface does render live and
graph:

| Sensor | Heater | Rating |
|---|---|---|
| `W_heater_bed`, `W_BED_VR`, `W_BED_HL`, `W_BED_HR` | the four bed zones | 600 W each |
| `W_chamber` | `chamber_heater` | 1500 W |
| `W_total` | all five | 3900 W at once |

The `W_` prefix groups them in the panel and puts the unit where it can be read,
because **the panel labels every sensor in degrees** — there is no way to tell
it otherwise from a config. `1850` on `W_total` means 1850 watts.

**The numbers are derived, not measured.** Nothing senses current or mains
voltage: it is rated power times the fraction of time the heater is switched on.
An aged element, or a mains that sags when 3900 W come on at once, moves the
real figure without moving this one. It answers "what is the printer asking
for", not "what is the meter reading". The totals also cover those five heaters
and nothing else — no steppers, no electronics, no hotends (add them to
`W_total`'s two lists if you want them; they are roughly 60–100 W each).

#### How it gets there

`klippy/heater_power.py` registers a Klipper *sensor type*, the same way
`temperature_combined` does — `load_config` calls
`pheaters.add_sensor_factory("heater_power", …)`. That is why `power-sensors.cfg`
opens with a bare `[heater_power]` section: a `[temperature_sensor]` does not
load the module that implements its `sensor_type`, and the factory has to be
registered before the sections that use it are parsed.

Installation is an update and nothing else. `patch_configurator.py` ships the
module into `configuration/klippy/`, and `t_register_heater_power_extension`
adds it to `expected_extensions` in `ratos-common.sh`; RatOS'
`verify_registered_extensions` registers and symlinks anything in that array the
printer does not have yet, on every update.

Those two halves fail silently apart — a registered extension whose file is
missing resolves to an empty path, and a shipped file nobody registered is never
symlinked, which stops the printer at boot on the first `sensor_type:
heater_power`. `build-configurator-fork.sh` therefore checks that both are
present and runs the module's test against the shipped copy.

#### What it will not do

The module is deliberately not strict, because a display must never be why a
printer will not boot:

- A heater named in the config that the printer does not have is **dropped with
  a console warning**, not a config error. The name to check is in the message
  and in the sensor's status.
- `min_temp` / `max_temp` are accepted and **never enforced**. A watt reading
  cannot shut the printer down, however large it gets — unlike
  `temperature_combined`, which calls `invoke_shutdown` on range violations.

`chamber_heater` is RatOS' name for the chamber heater and is the one guess in
the file. If the machine calls it something else, the console says so at boot
and `rated_watts`/`heaters` in `power-sensors.cfg` is where to fix it.

### What the two helpers are for

`_MAINTENANCE_APPROACH` (bed, then gantry) and
`_MAINTENANCE_POSITION_TOOLHEADS` (both carriages to X0/X1) are shared by all
of the above, so the geometry is written once. The second one parks both
carriages at −73/673 before moving them to their targets: going straight there
from wherever they happen to be can put them closer than `safe_distance`
mid-move, which Klipper aborts; from the parking positions the inward moves
cannot.

It is also why the copy/mirror reset lives in that helper rather than inline. A
macro body is rendered in full before its first line executes, and RatOS' `G28`
resets the IDEX mode itself and restores copy or mirror on the way out — so a
mode read inline would be the pre-homing one.

### Installing it

**`600.cfg` includes it**, so on a machine running the 600 printer type the
macros arrive with a configurator update and `printer.cfg` needs no edit at all.
The relative path (`../v-core-4-1-idex-600/maintenance.cfg`) is what makes that
work: Klipper resolves an include against the directory of the file containing
it, and `600.cfg` is the one file of ours the shared template pulls in by
itself. `build-configurator-fork.sh` checks that the path resolves in the
shipped tree, because in this repo the two files sit in one directory and on the
printer they do not.

Having

```
[include RatOS/printers/v-core-4-1-idex-600/maintenance.cfg]
```

in `printer.cfg` as well is harmless — Klipper's include guard rejects only
*recursive* includes (`configfile.py` drops the path from `visited` again after
parsing), so the file is parsed twice with identical content. On a machine still
running the hand-patched 500 config, neither path exists yet: paste the file's
contents into `printer.cfg` instead.

`tests/test_maintenance_macro.py` renders every macro with a stand-in for the
600's printer object and checks the coordinates above, the move order, the
`safe_distance` floor, the clamping, the temperature gate and the timeout. A
gcode_macro is a Jinja template Klippy renders in full before its first line
executes, so a typo in it is a startup error on the printer — this is the only
place that can catch it off-machine.

## When to switch the machine over

A fair question this raises: if the 600 geometry only arrives with the printer
type, how can the machine print correctly before that?

It already does. **The running geometry does not come from the configurator.**

- `printer.cfg` sets the axis limits directly (`position_min`/`max`/`endstop`
  for X, the dual carriage, Y and Z), and `printer.cfg` is included at the top
  of the generated config, so its sections come *last* and win over everything.
- The 600 includes come from `Custom_settings/600_idex.cfg`.
- Klippy never reads `printer-definition.json`. Grep the thirteen RatOS klippy
  modules for it and you get nothing — it is an input to *config generation*,
  not to the running printer.
- Nothing regenerates on its own. `ratos-update.sh` does not touch the
  generated config at all, and `regenerateConfiguration` has exactly one
  caller: an explicit CLI command.

So the machine boots on Kalico with correct 600 mm limits, from the config it
runs today, whether or not the configurator has ever heard of a 600.

### The order is not really a free choice

The printer type ships **in the fork's `configuration/`**. Getting it means
pointing `~/ratos-configurator` at the fork — which is the same action that
brings Kalico, because Moonraker's pull fires the post-merge hook, which runs
`ratos-update.sh`, whose first step is the klipper migration.

The type and the firmware therefore **arrive together**. What you actually
choose is *when you regenerate*, and that is a deliberate, separate action.

### The recommendation, and its cost

Regenerate **after** the firmware is validated, not during.

The reason is not that the config would be wrong before — it would not. It is
that regenerating swaps a hand-tuned config for a freshly generated one at the
same moment the firmware changes underneath it. If `G28 Z` then misbehaves you
cannot tell whether it is Kalico's extra homing retract or a changed limit.

The honest cost of waiting: the generated config is **not** the config you
validated. After regenerating you have to re-run the geometry-sensitive parts —
stage 3 in full, and the mesh bounds in stage 4. Budget for that rather than
assuming one pass covers both.

### The switch itself

1. Select **V-Core 4.1 IDEX 600** in the configurator and walk it through.
2. In `printer.cfg`, change `[include RatOS_4.1.cfg]` back to
   `[include RatOS.cfg]`.
3. Copy the three overrides above into `printer.cfg`.
4. `Custom_settings/600_idex.cfg` becomes redundant — its values now live in
   `600.cfg` and the definition. Keep `RatOS_4.1.cfg` as a backup until a print
   comes off clean.
5. Re-run stage 3 and the stage 4 mesh checks from the test plan.

Your own includes — `buffer.cfg`, `LEDS.cfg`, `filament_sensor.cfg`,
`Filter.cfg`, the nozzle wipe/scrub macros, the chamber heater and the
three-zone bed — are untouched by this. None of them depend on size or
`bedMargin`.

## What was verified, and what was not

**Verified**, off-printer:

- All 9 axis limits in `600.cfg` agree with the definition's `bedMargin` and
  size. Run on every build; the build refuses to commit otherwise.
- 28 size- and margin-dependent outputs simulated against the live config —
  25 match exactly, and the 3 that do not are precisely the overrides above.
  That includes both parking positions, `variable_bed_margin_*`, the resonance
  probe point, the Beacon mesh and contact-mesh bounds, and all five
  `[printer]` limits.

**Not verified**: a real configurator run, and hardware. Before the first
homing after switching over, check the axis limits in the generated config —
especially `sizes.600.z = 655`, which is the one value that was inconsistent
across the source files (`600_idex.cfg` said 650, an older `600.cfg` said 700,
`printer.cfg` says 655 and wins on the running machine).

## An upstream inconsistency, for the record

The same cross-check run against the stock **500** size reports a mismatch:
`500.cfg` sets `stepper_y.position_max: 534` where `bedMargin` implies 533.65.
300 and 400 are consistent, so this looks like a rounding slip upstream. It does
not affect this printer, and the fork does not touch it.
