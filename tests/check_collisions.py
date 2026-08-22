#!/usr/bin/env python3
"""Guard the one hardcoded fact in preflight: which paths Kalico takes over.

The migration is ``git checkout`` plus ``git reset --hard``, with no ``git
clean``. An untracked symlink in ``klippy/extras`` therefore survives it --
unless Kalico's tree TRACKS the same path, in which case one of two things
happens, verified by reproduction on git 2.43:

    listed in .git/info/exclude    -> silently replaced by Kalico's file
    not listed                     -> checkout aborts, migration returns 6,
                                      on every update, permanently

So the set of colliding basenames is the difference between "a symlink you can
ignore" and "a printer that can never be updated again". preflight hardcodes
that set, because it has to run on a machine with no checkout and no network.
This test re-derives it from the built forks and fails if the two disagree --
so a future Kalico that starts tracking, say, ``ratos.py`` cannot slip past.

Usage: check_collisions.py <kalico-checkout> <configurator-checkout> <preflight.sh>...
"""

import os
import re
import subprocess
import sys

# Modules third-party addons symlink into klippy/extras. Not derivable from
# either checkout -- they live in repositories the fork does not track -- so
# they are listed here, deliberately wider than this printer needs. Adding a
# name that nobody installs is harmless; the intersection just stays empty.
THIRD_PARTY = [
    "beacon.py",  # beacon3d/beacon_klipper
    "autotune_tmc.py",  # andrewmcgr/klipper_tmc_autotune
    "motor_constants.py",  # ditto
    "motor_database.cfg",  # ditto
    "linear_movement_analysis.py",  # worksasintended/klipper_linear_movement_analysis
    "led_effect.py",  # julianschill/klipper-led_effect
]


def kalico_tracked(checkout):
    out = subprocess.check_output(
        ["git", "-C", checkout, "ls-files", "klippy/extras", "klippy/kinematics"],
        text=True,
    )
    paths = [line for line in out.splitlines() if line.strip()]
    if not paths:
        sys.exit("ERROR: %s tracks nothing under klippy/extras -- not a Kalico checkout?" % checkout)
    return paths


def ratos_supplied(checkout):
    base = os.path.join(checkout, "configuration", "klippy")
    if not os.path.isdir(base):
        sys.exit("ERROR: %s has no configuration/klippy" % checkout)
    names = []
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name.endswith((".py", ".cfg")) and name != "__init__.py":
                names.append(name)
    if not names:
        sys.exit("ERROR: configuration/klippy is empty -- RatOS has restructured")
    return names


def declared_in(script):
    with open(script, "r") as handle:
        text = handle.read()
    match = re.search(r'^KALICO_TRACKED_COLLISIONS="([^"]*)"', text, re.MULTILINE)
    if not match:
        sys.exit("ERROR: %s no longer defines KALICO_TRACKED_COLLISIONS" % script)
    return set(match.group(1).split())


def main(argv):
    if len(argv) < 4:
        sys.exit(__doc__)
    kalico, configurator = argv[1], argv[2]
    scripts = argv[3:]

    tracked = {os.path.basename(p) for p in kalico_tracked(kalico)}
    supplied = set(ratos_supplied(configurator)) | set(THIRD_PARTY)
    actual = tracked & supplied

    failed = False
    for script in scripts:
        declared = declared_in(script)
        if declared == actual:
            print("ok: %s declares the %d real collision(s)" % (os.path.basename(script), len(actual)))
            continue
        failed = True
        print("FAIL: %s is out of date." % script)
        for name in sorted(actual - declared):
            print("      Kalico now tracks %s, which RatOS or an addon symlinks in." % name)
            print("      Add it to KALICO_TRACKED_COLLISIONS, or the migration aborts")
            print("      on any printer whose .git/info/exclude does not list it.")
        for name in sorted(declared - actual):
            print("      %s no longer collides -- remove it, it is now a false alarm." % name)

    if failed:
        return 1
    print("%d colliding path(s): %s" % (len(actual), ", ".join(sorted(actual)) or "(none)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
