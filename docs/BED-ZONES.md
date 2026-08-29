# Heating only the part of the bed the print sits on

This machine's bed is one plate over **four independent heaters**. Klipper
knows one of them as `[heater_bed]`; the other three are `[heater_generic]`
sections — `BED_VR`, `BED_HL`, `BED_HR` — and nothing drives them on their own,
which is why a macro has always mirrored the bed temperature onto all four.

[`machine/bed-zones.cfg`](../machine/bed-zones.cfg) replaces that macro with the
same behaviour plus a second variant:

| Mode | What heats |
|---|---|
| `ALL` | all four zones follow the bed temperature — today's behaviour, and the default |
| `AUTO` | only the zones the print actually covers; every other heater is set to 0 |

Switch with `BED_ZONES_MODE MODE=AUTO` / `MODE=ALL`. The choice is stored in the
variables file and survives a restart.

Nothing in the file is specific to Kalico or to this fork. It is plain
`gcode_macro` and runs the same on stock RatOS.

## Before you trust it: check the zone map

The file assumes this layout, in bed coordinates:

```
 y=600  +---------------+---------------+
        |    BED_HL     |    BED_HR     |   rear
 y=300  +---------------+---------------+
        |   heater_bed  |    BED_VR     |   front
 y=0    +---------------+---------------+
       x=0            x=300           x=600
```

That mapping is **an assumption from the heater names**, not something anybody
measured. If it is wrong, `AUTO` heats the wrong quarter and the print starts on
a cold plate. Check it once, on a cold bed:

```gcode
BED_ZONES_TEST ZONE=heater_bed TEMP=50
```

Wait two minutes, feel which quarter of the plate is warm, then `BED_ZONES_OFF`
and repeat for `BED_VR`, `BED_HL` and `BED_HR`. If a zone warms a different
quarter than the picture says, fix `variable_zones` in `bed-zones.cfg` — the
rectangles are plain numbers, there is nothing else to change.

## Installing

1. Copy the file to the printer:
   `~/printer_data/config/Custom_settings/bed-zones.cfg`
2. Add `[include Custom_settings/bed-zones.cfg]` to `printer.cfg`, **below**
   `[include RatOS.cfg]` and below your other custom includes.
3. **Remove the macro that currently mirrors `heater_bed` onto `BED_VR`,
   `BED_HL` and `BED_HR`.** Two mechanisms writing the same heater targets is
   worse than either one alone — whichever runs last wins, and which one that
   is depends on timing.
4. `FIRMWARE_RESTART`, then `BED_ZONES_STATUS`. It should list four zones, mode
   `ALL`, all four active.
5. Verify the zone map as above.
6. Only then `BED_ZONES_MODE MODE=AUTO`, and watch the first print start.

If you already define `_USER_START_PRINT` somewhere else, delete that section
from `bed-zones.cfg` and call `BED_ZONES_START_PRINT { rawparams }` from your
own hook instead. Two sections of the same name merge, and the last `gcode:`
block silently wins.

## Where the printed area comes from

`START_PRINT` calls `_USER_START_PRINT` with the slicer's parameters *before* it
heats the bed, which is where the zone selection happens. Sources, in the order
they are trusted:

1. `MIN_X`/`MIN_Y`/`MAX_X`/`MAX_Y` passed to `BED_ZONES_AUTO` by hand.
2. **`X0`/`Y0`/`X1`/`Y1`** — the adaptive bed mesh bounds RatOS already receives
   from the slicer. This is the normal path. It covers the whole printed area,
   wipe tower included. It requires a current RatOS slicer profile: the
   `START_PRINT` line in your start gcode must carry
   `X0={adaptive_bed_mesh_min[0]} Y0={adaptive_bed_mesh_min[1]} X1={adaptive_bed_mesh_max[0]} Y1={adaptive_bed_mesh_max[1]}`.
3. The bounding box over all `[exclude_object]` outlines, if the slicer labels
   objects. One box over everything, deliberately — a prime blob and a wipe
   tower are not objects, and a per-object selection would leave them on cold
   plate.
4. Nothing usable → the whole bed heats, and the console says so.

A zone counts as needed when the printed area comes within `variable_margin`
(40 mm by default) of it. That margin is what keeps a part ending 5 mm short of
a zone border off the cold edge of a heated zone. Raise it if you see edge
lifting, lower it to save more.

IDEX **copy and mirror mode always heat the whole bed**: the gcode is
transformed after `START_PRINT`, so the coordinates the slicer reported are not
where the second toolhead prints.

## Commands

| Command | What it does |
|---|---|
| `BED_ZONES_STATUS` | mode, requested temperature, and every zone's rectangle, temperature and target |
| `BED_ZONES_MODE MODE=ALL\|AUTO` | switch variant, remembered across restarts |
| `BED_ZONES_SET TEMP=60` | set the bed temperature on the active zones |
| `BED_ZONES_OFF` | all zones off, and drop an automatic selection |
| `BED_ZONES_SELECT ZONES=all` | heat the whole bed |
| `BED_ZONES_SELECT ZONES=BED_HL,BED_HR` | heat exactly these zones |
| `BED_ZONES_AUTO X0=.. Y0=.. X1=.. Y1=..` | select from a printed area by hand |
| `BED_ZONES_TEST ZONE=BED_VR [TEMP=50]` | heat one zone, to check the zone map |

`M140` and `M190` are routed through the selection, so slicers, `START_PRINT`
and the console all work unchanged. RatOS does not override either of them, so
nothing else is displaced.

The interface does not send `M140` — Mainsail and Fluidd set a bed temperature
with `SET_HEATER_TEMPERATURE`, and their heaters-off button uses
`TURN_OFF_HEATERS`. So the zones also *follow* `heater_bed` by observation: a
poll every two seconds notices a bed target that changed behind the macros' back
and re-applies the current selection, and notices an active zone that was
switched off elsewhere and switches the rest off too. It writes nothing when
nothing changed.

## What this costs you

Read these before running `AUTO` unattended.

- **A partly heated plate is not a flat plate.** 600 mm of aluminium with one
  hot quarter bends differently than one heated evenly. RatOS probes a fresh
  adaptive mesh in `START_PRINT` *after* the bed is at temperature, so the mesh
  matches the state the plate is actually in — but only if you let it probe. If
  you print from a **saved mesh profile** taken on a fully heated bed, that
  profile is wrong under `AUTO`. Use `ALL` in that case.
- **The bed slider reads 0 when the front-left quarter is not needed.**
  `heater_bed` is one of the four zones; when the print does not sit on it, it
  is switched off like any other zone and the interface shows it as off. Moving
  the slider still works — it is read as "set the bed temperature", the zones
  take it, and the slider snaps back. To switch the bed off in that state use
  `BED_ZONES_OFF`, the heaters-off button, or a zone's own slider.
- **A zone that gets switched off while `M190` waits for it hangs the wait.**
  `TEMPERATURE_WAIT` returns when the temperature is reached, and never if the
  target went to zero. That is Klipper's behaviour for every generic heater,
  RatOS' extruder waits included — but stock `M190` on `heater_bed` does *not*
  behave that way, so this is one thing you lose. `M112` or a firmware restart
  is the way out.
- **One zone alone heats slower than a quarter of the bed should.** Its
  neighbours are cold aluminium and take heat sideways. If a lone zone is slow
  enough to trip `verify_heater`, give it its own
  `[verify_heater BED_xx] check_gain_time:` rather than removing the zone.
- **The prime blob.** RatOS primes near the print, but "near" is not "inside".
  If priming ends up on cold plate, put that zone in `variable_always_on`.
- **The chamber heater.** If you heat the chamber via the bed
  (`chamber_heater_bed_temp`), `AUTO` gives it a quarter of the plate to work
  with. Use `ALL` for chamber-heated prints.
- **This saves energy, not time.** Three of four heaters off is roughly three
  quarters of the bed power not drawn. It does not make the print start sooner.

## Going back

`BED_ZONES_MODE MODE=ALL` restores the old behaviour immediately — all four
zones follow the bed temperature again, exactly as the macro this file replaces
did. Removing the include and restarting takes the whole thing out, at which
point nothing drives `BED_VR`, `BED_HL` and `BED_HR` any more.

## What was verified, and what was not

**Verified**, off-printer, by `tests/test_bed_zones.py` — 21 checks, run as part
of `tests/run-all.sh`. It executes the macros: the templates go through jinja2
with Klipper's delimiters and a fake printer, and the emitted commands are run
in order, so the check is on which heaters actually end up with a target.

- the four zones tile the 600 × 600 bed exactly, with no gap and no overlap
- `ALL` heats and waits for all four zones
- a print in the rear right selects `BED_HR` alone, `M190` heats and waits for
  that zone only, and `heater_bed` stays at 0
- a print straddling a border takes both zones, and each of the four margin
  edges is checked one step inside and one step outside the margin
- `exclude_object` outlines are used when the slicer sends no print area
- no print area, and IDEX copy/mirror, fall back to the whole bed
- switching the bed off drops the selection, so the next heat-up is whole-bed
- the poll adopts an outside bed target, switches everything off when a zone is
  switched off elsewhere, writes nothing when nothing changed, and keeps its
  hands off the zones while `M190` is waiting
- an unknown zone name is refused, and a missing heater is reported rather than
  silently skipped

The zone table, the active mask, all four margin edges, the wait temperature,
the poll's off-detection and the `START_PRINT` hand-off were each confirmed to
fail the suite when the corresponding line in the config is broken, so those
checks measure the config rather than themselves.

**Not verified**: anything physical. Which quarter of the plate each heater sits
under, how far heat creeps into a cold zone, what a half-heated 600 plate does
to first-layer flatness, and whether 40 mm is the right margin for your
filaments. That is what `BED_ZONES_TEST` and a watched first print are for.
