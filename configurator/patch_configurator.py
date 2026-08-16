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

Usage:
    patch_configurator.py --checkout DIR --kalico-url URL --kalico-branch NAME
                          --kalico-commit SHA
                          --configurator-url URL --deployment-branch NAME
                          [--check] [--no-sweeping-period]

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

    We dispatch to Kalico's `_run_test`, not to its 5-argument `run_test`.
    `run_test` wraps the body in `suspend_limits(...)`, which would fight
    RatOS' own SET_VELOCITY_LIMIT handling in this very module; `_run_test` is
    byte-for-byte the Klipper body RatOS was written against.

    Also make the toolhead position unpack defensive: Kalico's toolhead carries
    an extra_axes mechanism, so a fixed 4-tuple unpack is a latent break.
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
        "        # %s: Kalico's toolhead can carry extra axes, so slice rather\n"
        "        # than unpack a fixed 4-tuple.\n"
        "        X, Y, Z = toolhead.get_position()[:3]\n" % MARKER,
        "resonance_generator: get_position unpack",
    )
    text = sub_once(
        text,
        "            self.executor.run_test(test_seq, axis, gcmd)",
        "            # %s: Kalico widened run_test to\n"
        "            # (test_seq, axis, freq_end, accel_per_hz, gcmd) and wraps the\n"
        "            # body in suspend_limits(), which would fight the velocity\n"
        "            # limits this module sets itself. Its _run_test is the\n"
        "            # unchanged Klipper body -- use that when present.\n"
        "            _run = getattr(self.executor, \"_run_test\", None)\n"
        "            if _run is not None and len(\n"
        "                inspect.signature(self.executor.run_test).parameters\n"
        "            ) > 3:\n"
        "                _run(test_seq, axis, gcmd)\n"
        "            else:\n"
        "                self.executor.run_test(test_seq, axis, gcmd)" % MARKER,
        "resonance_generator: run_test dispatch",
    )
    return text


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
]


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
    parser.add_argument("--deployment-branch", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-sweeping-period", action="store_true")
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

    if args.check:
        if pending:
            print("%d file(s) need patching, %d already patched" % (len(pending), already))
            for path, _text in pending:
                print("  %s" % os.path.relpath(path, checkout))
            return 1
        print("checkout is fully patched (%d files carry the %s marker)" % (already, MARKER))
        return 0

    for path, text in pending:
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
