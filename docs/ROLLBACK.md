# Going back to stock RatOS

> **Deutsche Fassung: [ROLLBACK.de.md](ROLLBACK.de.md)**

This returns a printer from Kalico to the Klipper-based RatOS it came with.

It is a normal path, not an emergency procedure, and it is deliberately written
so you can follow it on a printer that is currently *not* working. Nothing here
depends on Klippy starting.

---

## ⚠️ Before you start

- **Stay near the emergency stop afterwards too.** Coming back is a firmware
  change like any other; the first `G28 Z` after it deserves the same attention
  as it did going the other way.
- **Do not use Mainsail's "Recover" or "Hard Recover" buttons** on a migrated
  machine. Hard Recover deletes `~/klipper` entirely and reinstalls stock Klipper
  from the old address — which sounds like what you want here, but it also
  destroys plugin links without telling you, and it does not undo the
  configurator side. Use this guide instead.
- **You will need to re-flash your boards** if you rebuilt their firmware while
  on Kalico. See step 4.

---

## What you need

The two values you wrote down in step 0 of the upgrade:

```
commit   e.g. 2817b348e23c779b68ae5f27f2b9b9af8cfcf0da
branch   e.g. ratos/v2.1.x
```

Lost them? They are recoverable:

```bash
git -C ~/klipper log --oneline --all | head -30
```

Look for the last commit that is *not* one of the Kalico ones. If nothing looks
familiar, use `ratos/v2.1.x` as the branch and skip pinning the exact commit —
RatOS will pull the right one itself in step 3.

---

## Step 1 — Point the configurator back at RatOS

```bash
bash -c '
cd ~/ratos-configurator &&
git remote set-url origin https://github.com/Rat-OS/RatOS-configurator.git &&
git fetch origin &&
git checkout -B v2.1.x-deployment-2 origin/v2.1.x-deployment-2
' 2>&1 | tee ~/rollback.log
```

Check it took:

```bash
git -C ~/ratos-configurator branch --show-current
grep -n 'RATOS_FORK_URL=' ~/printer_data/config/RatOS/scripts/klipper-fork-migration.sh
```

Expected: `v2.1.x-deployment-2`, and a `RATOS_FORK_URL` pointing at
`Rat-OS/klipper`.

> If the `git fetch` fails because the branch cannot be found, your checkout is
> restricted to one branch. Widen it once:
> `git -C ~/ratos-configurator config --unset-all remote.origin.fetch && git -C ~/ratos-configurator config --add remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`
> then repeat the block above.

---

## Step 2 — Point Klipper back

```bash
bash -c '
cd ~/klipper &&
git remote set-url origin https://github.com/Klipper3d/klipper.git &&
git fetch origin &&
git checkout -B ratos/v2.1.x origin/master
' 2>&1 | tee -a ~/rollback.log
```

If you have the exact commit from your notes, pin it:

```bash
git -C ~/klipper reset --hard 2817b348e23c779b68ae5f27f2b9b9af8cfcf0da
```

Confirm Kalico is gone — this file only exists in Kalico:

```bash
ls ~/klipper/klippy/__init__.py
```

`No such file or directory` is the correct answer here.

---

## Step 3 — Let RatOS put itself back together

```bash
sudo systemctl restart ratos-configurator moonraker
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
sudo systemctl restart klipper
```

The update re-creates RatOS' own plugin links, including Beacon's.

---

## Step 4 — Undo the two configuration additions

If you added either of these during the upgrade, take them out again — they are
harmless on Klipper, but leaving them is untidy and one of them changes homing:

- `homing_retract_dist: 1` in `[stepper_z]` — **remove it**, or set it back to
  whatever it was. Leaving it means a shorter Z homing retract than stock.
- `rref: 12000` in `[tmc2240 …]` — harmless either way, since it equals
  Klipper's default. Remove it if you prefer a clean file.

Then `sudo systemctl restart klipper`.

---

## Step 5 — Board firmware

**If you rebuilt your board firmware while on Kalico, you must rebuild it now.**
Klipper and Kalico firmware are not interchangeable; leaving Kalico firmware on a
board while running Klipper produces protocol errors that are hard to read.

One board at a time, mainboard first:

```bash
ls -l /dev/RatOS/
sudo ~/printer_data/config/RatOS/scripts/flash-path.sh /dev/RatOS/<board-name> 2>&1 | tee ~/reflash.log
```

If you never got to step 8 of the upgrade, your boards still have their original
firmware and there is nothing to do.

---

## Step 6 — Check, then move

```bash
grep -n 'App Name:' ~/printer_data/logs/klippy.log | tail -1
```

On stock Klipper this prints **nothing** — Klipper does not write that line. That
is the result you want.

Then, hand near the emergency stop:

```gcode
M84
G28 X
G28 Y
G28 Z
```

---

## Restoring from the backup instead

If the printer is in a state you no longer want to reason about, the backup from
step 0 of the upgrade puts the configuration back wholesale:

```bash
sudo systemctl stop klipper
tar xzf ~/ratos-backup.tgz -C /
sudo systemctl start klipper
```

This restores `~/printer_data/config` and the Python environment. It does **not**
restore `~/klipper` or `~/ratos-configurator` — steps 1 and 2 above do that, and
you should run them first.

---

## If nothing works

Reflashing the SD card with a fresh RatOS image and restoring
`~/printer_data/config` from your backup is a legitimate answer, and on a printer
you need working it is often the fastest one. Nothing in this fork touches the
bootloader or the board firmware unless you ran step 8 of the upgrade.
