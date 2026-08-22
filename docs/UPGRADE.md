# Upgrading a RatOS 2.1 printer to Kalico

> **Deutsche Fassung: [UPGRADE.de.md](UPGRADE.de.md)**

This guide is written for people who are comfortable copying commands into a
terminal but do not want to understand what each one does. Every step says what
you should see, and what to do when you see something else.

---

## ⚠️ Read this part. Do not skip it.

**This is an early version. It has been run on exactly one printer.**

Every step below has been performed on a real machine — a V-Core 4.1 IDEX with a
Beacon probe — and every failure that machine hit is written into this guide. But
one printer is one printer. Your board, your probe, your plugins are combinations
nobody has tried.

**Stay within reach of the emergency stop for the whole first session.** Not "in
the room" — within arm's reach, hand ready. Specifically:

- The first `G28 Z` after the switch is the dangerous moment. Kalico moves the Z
  axis differently from Klipper during homing, and if your probe configuration
  is not right the nozzle can be driven into the bed.
- Do not start a print, and do not leave the printer alone, until you have
  homed all three axes and watched a probe cycle complete normally.
- Never run this on a printer you need working tomorrow. Plan for the
  possibility that you spend an evening putting it back.

**You can always go back.** [ROLLBACK.md](ROLLBACK.md) returns the printer to
stock Klipper-based RatOS. Read it *before* you start, so you know what the exit
looks like.

---

## What this actually changes

RatOS normally runs on [Klipper](https://www.klipper3d.org/). This fork makes it
run on [Kalico](https://github.com/KalicoCrew/kalico) instead — a Klipper fork
with extra features. Everything else about RatOS stays: the same configurator,
the same macros, the same web interface.

Two things are worth knowing up front:

- **Your `printer.cfg` is not touched.** The upgrade changes `~/klipper` and
  `~/ratos-configurator`, not your own configuration.
- **Your MCU firmware keeps working at first.** Kalico will tell you it would
  prefer freshly built firmware. That is step 8, and it is not urgent.

---

## Before you start

You need:

- A printer running **RatOS 2.1** that currently works.
- **SSH access** to it. On Windows use PowerShell or PuTTY; on macOS and Linux
  use the Terminal. `ssh pi@RatOS.local` — if that name does not resolve, use
  the printer's IP address.
- **45 minutes**, physical access to the machine, and nothing printing.

Every command below is typed on the printer, over SSH, unless it says otherwise.

---

## Step 0 — Back up

```bash
tar czf ~/ratos-backup.tgz ~/printer_data/config ~/klippy-env
git -C ~/klipper rev-parse HEAD
git -C ~/klipper branch --show-current
```

The last two commands print something like `2817b348…` and `ratos/v2.1.x`.
**Write both down.** They are what you go back to, and rollback is far easier
with them than without.

Then copy the backup off the printer, from *your own computer*, not the printer:

```bash
scp pi@RatOS.local:~/ratos-backup.tgz .
```

---

## Step 1 — Check the printer is ready

Download [`scripts/preflight-standalone.sh`](../scripts/preflight-standalone.sh)
from this repository, then, **from your own computer**:

```bash
scp preflight-standalone.sh pi@RatOS.local:~/
ssh pi@RatOS.local 'bash ~/preflight-standalone.sh'
```

It changes nothing. It only reads.

**You want to see `==> Ready.` or `==> Ready, with notes.`** at the end.

If you see `NOT READY`, fix the `[FAIL]` lines first — each one tells you what to
do. The two most common:

| It says | What to do |
|---|---|
| `beacon does not expose the legacy probe protocol` | `git -C ~/beacon pull` then `sudo systemctl restart klipper`, and run preflight again |
| `working tree has modifications` | Something changed files inside `~/klipper`. Run `git -C ~/klipper status --short` and ask before continuing — the upgrade will refuse to run otherwise |

---

## Step 2 — Point RatOS at the fork

This is the step that makes everything else happen. Copy the whole block at once:

```bash
bash -c '
cd ~/ratos-configurator &&
git remote set-url origin https://github.com/Maximilian-Ha/RatOS-configurator.git &&
git config --unset-all remote.origin.fetch &&
git config --add remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*" &&
git fetch origin &&
git checkout -B v2.1.x-kalico-deployment origin/v2.1.x-kalico-deployment
' 2>&1 | tee ~/kalico-switch.log
```

> **Why the `bash -c`:** if something fails, only that inner shell stops. Without
> it, a failure would close your SSH window and take the error message with it.
> The output is also saved to `~/kalico-switch.log`, so nothing is lost.

Now **check it worked**. Do not skip this:

```bash
git -C ~/ratos-configurator branch --show-current
grep -n 'RATOS_FORK_URL=' ~/printer_data/config/RatOS/scripts/klipper-fork-migration.sh
```

You must see:

```
v2.1.x-kalico-deployment
178:readonly RATOS_FORK_URL="https://github.com/Maximilian-Ha/kalico.git"
```

**If the second line still says `Rat-OS/klipper`, stop here.** The switch did not
take, and the next step would simply reinstall stock Klipper and report success.
Look at `~/kalico-switch.log` for the reason.

---

## Step 3 — Run the RatOS update

```bash
sudo systemctl restart ratos-configurator moonraker
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
```

This takes a few minutes. Watch the output for `RatOS update completed
successfully`.

If you instead see `UNSUPPORTED_REPOSITORY_SOURCE` or
`GIT_CHECKOUT_REMOTE_FAILED`, stop and read the
[Troubleshooting](#troubleshooting) section — both have specific causes.

---

## Step 4 — Restart Klipper properly

```bash
sudo systemctl restart klipper
```

**This must be the service restart above.** `RESTART` and `FIRMWARE_RESTART` in
the web interface reload your configuration inside the *existing* program — they
will not load Kalico, and you will get confusing errors that look like Kalico is
broken when in fact it was never started.

The first start takes longer than usual, because Kalico compiles a helper
library. Give it a minute.

---

## Step 5 — Confirm Kalico is really running

```bash
grep -n 'App Name:' ~/printer_data/logs/klippy.log | tail -1
```

Take the line number it prints — say it is `12345` — and look at that block:

```bash
sed -n '12345,12352p' ~/printer_data/logs/klippy.log
```

You want:

```
App Name: Kalico
Branch: ratos-kalico/v2.1.x
Tracked URL: https://github.com/Maximilian-Ha/kalico.git
```

**If `grep` prints nothing at all, Kalico is not running.** Klipper never writes
that line. Go back to step 4; if that does not help, step 2 did not take.

Then check it started cleanly:

```bash
grep -nE 'Unknown config object|is not valid|Traceback' ~/printer_data/logs/klippy.log | tail -5
```

Nothing is good. Something means Kalico rejected part of your configuration —
[Troubleshooting](#troubleshooting) covers the cases we have seen.

---

## Step 6 — One config line you may need

**If your board uses TMC2240 stepper drivers** — most modern toolhead boards do,
including the LDO Orbitool and the BTT SB2240 — Kalico requires a setting that
Klipper filled in silently. You will know because Klippy refuses to start with:

```
Option 'rref' in section 'tmc2240 extruder' must be specified
```

Open `printer.cfg` in Mainsail or Fluidd and add this **at the very end**:

```ini
[tmc2240 extruder]
rref: 12000

# only if you have a second toolhead
[tmc2240 extruder1]
rref: 12000
```

`12000` is not a guess: it is the value Klipper used by default, so this changes
nothing about how your motors behave. Save, then `sudo systemctl restart klipper`.

> Freshly generated configurations already contain this. You only need to add it
> by hand because your existing configuration was written before the upgrade.

**Beacon users:** the equivalent Z-homing setting now ships with the fork, so
there is nothing for you to add. If step 7 fails with `Toolhead stopped below
model range`, see [Troubleshooting](#troubleshooting).

---

## Step 7 — First movement. Hand on the emergency stop.

This is the part where a mistake costs hardware. Do it in this order and do not
improvise.

```gcode
M84
G28 X
G28 Y
G28 Z
```

- `M84` should produce no error at all.
- `G28 X` and `G28 Y` should look exactly like they always did.
- **`G28 Z` is the one to watch.** Expect: a quick descent to a few millimetres
  above the bed, a short lift, a slower second descent to the same point, another
  short lift, then a pause of a few seconds while the probe measures.

**Hit the emergency stop if** the nozzle keeps descending past the point where it
first stopped, or touches the bed at all. Neither belongs in a normal homing
cycle.

Homing X and Y first is not optional — RatOS triggers an emergency stop by design
if you home Z without them.

Once that works, do a `Z_TILT_ADJUST` (or `QUAD_GANTRY_LEVEL`) and a bed mesh,
still watching. Only then consider printing.

---

## Step 8 — Rebuild the board firmware

Kalico writes a line like this into the log for each board:

```
MCU 'mcu' currently has firmware compiled for Klipper (version v0.12.0-…).
  It is recommended to re-flash for best compatiblity with Kalico
```

Your printer works without doing this. Do it when you have time, **one board at a
time**, starting with the mainboard:

```bash
ls -l /dev/RatOS/
sudo ~/printer_data/config/RatOS/scripts/flash-path.sh /dev/RatOS/<board-name> 2>&1 | tee ~/flash.log
```

The board names come from `ls -l /dev/RatOS/`. **Klipper stopping and starting
during this is normal** — the script does it deliberately, to free the USB port.

Expect `Flashing successful.` at the end. If a toolhead board fails partway you
may have to open the enclosure and press its bootloader button, which is why the
mainboard goes first: it is the easiest one to recover.

You can also do this from the RatOS configurator's web interface once the fork is
installed.

---

## Troubleshooting

Errors we have actually hit on a real machine, with the real cause.

### `UNSUPPORTED_REPOSITORY_SOURCE` during the update

The configurator is still the stock one. Go back to step 2 and check the
`RATOS_FORK_URL` line.

### `GIT_CHECKOUT_REMOTE_FAILED`, and it happens on every update

Something in `~/klipper/klippy/extras/` is a symlink over a file Kalico ships,
and git refuses to overwrite it. Current versions of the fork remove those links
automatically. If you are on an older one:

```bash
rm ~/klipper/klippy/extras/gcode_shell_command.py
```

### `Unable to load module 'error_mcu'`

The old Klipper program is still running against the new Kalico files. This is
step 4: `sudo systemctl restart klipper`.

### `Option 'rref' in section 'tmc2240 …' must be specified`

Step 6.

### `Toolhead stopped below model range` when homing Z

Beacon users. The message is misleading — the toolhead is too far *above* the
bed, not below. Add this to the `[stepper_z]` section of your `printer.cfg`:

```ini
homing_retract_dist: 1
```

The technical explanation is in [RISKS.md](RISKS.md) §3.

### `Section 'belay my_belay' is not a valid config section`

Only relevant if you use the third-party [Belay] filament-buffer plugin — it is
**not** part of RatOS, and most printers do not have it. Kalico includes Belay
natively, so after the upgrade you can uninstall the separate plugin; the
configuration keeps working unchanged.

### The configurator cannot read board firmware versions

Fixed in current versions of the fork. Note that this query fails *by design*
while Klipper is running, because Klipper holds the USB port exclusively — stop
Klipper first if you want to run it by hand.

### `klipper_tmc_autotune` after the upgrade

Only relevant if you installed it yourself — it is **not** part of RatOS. It
works on Kalico, but it is not restored automatically if `~/klipper` is ever
deleted and re-cloned. Re-run `~/klipper_tmc_autotune/install.sh` if its sections
suddenly become unknown.

### Something else

Collect these three and ask:

```bash
tail -60 ~/printer_data/logs/klippy.log
git -C ~/klipper log --oneline -1
git -C ~/ratos-configurator branch --show-current
```

---

## Going back

[ROLLBACK.md](ROLLBACK.md). It is a normal, supported path, not an emergency
procedure.
