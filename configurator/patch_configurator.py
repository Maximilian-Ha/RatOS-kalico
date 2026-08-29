#!/usr/bin/env python3
"""Turn a pristine RatOS-configurator checkout into the RatOS-Kalico fork.

The RatOS configurator is the repository RatOS updates actually flow through:
it owns ``configuration/`` (symlinked to ``~/printer_data/config/RatOS``), all
thirteen klippy extensions, the kinematics module, ``moonraker.conf`` and every
update script including ``klipper-fork-migration.sh``. Forking RatOS therefore
means forking this repository.

This script applies the fork's whole delta to a checkout, in place. It is the
only place the delta is defined -- ``scripts/build-configurator-fork.sh`` runs
it and commits the result onto the fork branch, so re-basing onto a new RatOS
release is "check out the new upstream, run this again".

Every transform is anchored on exact source text and asserts it matches exactly
once. If RatOS moves an anchor the script aborts and names the transform rather
than half-applying: a half-patched update script is far worse than an
un-patched one, because the printer keeps running either way but only one of
them is diagnosable.

It also installs the fork's own publish workflow and removes upstream's, because
Moonraker pulls a CI-built deployment branch rather than the source branch --
without that workflow the fork has no branch a printer can be pointed at.

Finally it adds "V-Core 4.1 IDEX 600" as a real printer type, so that machine
can be generated rather than hand-patched after picking 500.

Usage:
    patch_configurator.py --checkout DIR --kalico-url URL --kalico-branch NAME
                          --kalico-commit SHA --configurator-url URL
                          --source-branch NAME --deployment-branch NAME
                          [--check] [--no-sweeping-period] [--no-printer-600]

Exit codes:
    0  patched (or, with --check, already fully patched)
    1  an anchor did not match, or --check found work to do
    2  bad invocation / IO error
"""

import argparse
import os
import re
import sys

MARKER = "RatOS-Kalico"

# Paths Kalico tracks in klippy/extras that RatOS or a third-party addon
# also symlinks in. Derived, not guessed -- tests/check_collisions.py
# recomputes this from the built forks and fails if it drifts.
KALICO_OWNED_PATHS = ["gcode_shell_command.py", "belay.py"]

# Upstream commit these transforms were written and verified against.
VERIFIED_UPSTREAM = "26d261742157103e166e6879cd9dead37cf2cc42"


class AnchorError(Exception):
    """An anchor did not match exactly once."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sub_once(text, old, new, what):
    """Replace `old` with `new`, requiring exactly one occurrence."""
    count = text.count(old)
    if count != 1:
        raise AnchorError(
            "%s: anchor found %d times, expected exactly 1.\n"
            "    Anchor: %r\n"
            "    RatOS has changed this file. Review the transform against the "
            "new upstream before shipping." % (what, count, old[:160])
        )
    return text.replace(old, new, 1)


def re_sub_once(text, pattern, repl, what, flags=0):
    """Regex-replace, requiring exactly one match."""
    rx = re.compile(pattern, flags)
    matches = rx.findall(text)
    if len(matches) != 1:
        raise AnchorError(
            "%s: pattern matched %d times, expected exactly 1.\n"
            "    Pattern: %s\n"
            "    RatOS has changed this file. Review the transform against the "
            "new upstream before shipping." % (what, len(matches), pattern)
        )
    return rx.sub(repl, text, count=1)


# ---------------------------------------------------------------------------
# transforms
#
# Each returns the patched text. They receive the fork's identity in `cfg`.
# ---------------------------------------------------------------------------


def t_migration_constants(text, cfg):
    """Point klipper-fork-migration.sh at Kalico instead of Rat-OS/klipper.

    This is the blocker that makes every other change pointless without it.
    ratos-update.sh runs this script first on every single update; if it
    returns non-zero the whole update reports failure.

    The script decides whether a ~/klipper checkout is "supported" by exact
    string comparison of `git remote get-url origin` against a fixed allowlist.
    A Kalico origin matches nothing and falls through to
    UNSUPPORTED_REPOSITORY_SOURCE -> return 2 -> exit 2, forever.

    Note what is deliberately NOT changed: the variable names. They are
    referenced elsewhere in the file, and renaming them buys nothing but a
    bigger diff to re-apply on every RatOS release.
    """
    text = sub_once(
        text,
        'readonly OFFICIAL_KLIPPER_URL="https://github.com/Klipper3d/klipper.git"',
        "# %s: the 'official' upstream is now Kalico.\n"
        'readonly OFFICIAL_KLIPPER_URL="%s"' % (MARKER, cfg["kalico_upstream_url"]),
        "migration: OFFICIAL_KLIPPER_URL",
    )
    text = sub_once(
        text,
        'readonly RATOS_FORK_URL="https://github.com/Rat-OS/klipper.git"',
        'readonly RATOS_FORK_URL="%s"' % cfg["kalico_fork_url"],
        "migration: RATOS_FORK_URL",
    )
    # Existing RatOS 2.1 machines have origin = Klipper3d/klipper (that is what
    # the OS image clones). Once OFFICIAL_KLIPPER_URL points at Kalico, those
    # machines match neither the official list nor the fork URL, so every
    # update would abort. Listing the old URLs as deprecated makes them take
    # the "migration needed" path (return 0) and roll forward instead.
    text = sub_once(
        text,
        '\t"https://github.com/tg73/klipper.git" # tg73 fork sometimes used during development\n',
        '\t"https://github.com/tg73/klipper.git" # tg73 fork sometimes used during development\n'
        "\t# %s: accept the pre-fork Klipper origins so already-deployed\n"
        "\t# RatOS 2.1 machines migrate forward instead of aborting.\n"
        '\t"https://github.com/Klipper3d/klipper.git"\n'
        '\t"git@github.com:Klipper3d/klipper.git"\n'
        '\t"ssh://git@github.com/Klipper3d/klipper.git"\n'
        '\t"git://github.com/Klipper3d/klipper.git"\n'
        '\t"https://github.com/Rat-OS/klipper.git"\n'
        '\t"git@github.com:Rat-OS/klipper.git"\n'
        '\t"ssh://git@github.com/Rat-OS/klipper.git"\n' % MARKER,
        "migration: DEPRECATED_FORK_URLS",
    )
    # A distinct remote name. Reusing "ratos-fork" would leave an existing
    # machine's remote pointing at Klipper history under a name the script
    # then rewrites -- workable, but the distinct name makes the state
    # obvious in `git remote -v` when diagnosing a printer.
    text = sub_once(
        text,
        'readonly RATOS_FORK_REMOTE="ratos-fork"',
        'readonly RATOS_FORK_REMOTE="ratos-kalico"',
        "migration: RATOS_FORK_REMOTE",
    )
    # A distinct branch name matters more than it looks. checkout_target_branch
    # first tries `show-ref --verify refs/heads/$TARGET_BRANCH`; on an existing
    # RatOS box a local `ratos/v2.1.x` already exists pointing at Klipper
    # history. Reusing the name would check that stale branch out and only then
    # reset it onto unrelated Kalico history.
    text = sub_once(
        text,
        'readonly TARGET_BRANCH="ratos/v2.1.x"',
        'readonly TARGET_BRANCH="%s"' % cfg["kalico_fork_branch"],
        "migration: TARGET_BRANCH",
    )
    return text


def t_migration_allowlist(text, cfg):
    """Replace the four hardcoded Klipper3d URL spellings with Kalico's.

    Only the first element of this array uses the constant; the other three are
    literals that would silently never match a Kalico checkout.
    """
    new_block = (
        "    local official_urls=(\n"
        '        "$OFFICIAL_KLIPPER_URL"                       # HTTPS\n'
        '        "git@github.com:KalicoCrew/kalico.git"        # SSH shorthand\n'
        '        "ssh://git@github.com/KalicoCrew/kalico.git"  # SSH protocol\n'
        '        "git://github.com/KalicoCrew/kalico.git"      # Git protocol\n'
        "    )"
    )
    return re_sub_once(
        text,
        r"    local official_urls=\(\n"
        r'        "\$OFFICIAL_KLIPPER_URL".*\n'
        r'        "git@github\.com:Klipper3d/klipper\.git".*\n'
        r'        "ssh://git@github\.com/Klipper3d/klipper\.git".*\n'
        r'        "git://github\.com/Klipper3d/klipper\.git".*\n'
        r"    \)",
        lambda _m: new_block,
        "migration: official_urls allowlist",
    )


def t_migration_url_normalization(text, cfg):
    """Compare origin URLs tolerantly instead of byte-exactly.

    Upstream compares `git remote get-url origin` against the allowlist with
    `[[ "$a" == "$b" ]]` -- no case folding, no trailing-slash handling, no
    `.git` stripping. A checkout cloned as `https://github.com/KalicoCrew/kalico`
    (no `.git`) lands on UNSUPPORTED_REPOSITORY_SOURCE and bricks every future
    update. Kalico users are far more likely to have hand-cloned than stock
    RatOS users, so this is a real failure mode rather than a hypothetical.

    Kept in one helper so re-basing against upstream RatOS stays cheap.
    """
    helper = (
        "# %s: upstream compares origin URLs byte-exactly, so a clone made\n"
        "# without the trailing .git (or with a trailing slash, or in different\n"
        "# case) is rejected as an unsupported source and every future update\n"
        "# fails. Normalize both sides before comparing.\n"
        "normalize_git_url() {\n"
        '    local u="${1:-}"\n'
        '    u="${u%%/}"\n'
        '    u="${u%%.git}"\n'
        '    printf "%%s" "${u,,}"\n'
        "}\n"
        "\n"
        "# Migration constants (readonly to prevent accidental modification)" % MARKER
    )
    text = sub_once(
        text,
        "# Migration constants (readonly to prevent accidental modification)",
        helper,
        "migration: normalize_git_url helper",
    )
    text = sub_once(
        text,
        '        if [[ "$current_origin" == "$official_url" ]]; then',
        '        if [[ "$(normalize_git_url "$current_origin")" == '
        '"$(normalize_git_url "$official_url")" ]]; then',
        "migration: official URL comparison",
    )
    text = sub_once(
        text,
        '\t\tif [[ "$current_origin" == "$deprecated_url" ]]; then',
        '\t\tif [[ "$(normalize_git_url "$current_origin")" == '
        '"$(normalize_git_url "$deprecated_url")" ]]; then',
        "migration: deprecated URL comparison",
    )
    text = sub_once(
        text,
        '    if [[ "$current_origin" == "$RATOS_FORK_URL" ]]; then',
        '    if [[ "$(normalize_git_url "$current_origin")" == '
        '"$(normalize_git_url "$RATOS_FORK_URL")" ]]; then',
        "migration: fork URL comparison",
    )
    return text


def t_migration_yield_kalico_owned(text, cfg):
    """Remove symlinks over paths Kalico tracks, before checking Kalico out.

    Kalico tracks ``klippy/extras/gcode_shell_command.py`` and
    ``klippy/extras/belay.py``. RatOS symlinks its own copy over the first; a
    standalone Belay install symlinks over the second. What happens next depends
    entirely on whether the path is listed in ``.git/info/exclude``, and the two
    outcomes could not be further apart -- verified by reproduction on git 2.43:

    * listed   -> git treats the symlink as expendable and ``checkout -b``
                  silently replaces it with Kalico's file. This is the outcome
                  the fork wants; see ``t_drop_gcode_shell_extension``.
    * unlisted -> ``error: The following untracked working tree files would be
                  overwritten by checkout ... Aborting``. In this script that is
                  GIT_CHECKOUT_REMOTE_FAILED, which returns 1 from
                  checkout_target_branch and 6 from the migration -- and because
                  origin is never repointed, the migration re-runs and fails
                  again on *every* subsequent update. The printer never reaches
                  Kalico and no amount of retrying helps.

    Whether the line is present is a property of the printer's history, not of
    anything this fork controls: RatOS writes it when it registers an extension,
    and Moonraker's Hard Recover deletes the whole ``.git`` directory along with
    it. So the safe state is not something to hope for.

    Deleting the links first makes both paths converge on the good one. Guarded
    on -L so it can only ever remove a symlink, never a real file, and it never
    touches the source in printer_data. After the first migration these paths
    are regular tracked files and the guard makes this a no-op -- which matters,
    because this script runs on every update, not just once.

    The list is not free-form: ``tests/check_collisions.py`` re-derives it from
    the built forks and fails if it drifts.
    """
    owned = " ".join(KALICO_OWNED_PATHS)
    return sub_once(
        text,
        "    # Check if target branch already exists locally\n",
        "    # %s: Kalico tracks these paths in klippy/extras, and RatOS or a\n"
        "    # third-party addon may have symlinked its own copy over them. A\n"
        "    # symlink that is not in .git/info/exclude makes the checkout below\n"
        "    # refuse to overwrite it, and this script then returns 6 -- on every\n"
        "    # update, forever. The fork cedes these files to Kalico anyway, so\n"
        "    # drop the links and let the checkout supply the real ones. -L means\n"
        "    # only a symlink can ever be removed, never a real file, and never\n"
        "    # the source in printer_data.\n"
        "    for _ratos_kalico_owned in %s; do\n"
        "        if [ -L \"$KLIPPER_DIR/klippy/extras/$_ratos_kalico_owned\" ]; then\n"
        "            log_info \"Removing $_ratos_kalico_owned symlink; Kalico ships its own.\" \"checkout_branch\"\n"
        "            rm -f \"$KLIPPER_DIR/klippy/extras/$_ratos_kalico_owned\"\n"
        "        fi\n"
        "    done\n"
        "\n"
        "    # Check if target branch already exists locally\n" % (MARKER, owned),
        "migration: yield Kalico-owned paths",
    )


def t_tmc2240_rref(text, cfg):
    """Emit ``rref`` for TMC2240 drivers -- Kalico refuses to start without it.

    Klipper defaults it: ``config.getfloat('rref', 12000., minval=12000.,
    maxval=60000.)`` at klippy/extras/tmc2240.py in the commit RatOS pins.
    Kalico dropped the default (``tmc2240.py:285``), so the option is mandatory
    and a generated RatOS config dies at startup with

        Option 'rref' in section 'tmc2240 extruder' must be specified

    on every machine with a TMC2240 toolboard -- BTT SB2240 and LDO 2240 among
    them. The generator never wrote the option because it only knows
    ``senseResistor``, which TMC2240 drivers do not have; they use a reference
    resistor instead.

    12000 is not a guess and not a datasheet lookup: it is the value every RatOS
    machine has silently been running with, because that was Klipper's default.
    Emitting it explicitly reproduces the existing motor current exactly. If a
    board's real reference resistor differed, the printer would already have
    been running at the wrong current under Klipper, and a firmware migration is
    the wrong moment to change that.

    Placed just before the return so it covers both branches -- the motor-preset
    path and the plain run_current path -- and guarded against a preset that
    already supplies rref, so it can never emit the option twice.
    """
    return sub_once(
        text,
        "\t\t\treturn section.join('\\n') + '\\n';\n\t\t},\n\t\trenderSpeedLimits() {",
        "\t\t\t// %s: Kalico makes rref mandatory for TMC2240 where Klipper\n"
        "\t\t\t// defaulted it to 12000. Without this the generated config does\n"
        "\t\t\t// not load at all. 12000 is what Klipper's default meant, so this\n"
        "\t\t\t// reproduces the current the machine already runs.\n"
        "\t\t\tif (rail.driver.type === 'TMC2240' && !section.some((l) => l.startsWith('rref:'))) {\n"
        "\t\t\t\tsection.push(`rref: 12000`);\n"
        "\t\t\t}\n"
        "\t\t\treturn section.join('\\n') + '\\n';\n\t\t},\n\t\trenderSpeedLimits() {" % MARKER,
        "klipper-config.ts: rref for TMC2240",
    )

def t_beacon_homing_retract(text, cfg):
    """Bound the Z homing retract so beacon can still see the bed afterwards.

    Kalico retracts TWICE in ``home_rails`` -- once before the second homing
    pass (``homing.py:348``, which Klipper also does) and once after it
    (``homing.py:391``, added by Kalico 72b9b995 for sensorless homing). The
    ``homing:home_rails_end`` event fires at ``homing.py:401``, i.e. AFTER that
    second retract.

    Beacon's post-homing handler samples the sensor at whatever position the
    toolhead is standing in when that event arrives, and replaces the homed Z
    with the measurement (``beacon.py:2272-2278``). On Klipper the event fires
    at the trigger point. On Kalico it fires ``homing_retract_dist`` higher.

    RatOS never sets ``homing_retract_dist`` for a beacon printer -- the
    generator emits Z homing settings only when there is no probe -- so it falls
    to Kalico's default of 5.0 (``stepper.py:482``). With beacon's default
    ``trigger_distance`` of 2.0 that puts the sample at 7.0mm, above the top of
    a default-calibrated model (cal_ceil 5.0). ``freq_to_dist_raw`` returns
    ``+inf``, and ``math.isinf`` raises

        Toolhead stopped below model range

    which is beacon's wording for the ``-inf`` (too close) case and is simply
    wrong for this one -- the toolhead is too far ABOVE the bed, not below.

    1.0 puts the sample at 2.0 + 1.0 = 3.0mm, inside the band with margin at
    both ends, and keeps the second homing pass and its "Endstop still triggered
    after retract" check. Setting 0 would also work but disables the whole
    second-home block (``homing.py:337`` gates all of it), and leaves the
    toolhead closer to the bed rather than further from it.

    It is also strictly safer than 5.0 in the endstop-failure case: the second
    pass is set up to descend twice the retract distance below the endstop
    position, so 5.0 aims a failed pass at kinematic Z = -3.0 -- into the bed --
    where 1.0 aims it at 1.0.

    The constraint to preserve when changing this: ``trigger_distance`` plus
    this value must stay below the beacon model's ceiling.

    This goes in a SHIPPED file rather than the generator, deliberately. Files
    under ``configuration/`` reach a printer by git pull; anything the generator
    emits reaches it only on a regeneration, which destroys hand edits and which
    a user with a broken G28 cannot safely do.
    """
    return sub_once(
        text,
        "[bed_mesh]\nmesh_min: 20,30",
        "# %s: Kalico retracts a second time AFTER the second homing pass and\n"
        "# only then fires homing:home_rails_end, where beacon takes the sample\n"
        "# that becomes the homed Z. At Kalico's default retract of 5.0 that\n"
        "# sample is taken 7mm up -- above the model -- and G28 Z fails with\n"
        "# \"Toolhead stopped below model range\". Keep trigger_distance plus this\n"
        "# value below the model ceiling. See docs/RISKS.md section 3.\n"
        "[stepper_z]\n"
        "homing_retract_dist: 1\n"
        "\n"
        "[bed_mesh]\nmesh_min: 20,30" % MARKER,
        "beacon.cfg: bound the Z homing retract",
    )


def t_led_vaoc_pwm(text, cfg):
    """Let the VAOC light work when it is a plain PWM LED, not a neopixel.

    ``_LED_VAOC_ON`` and ``_LED_VAOC_OFF`` are the only two macros in
    ``led_control.cfg`` that drive a fixture directly instead of going through
    ``_LED_SET``, and both guard on ``printer['neopixel vaoc_led']``
    (``led_control.cfg:106`` and ``:112``).

    ``configuration/extras/ratrig-vaoc.cfg:39`` does emit ``[neopixel vaoc_led]``,
    so the guard holds for the stock Rat Rig VAOC camera module. It does not
    hold for anyone who wired a plain single-colour lamp to the VAOC LED pin and
    declared it as Klipper's PWM LED section::

        [led vaoc_led]
        white_pin: <pin>

    ``[led ...]`` is ``PrinterPWMLED`` and registers its printer object under
    ``led vaoc_led``; ``[neopixel ...]`` registers under ``neopixel vaoc_led``.
    The guard is therefore permanently false for the PWM case, both macros run
    to completion doing nothing, and the light never comes on -- with no error
    anywhere, which is what makes it hard to find. It affects all three call
    sites at once: ``macros/idex/vaoc.cfg:177`` (VAOC start), ``:300`` (VAOC end)
    and ``:1085`` (``_VAOC_SWITCH_LED``, the toggle in the UI).

    The fix adds an ``elif`` branch rather than replacing the guard, so the
    neopixel path is byte-for-byte what it was and a stock VAOC module sees no
    behaviour change at all.

    The PWM branch sets all four channels. ``PrinterPWMLED.__init__`` builds
    ``self.pins`` only from the ``*_pin`` options actually present
    (``klippy/extras/led.py``, the ``("red", "green", "blue", "white")`` loop),
    and ``update_leds`` only touches those, so the unused values are discarded.
    One command therefore covers a white-only lamp and an RGB one without
    having to know which is wired.

    This goes in a SHIPPED file rather than the generator, for the same reason
    as ``t_beacon_homing_retract``: files under ``configuration/`` reach a
    printer by git pull, while anything the generator emits reaches it only on a
    regeneration, which destroys hand edits.
    """
    on_old = (
        "[gcode_macro _LED_VAOC_ON]\n"
        "gcode:\n"
        "\t{% if printer['neopixel vaoc_led'] is defined %}\n"
        "\t\tSET_LED LED=vaoc_led RED=1.0 GREEN=1.0 BLUE=1.0\n"
        "\t{% endif %}\n"
    )
    on_new = (
        "# " + MARKER + ": a VAOC light wired as a plain PWM lamp is\n"
        "# [led vaoc_led], not [neopixel vaoc_led], so upstream's guard alone\n"
        "# never matches it and the light stays dark with no error anywhere.\n"
        "# The elif sets all four channels because PrinterPWMLED keeps only the\n"
        "# ones whose *_pin option exists, so one command covers a white-only\n"
        "# lamp and an RGB one. See docs/RISKS.md.\n"
        "[gcode_macro _LED_VAOC_ON]\n"
        "gcode:\n"
        "\t{% if printer['neopixel vaoc_led'] is defined %}\n"
        "\t\tSET_LED LED=vaoc_led RED=1.0 GREEN=1.0 BLUE=1.0\n"
        "\t{% elif printer['led vaoc_led'] is defined %}\n"
        "\t\tSET_LED LED=vaoc_led RED=1.0 GREEN=1.0 BLUE=1.0 WHITE=1.0\n"
        "\t{% endif %}\n"
    )
    off_old = (
        "[gcode_macro _LED_VAOC_OFF]\n"
        "gcode:\n"
        "\t{% if printer['neopixel vaoc_led'] is defined %}\n"
        "\t\tSET_LED LED=vaoc_led RED=0.0 GREEN=0.0 BLUE=0.0\n"
        "\t{% endif %}\n"
    )
    off_new = (
        "# " + MARKER + ": see _LED_VAOC_ON above.\n"
        "[gcode_macro _LED_VAOC_OFF]\n"
        "gcode:\n"
        "\t{% if printer['neopixel vaoc_led'] is defined %}\n"
        "\t\tSET_LED LED=vaoc_led RED=0.0 GREEN=0.0 BLUE=0.0\n"
        "\t{% elif printer['led vaoc_led'] is defined %}\n"
        "\t\tSET_LED LED=vaoc_led RED=0.0 GREEN=0.0 BLUE=0.0 WHITE=0.0\n"
        "\t{% endif %}\n"
    )
    text = sub_once(
        text, on_old, on_new,
        "led_control.cfg: _LED_VAOC_ON also drives a PWM [led]",
    )
    return sub_once(
        text, off_old, off_new,
        "led_control.cfg: _LED_VAOC_OFF also drives a PWM [led]",
    )


def t_check_version_package_import(text, cfg):
    """Import klippy's modules through the package, because Kalico has one.

    Kalico turned ``klippy`` into a real Python package: it has an
    ``__init__.py`` and its modules import each other relatively --
    ``reactor.py`` opens with ``from . import chelper, util``. RatOS' board
    version checker predates that. It appends ``<klipper>/klippy`` to
    ``sys.path`` and imports ``reactor``, ``serialhdl``, ``clocksync`` and
    ``mcu`` as top-level modules, which under Kalico raises

        ImportError: attempted relative import with no known parent package

    The configurator surfaces that as a failed ``mcu.boardVersion`` tRPC call,
    so the board list in the UI cannot report firmware versions -- on a machine
    where Kalico is asking to be re-flashed on every single boot.

    Putting the klipper root on sys.path instead makes ``klippy`` importable as
    the package it now is. The flat layout is kept as a fallback so the script
    still runs against stock Klipper, which matters because this same
    configurator has to work on a machine mid-migration.

    Only this one script has the pattern; every other script under src/scripts
    was checked.
    """
    return sub_once(
        text,
        'KLIPPER_DIR = os.path.abspath(os.environ[\'KLIPPER_DIR\'])\n'
        'sys.path.append(os.path.join(KLIPPER_DIR, "klippy"))\n'
        "import argparse\n"
        "import logging\n"
        "import time\n"
        "import traceback\n"
        "import reactor\n"
        "import serialhdl\n"
        "import clocksync\n"
        "import mcu\n",
        'KLIPPER_DIR = os.path.abspath(os.environ[\'KLIPPER_DIR\'])\n'
        "import argparse\n"
        "import logging\n"
        "import time\n"
        "import traceback\n"
        "\n"
        "# %s: Kalico made klippy a package, and its modules import each other\n"
        "# relatively -- reactor.py opens with `from . import chelper, util`.\n"
        "# Appending klippy/ to sys.path and importing them flat therefore raises\n"
        '# "attempted relative import with no known parent package", which the\n'
        "# configurator surfaces as a failed mcu.boardVersion call. Put the\n"
        "# klipper root on the path so `klippy` resolves as the package it is,\n"
        "# and keep the flat layout as a fallback for stock Klipper.\n"
        'if os.path.isfile(os.path.join(KLIPPER_DIR, "klippy", "__init__.py")):\n'
        "    sys.path.insert(0, KLIPPER_DIR)\n"
        "    from klippy import reactor, serialhdl, clocksync, mcu\n"
        "else:\n"
        '    sys.path.append(os.path.join(KLIPPER_DIR, "klippy"))\n'
        "    import reactor\n"
        "    import serialhdl\n"
        "    import clocksync\n"
        "    import mcu\n" % MARKER,
        "check-version.py: import klippy as a package",
    )

def t_moonraker_klipper_pin(text, cfg):
    """Repoint the pinned klipper commit at the Kalico fork.

    This value is read twice: by moonraker's updater, and by
    klipper-fork-migration.sh, which awk-parses it out of this exact section
    and uses it as TARGET_COMMIT for `git reset --hard`. If it stays a
    Rat-OS/klipper SHA, `git cat-file -e` fails against Kalico history and the
    migration exits 7.

    Do NOT add `origin:` or `primary_branch:` here. Moonraker's built-in
    klipper entry only honours channel / pinned_commit / refresh_interval;
    anything else in this section is silently discarded.
    """
    return re_sub_once(
        text,
        r"\[update_manager klipper\]\nchannel: dev\npinned_commit: [0-9a-fA-F]{40}",
        lambda _m: "[update_manager klipper]\nchannel: dev\n"
        "# %s: tip of %s\npinned_commit: %s"
        % (MARKER, cfg["kalico_fork_branch"], cfg["kalico_commit"]),
        "moonraker.conf: klipper pinned_commit",
    )


def t_moonraker_configurator(text, cfg):
    """Point the configurator updater at the fork's deployment branch.

    origin and primary_branch ARE honoured for this section (unlike the klipper
    one) because it is a plain git_repo entry.

    primary_branch must name a *deployment* branch, not a source branch: RatOS
    CI builds the Next.js app, deletes the source-only directories, renames
    src/ to app/ and force-pushes the result. The systemd unit's WorkingDirectory
    points into app/, so a source branch leaves the configurator service with
    nothing to serve.
    """
    text = sub_once(
        text,
        "primary_branch: v2.1.x-deployment-2\n"
        "origin: https://github.com/Rat-OS/RatOS-configurator.git",
        "# %s: the fork's own CI-built deployment branch.\n"
        "primary_branch: %s\norigin: %s"
        % (MARKER, cfg["deployment_branch"], cfg["configurator_fork_url"]),
        "moonraker.conf: ratos-configurator origin/primary_branch",
    )
    return text


def t_kinematics(text, cfg):
    """Make RatOS' hybrid-CoreXY kinematics satisfy Kalico's contract.

    Three independent breaks, all fatal:

    1. Kalico refuses to load [dual_carriage] unless the kinematics object
       exposes `supports_dual_carriage`. It reads the attribute unguarded, so
       the failure is a bare AttributeError during config load.
    2. Kalico hands `set_position` the homed axes as a STRING ("xyz"); RatOS
       indexes self.rails with the iterated element, which assumes ints. Note
       upstream also nests the homing_axes loop inside the rail loop, so the
       limits are recomputed once per rail -- the rewrite separates them.
    3. Kalico calls `clear_homing_state(axes)` unguarded from
       stepper_enable.motor_off() on every M84, from force_move's
       SET_KINEMATIC_POSITION (which RatOS' own macros use for belt-tension and
       shaper graphs) and from safe_z_home. RatOS only has note_z_not_homed().

    The result stays backward compatible with Klipper: the extra attribute is
    inert there, set_position still accepts integer indices, and
    note_z_not_homed survives as a wrapper.
    """
    text = sub_once(
        text,
        "        # itersolve parameters\n",
        "        # %s: Kalico's toolhead refuses [dual_carriage] unless the\n"
        "        # kinematics declares support, and reads it unguarded.\n"
        "        self.supports_dual_carriage = True\n"
        "        # itersolve parameters\n" % MARKER,
        "kinematics: supports_dual_carriage",
    )
    text = sub_once(
        text,
        "    def set_position(self, newpos, homing_axes):\n"
        "        for i, rail in enumerate(self.rails):\n"
        "            rail.set_position(newpos)\n"
        "            for axis in homing_axes:\n"
        "                if self.dc_module and axis == self.dc_module.axis:\n"
        "                    rail = self.dc_module.get_primary_rail().get_rail()\n"
        "                else:\n"
        "                    rail = self.rails[axis]\n"
        "                self.limits[axis] = rail.get_range()\n",
        '    def set_position(self, newpos, homing_axes=""):\n'
        "        for rail in self.rails:\n"
        "            rail.set_position(newpos)\n"
        "        # %s: Kalico passes axis names ('xyz'), Klipper passes indices.\n"
        "        # Accept both, and set the limits once per homed axis rather\n"
        "        # than once per axis per rail.\n"
        "        for axis in homing_axes:\n"
        "            if isinstance(axis, str):\n"
        '                axis = "xyz".index(axis)\n'
        "            if self.dc_module and axis == self.dc_module.axis:\n"
        "                rail = self.dc_module.get_primary_rail().get_rail()\n"
        "            else:\n"
        "                rail = self.rails[axis]\n"
        "            self.limits[axis] = rail.get_range()\n" % MARKER,
        "kinematics: set_position",
    )
    text = sub_once(
        text,
        "    def note_z_not_homed(self):\n"
        "        # Helper for Safe Z Home\n"
        "        self.limits[2] = (1.0, -1.0)\n",
        "    # %s: Kalico calls this from stepper_enable.motor_off (every M84),\n"
        "    # from force_move's SET_KINEMATIC_POSITION and from safe_z_home.\n"
        "    def clear_homing_state(self, clear_axes):\n"
        '        for axis, axis_name in enumerate("xyz"):\n'
        "            if axis_name in clear_axes:\n"
        "                self.limits[axis] = (1.0, -1.0)\n"
        "    def note_z_not_homed(self):\n"
        "        # Helper for Safe Z Home (pre-Kalico Klipper API)\n"
        '        self.clear_homing_state("z")\n' % MARKER,
        "kinematics: clear_homing_state",
    )
    return text


def t_ratos_homing(text, cfg):
    """Hand Kalico an axis name instead of an axis index.

    This is coupled to the kinematics transform and must ship with it: Kalico's
    toolhead.set_position does no validation, it passes homing_axes straight
    through to the kinematics. Once the kinematics accepts both forms this call
    would in fact still work -- but only by accident of our own compatibility
    shim. Say what we mean.

    The path is hit on every G28 while Z is unhomed, i.e. every cold start.
    """
    text = sub_once(
        text,
        "                    toolhead.set_position(pos, homing_axes=[2])",
        '                    # %s: Kalico\'s contract is axis names, not indices.\n'
        '                    toolhead.set_position(pos, homing_axes="z")' % MARKER,
        "ratos_homing: set_position homing_axes",
    )
    text = sub_once(
        text,
        '                    if hasattr(toolhead.get_kinematics(), "note_z_not_homed"):\n'
        "                        toolhead.get_kinematics().note_z_not_homed()",
        "                    # %s: prefer Kalico's clear_homing_state, fall back\n"
        "                    # to the older note_z_not_homed.\n"
        "                    kin = toolhead.get_kinematics()\n"
        '                    if hasattr(kin, "clear_homing_state"):\n'
        '                        kin.clear_homing_state("z")\n'
        '                    elif hasattr(kin, "note_z_not_homed"):\n'
        "                        kin.note_z_not_homed()" % MARKER,
        "ratos_homing: clear_homing_state",
    )
    return text


def t_resonance_generator(text, cfg):
    """Adapt to Kalico's widened ResonanceTestExecutor.run_test signature.

    Klipper:  run_test(self, test_seq, axis, gcmd)
    Kalico:   run_test(self, test_seq, axis, freq_end, accel_per_hz, gcmd)

    RatOS calls the 3-argument form, which raises TypeError on Kalico, so
    GENERATE_RESONANCES is dead today.

    Call Kalico's real five-argument `run_test`, with the same arguments
    Kalico's own caller uses. Do NOT reach for its private `_run_test`: Kalico
    hoisted the input-shaper disable and the SET_VELOCITY_LIMIT overrides *out*
    of `_run_test` and into `run_test`'s `suspend_limits()` context manager, so
    calling `_run_test` directly would run the whole sweep with input shaping
    still enabled and at the printer's normal accel limits -- silently, and the
    resulting shaper graphs would be wrong rather than obviously broken.

    `self.generator` is a SweepingVibrationsTestGenerator, which exposes the
    inner VibrationPulseTestGenerator as `.vibration_generator` -- that is where
    freq_end and accel_per_hz live. (The sweeping generator itself has no
    get_accel_per_hz.)

    Separately, keep the whole position vector rather than unpacking a fixed
    four-tuple: Kalico's toolhead can append extra axes to commanded_pos. The
    two move() calls have to carry that tail through, otherwise
    `commanded_pos[:] = move.end_pos` truncates it.
    """
    text = sub_once(
        text,
        "from toolhead import ToolHead\n"
        "from . import resonance_tester\n",
        "import inspect\n"
        "\n"
        "from toolhead import ToolHead\n"
        "from . import resonance_tester\n",
        "resonance_generator: inspect import",
    )
    text = sub_once(
        text,
        "        X, Y, Z, E = toolhead.get_position()\n",
        "        # %s: Kalico's toolhead can carry extra axes beyond X/Y/Z/E, so\n"
        "        # keep the whole vector and only name the three we move.\n"
        "        pos = list(toolhead.get_position())\n"
        "        X, Y, Z = pos[:3]\n" % MARKER,
        "resonance_generator: get_position unpack",
    )
    text = sub_once(
        text,
        "            toolhead.move([nX, nY, Z, E], max_v)\n"
        "            toolhead.move([X, Y, Z, E], max_v)\n",
        "            # %s: carry every axis past Y at its current value.\n"
        "            toolhead.move([nX, nY] + pos[2:], max_v)\n"
        "            toolhead.move(list(pos), max_v)\n" % MARKER,
        "resonance_generator: move calls keep the extra-axis tail",
    )
    text = sub_once(
        text,
        "            self.executor.run_test(test_seq, axis, gcmd)",
        "            # %s: Kalico widened run_test to\n"
        "            # (test_seq, axis, freq_end, accel_per_hz, gcmd). Its\n"
        "            # suspend_limits() wrapper is what disables the input shaper\n"
        "            # and raises the accel limits for the sweep, so the public\n"
        "            # form is required -- calling the private _run_test would\n"
        "            # measure with shaping still on.\n"
        "            if len(\n"
        "                inspect.signature(self.executor.run_test).parameters\n"
        "            ) > 3:\n"
        "                _vg = self.generator.vibration_generator\n"
        "                self.executor.run_test(\n"
        "                    test_seq, axis, _vg.freq_end, _vg.accel_per_hz, gcmd\n"
        "                )\n"
        "            else:\n"
        "                self.executor.run_test(test_seq, axis, gcmd)" % MARKER,
        "resonance_generator: run_test dispatch",
    )
    return text


def t_klippy_requirements(text, cfg):
    """Write the numpy/scipy constraint down where an installer can see it.

    RatOS pins `pygam==0.9.1`. pygam's own metadata then caps scipy at
    `>=1.11.1,<1.12`, and every scipy in that window declares
    `numpy>=1.21.6,<1.28` -- so pygam transitively forbids numpy 2. Nothing on
    the printer states that: this file names only pygam, beacon's file asks for
    unbounded `numpy>=1.16.6` / `scipy>=1.2.3`, and the real constraint lives
    inside pygam's wheel metadata where no installer preserves it across runs.

    That matters because four things pip into the same venv -- this file via
    ratos-update.sh on every configurator merge, and moonraker's update_manager
    entries for klipper, beacon and LinearMovementAnalysis, all with `-U -r`.
    Whichever ran last wins, so a printer can boot fine and then break after an
    unrelated update, with no config change to blame.

    On Kalico it stops being a drift risk and becomes a boot blocker: Kalico
    imports numpy at module level in webhooks.py, and this printer loads
    [beacon_adaptive_heat_soak], whose module imports pygam. Both have to work.

    So pin all three here. The fork's Kalico branch holds numpy below 2 to
    match; the two files have to agree or they fight on every update.
    """
    return sub_once(
        text,
        "pygam==0.9.1\n",
        "pygam==0.9.1\n"
        "\n"
        "# %s: pygam 0.9.1 caps scipy at <1.12, and no scipy below 1.12 supports\n"
        "# numpy 2 -- so pygam transitively forbids it. That constraint is only\n"
        "# visible inside pygam's metadata, which no installer here preserves, and\n"
        "# four separate pip runs write this venv. State it explicitly so every one\n"
        "# of them converges on the same working set instead of fighting.\n"
        "#\n"
        "# The fork's Kalico branch holds numpy below 2 to match. Both files have\n"
        "# to agree; changing one alone reintroduces the ping-pong.\n"
        "#\n"
        "# To move to numpy 2 later: a pygam that allows scipy >= 1.13 is needed.\n"
        "# pygam's main branch declares 0.10.1 with scipy<1.17, but no such release\n"
        "# is tagged, so verify it exists on PyPI from the printer before trying.\n"
        "numpy>=1.26.4,<2\n"
        "scipy>=1.11.1,<1.12\n" % MARKER,
        "klippy/requirements.txt: pin numpy and scipy",
    )


def t_drop_gcode_shell_extension(text, cfg):
    """Stop registering RatOS' gcode_shell_command.py -- Kalico ships its own.

    Kalico TRACKS klippy/extras/gcode_shell_command.py. RatOS symlinks its copy
    over exactly that path and whitelists it in .git/info/exclude, which makes
    git treat it as expendable: the migration's `git reset --hard` silently
    replaces the symlink with Kalico's file. After that, symlinkExtensions skips
    re-linking because the destination exists, and verify_registered_extensions
    still reports "properly registered" because it only inspects the source path
    in printer_data.

    So the swap is invisible in both directions -- there is no diagnostic that
    would ever surface it. Cede the file deliberately instead.

    Worse, if the fork's Kalico branch were instead made to `git rm` the file,
    the symlink-vs-file mode conflict shows up in `git diff-index` and the
    migration aborts permanently with KLIPPER_UNCOMMITTED_CHANGES.

    Behavioural difference: Kalico's implementation additionally runs
    os.path.expandvars() over the command.
    """
    return sub_once(
        text,
        '        ["gcode_shell_extension"]=$(realpath "${RATOS_PRINTER_DATA_DIR}/config/RatOS/klippy/gcode_shell_command.py")\n',
        "        # %s: Kalico ships klippy/extras/gcode_shell_command.py itself and\n"
        "        # tracks it in git, so RatOS' symlink is clobbered by every\n"
        "        # `git reset --hard` -- silently, because the verifier only checks\n"
        "        # the source path. Cede the file to Kalico rather than fight it.\n" % MARKER,
        "ratos-common.sh: drop gcode_shell_extension",
    )


def t_sweeping_period(path_text_pairs, cfg):
    """Pin sweeping_period so shaper results do not silently change.

    Klipper defaults [resonance_tester] sweeping_period to 1.2; Kalico defaults
    it to 0.0, which makes `if self.test_sweeping_period:` false and degenerates
    the sweep into plain vibration pulses. No RatOS printer profile sets it, so
    the change is silent -- it just alters what SHAPER_CALIBRATE,
    TEST_RESONANCES and RatOS' own GENERATE_RESONANCES measure.

    Pinning RatOS' inherited value keeps results comparable with RatOS on
    Klipper. Pass --no-sweeping-period to inherit Kalico's default instead.
    """
    out = []
    for path, text in path_text_pairs:
        if "sweeping_period" in text:
            out.append((path, text))
            continue
        patched = re_sub_once(
            text,
            r"\[resonance_tester\]\n",
            lambda _m: "[resonance_tester]\n"
            "# %s: Kalico defaults sweeping_period to 0.0, which silently turns\n"
            "# the sweep into plain vibration pulses. Klipper's default is 1.2;\n"
            "# pin it so shaper results stay comparable with RatOS on Klipper.\n"
            "sweeping_period: 1.2\n" % MARKER,
            "%s: [resonance_tester] sweeping_period" % os.path.basename(path),
        )
        out.append((path, patched))
    return out


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

FILE_TRANSFORMS = [
    (
        "configuration/scripts/klipper-fork-migration.sh",
        [
            t_migration_url_normalization,
            t_migration_constants,
            t_migration_allowlist,
            t_migration_yield_kalico_owned,
        ],
    ),
    (
        "configuration/moonraker.conf",
        [t_moonraker_klipper_pin, t_moonraker_configurator],
    ),
    (
        "configuration/klippy/kinematics/ratos_hybrid_corexy.py",
        [t_kinematics],
    ),
    ("configuration/klippy/ratos_homing.py", [t_ratos_homing]),
    ("configuration/klippy/resonance_generator.py", [t_resonance_generator]),
    ("configuration/scripts/ratos-common.sh", [t_drop_gcode_shell_extension]),
    ("configuration/klippy/requirements.txt", [t_klippy_requirements]),
    ("src/server/helpers/klipper-config.ts", [t_tmc2240_rref]),
    ("configuration/z-probe/beacon.cfg", [t_beacon_homing_retract]),
    ("configuration/macros/led_control.cfg", [t_led_vaoc_pwm]),
    ("src/scripts/check-version.py", [t_check_version_package_import]),
]


WORKFLOW_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publish-kalico.yml.in")
WORKFLOW_DEST = ".github/workflows/publish-kalico.yml"


def workflow_plan(checkout, cfg):
    """Install the fork's publish workflow and disarm upstream's.

    Moonraker does not pull the source branch -- it pulls a branch carrying a
    built Next.js app, which RatOS produces in CI. Without an equivalent
    workflow the fork has no such branch, and pointing a printer at the source
    branch leaves the configurator service with nothing to serve.

    Upstream's own publish workflows are removed from the fork. They target
    RatOS' branch names, and leaving live workflows in a fork that push to
    branches nobody is watching is a trap, not a feature.

    Returns (writes, deletes); either may be empty.
    """
    with open(WORKFLOW_TEMPLATE, "r") as handle:
        text = handle.read()

    html_url = cfg["configurator_fork_url"]
    if html_url.endswith(".git"):
        html_url = html_url[: -len(".git")]

    for token, value in (
        ("@@SOURCE_BRANCH@@", cfg["source_branch"]),
        ("@@DEPLOYMENT_BRANCH@@", cfg["deployment_branch"]),
        ("@@FORK_URL_HTML@@", html_url),
    ):
        if token not in text:
            raise AnchorError(
                "publish-kalico.yml.in no longer contains %s -- the template "
                "and this installer have drifted apart" % token
            )
        text = text.replace(token, value)

    writes = [(os.path.join(checkout, WORKFLOW_DEST), text)]

    deletes = []
    wf_dir = os.path.join(checkout, ".github", "workflows")
    if os.path.isdir(wf_dir):
        for name in sorted(os.listdir(wf_dir)):
            if name.startswith("publish") and name != os.path.basename(WORKFLOW_DEST):
                deletes.append(os.path.join(wf_dir, name))
    return writes, deletes


PRINTER600_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "printer-600")
PRINTER600_ID = "v-core-4-1-idex-600"
PRINTER600_STOCK = "v-core-4-1-idex"
PRINTER600_IMAGE = "v-core-4-idex.png"


def printer_600_plan(checkout, cfg):
    """Ship "V-Core 4.1 IDEX 600" as a real printer type.

    RatOS offers this machine only at 300/400/500. Picking 500 and hand-editing
    the result is what the printer does today, and it costs the ability to
    regenerate at all -- the generated config had to be renamed so the
    configurator would stop overwriting it.

    It has to be a whole printer type, not just an extra size, because
    ``bedMargin`` is a per-printer field: the schema only allows x/y/z inside a
    size entry. This frame's margins are [75, 75] / [1, 65] against the stock
    [60.6, 60.6] / [14.35, 33.65], and those margins are what the configurator
    derives the axis limits, both parking positions and ``variable_bed_margin_*``
    from. A 600 size on the stock printer would still generate wrong numbers.

    Two placements are forced rather than chosen:

    * The definition goes in its own directory because the configurator globs
      ``printers/*/printer-definition.json`` at runtime and takes the printer id
      from the directory name.
    * ``600.cfg`` goes in the *stock* printer's directory, because the shared
      template hardcodes ``[include RatOS/printers/v-core-4-1-idex/${size}.cfg]``.

    The definition reuses the stock ``v-core-4-1-idex.ts`` template, so no new
    template has to be bundled -- only printer definitions are read at runtime.

    Returns (text_writes, binary_copies).
    """
    printers = os.path.join(checkout, "configuration", "printers")
    stock = os.path.join(printers, PRINTER600_STOCK)
    if not os.path.isdir(stock):
        raise AnchorError(
            "the stock printer directory %s is gone -- RatOS has restructured "
            "configuration/printers and the 600 type needs re-homing" % PRINTER600_STOCK
        )
    stock_image = os.path.join(stock, PRINTER600_IMAGE)
    if not os.path.isfile(stock_image):
        raise AnchorError(
            "%s no longer ships %s; the 600 printer type would appear without a "
            "picture" % (PRINTER600_STOCK, PRINTER600_IMAGE)
        )

    dest = os.path.join(printers, PRINTER600_ID)
    writes = []
    for name in ("printer-definition.json", "printer.cfg.overrides"):
        with open(os.path.join(PRINTER600_SRC, name), "r") as handle:
            writes.append((os.path.join(dest, name), handle.read()))
    with open(os.path.join(PRINTER600_SRC, "600.cfg"), "r") as handle:
        writes.append((os.path.join(stock, "600.cfg"), handle.read()))

    copies = [(stock_image, os.path.join(dest, PRINTER600_IMAGE))]
    return writes, copies


def find_resonance_tester_files(checkout):
    """Every shipped .cfg that opens a [resonance_tester] section."""
    hits = []
    base = os.path.join(checkout, "configuration")
    for root, _dirs, files in os.walk(base):
        for name in files:
            if not name.endswith(".cfg"):
                continue
            path = os.path.join(root, name)
            with open(path, "r") as handle:
                text = handle.read()
            if re.search(r"^\[resonance_tester\]", text, re.MULTILINE):
                hits.append(path)
    return sorted(hits)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--kalico-upstream-url", default="https://github.com/KalicoCrew/kalico.git")
    parser.add_argument("--kalico-url", required=True)
    parser.add_argument("--kalico-branch", required=True)
    parser.add_argument("--kalico-commit", required=True)
    parser.add_argument("--configurator-url", required=True)
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--deployment-branch", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-sweeping-period", action="store_true")
    parser.add_argument("--no-printer-600", action="store_true")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[0-9a-f]{40}", args.kalico_commit):
        # klipper-fork-migration.sh validates this with ^[a-fA-F0-9]{40}$ and
        # exits 3 on a mismatch -- and because of an ERR-trap interaction the
        # operator sees a generic SCRIPT_ERROR rather than the real cause.
        # Catch it here where the message is useful.
        sys.stderr.write(
            "ERROR: --kalico-commit must be a full 40-char lowercase SHA, got %r\n"
            % args.kalico_commit
        )
        return 2

    cfg = {
        "kalico_upstream_url": args.kalico_upstream_url,
        "kalico_fork_url": args.kalico_url,
        "kalico_fork_branch": args.kalico_branch,
        "kalico_commit": args.kalico_commit,
        "configurator_fork_url": args.configurator_url,
        "source_branch": args.source_branch,
        "deployment_branch": args.deployment_branch,
    }

    checkout = os.path.abspath(args.checkout)
    if not os.path.isdir(os.path.join(checkout, "configuration")):
        sys.stderr.write("ERROR: %s is not a RatOS-configurator checkout\n" % checkout)
        return 2

    targets = list(FILE_TRANSFORMS)
    pending = []
    already = 0

    for rel, transforms in targets:
        path = os.path.join(checkout, rel)
        if not os.path.isfile(path):
            sys.stderr.write("ERROR: missing %s\n" % rel)
            return 1
        with open(path, "r") as handle:
            original = handle.read()
        if MARKER in original:
            already += 1
            continue
        text = original
        try:
            for transform in transforms:
                text = transform(text, cfg)
        except AnchorError as exc:
            sys.stderr.write("ERROR in %s\n  %s\n" % (rel, exc))
            return 1
        pending.append((path, text))

    if not args.no_sweeping_period:
        res_files = find_resonance_tester_files(checkout)
        if not res_files:
            sys.stderr.write(
                "ERROR: no shipped .cfg declares [resonance_tester]; RatOS has "
                "restructured its config tree, review the transform.\n"
            )
            return 1
        pairs = []
        for path in res_files:
            with open(path, "r") as handle:
                pairs.append((path, handle.read()))
        try:
            for path, text in t_sweeping_period(pairs, cfg):
                with open(path, "r") as handle:
                    if handle.read() != text:
                        pending.append((path, text))
        except AnchorError as exc:
            sys.stderr.write("ERROR: %s\n" % exc)
            return 1

    binary_copies = []
    if not args.no_printer_600:
        try:
            p600_writes, p600_copies = printer_600_plan(checkout, cfg)
        except (AnchorError, OSError) as exc:
            sys.stderr.write("ERROR: printer type 600: %s\n" % exc)
            return 1
        for path, text in p600_writes:
            existing = None
            if os.path.isfile(path):
                with open(path, "r") as handle:
                    existing = handle.read()
            if existing != text:
                pending.append((path, text))
        for src, dst in p600_copies:
            if not os.path.isfile(dst) or open(src, "rb").read() != open(dst, "rb").read():
                binary_copies.append((src, dst))

    try:
        wf_writes, wf_deletes = workflow_plan(checkout, cfg)
    except AnchorError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 1
    for path, text in wf_writes:
        existing = None
        if os.path.isfile(path):
            with open(path, "r") as handle:
                existing = handle.read()
        if existing != text:
            pending.append((path, text))

    if args.check:
        if wf_deletes:
            print("%d upstream publish workflow(s) still present" % len(wf_deletes))
            for path in wf_deletes:
                print("  %s" % os.path.relpath(path, checkout))
            return 1
        if binary_copies:
            print("%d binary file(s) need installing" % len(binary_copies))
            for _src, dst in binary_copies:
                print("  %s" % os.path.relpath(dst, checkout))
            return 1
        if pending:
            print("%d file(s) need patching, %d already patched" % (len(pending), already))
            for path, _text in pending:
                print("  %s" % os.path.relpath(path, checkout))
            return 1
        print("checkout is fully patched (%d files carry the %s marker)" % (already, MARKER))
        return 0

    for src, dst in binary_copies:
        parent = os.path.dirname(dst)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(src, "rb") as rh, open(dst, "wb") as wh:
            wh.write(rh.read())
        print("installed %s" % os.path.relpath(dst, checkout))

    for path in wf_deletes:
        os.unlink(path)
        print("removed %s (upstream publish workflow)" % os.path.relpath(path, checkout))

    for path, text in pending:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w") as handle:
            handle.write(text)
        print("patched %s" % os.path.relpath(path, checkout))

    if already:
        print("(%d file(s) already carried the %s marker and were left alone)" % (already, MARKER))
    if not pending and not already:
        print("nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
