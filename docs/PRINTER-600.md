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

## Switching the machine over

This is **not** required to run Kalico, and it is a separate step from the
firmware migration. Do the firmware first (`docs/TESTPLAN.md`), get it printing,
and only then come back to this. Doing both at once means two suspects for
every symptom.

1. Select **V-Core 4.1 IDEX 600** in the configurator and walk it through.
2. In `printer.cfg`, change `[include RatOS_4.1.cfg]` back to
   `[include RatOS.cfg]`.
3. Copy the three overrides above into `printer.cfg`.
4. `Custom_settings/600_idex.cfg` becomes redundant — its values now live in
   `600.cfg` and the definition. Keep `RatOS_4.1.cfg` as a backup until a print
   comes off clean.

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
