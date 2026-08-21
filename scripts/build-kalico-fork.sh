#!/usr/bin/env bash
#
# Build the fork's Klipper replacement: Kalico plus the RatOS compatibility
# port, on branch $FORK_KALICO_BRANCH.
#
# The resulting commit SHA is what configuration/moonraker.conf must pin, so
# this script must run before build-configurator-fork.sh. It prints the SHA on
# the last line and writes it to .work/kalico-commit.txt.
#
# Usage:
#   scripts/build-kalico-fork.sh [--push] [--base <commit-ish>]
#
#   --push          push $FORK_KALICO_BRANCH and the recovery branch to
#                   $FORK_KALICO_URL
#   --base REF      build on top of REF instead of the upstream branch tip
#                   (use $VERIFIED_KALICO_COMMIT to reproduce a known-good build)

SCRIPT_DIR="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" &>/dev/null && pwd)"
# shellcheck source=scripts/lib.sh
source "$SCRIPT_DIR/lib.sh"

PUSH=0
BASE=""

while [ $# -gt 0 ]; do
	case "$1" in
	--push) PUSH=1 ;;
	--base)
		shift
		BASE="${1:-}"
		[ -n "$BASE" ] || die "--base needs a commit-ish"
		;;
	-h | --help)
		sed -n '2,/^[^#]/p' "$0" | sed -n 's/^# \{0,1\}//p'
		exit 0
		;;
	*) die "unknown option: $1" ;;
	esac
	shift
done

need git
need python3
ensure_work_dir

PATCH="$REPO_ROOT/kalico/0001-ratos-compat-bed_mesh-gcode_macro.patch"
[ -f "$PATCH" ] || die "missing $PATCH"

CHECKOUT="$WORK_DIR/kalico"
ensure_checkout "$CHECKOUT" "$UPSTREAM_KALICO_URL" "$UPSTREAM_KALICO_BRANCH"
warn_if_upstream_moved "$CHECKOUT" "$VERIFIED_KALICO_COMMIT" "Kalico"

if [ -z "$BASE" ]; then
	BASE="$(git -C "$CHECKOUT" rev-parse FETCH_HEAD)"
fi
say "Building $FORK_KALICO_BRANCH on top of $BASE"

# Rebuild the branch from scratch every time. The fork's delta is a single
# derived commit, so there is no history worth preserving on this branch -- and
# re-deriving is what keeps a Kalico bump from turning into a conflict replay.
git -C "$CHECKOUT" checkout --quiet -B "$FORK_KALICO_BRANCH" "$BASE"

say "Applying the RatOS compatibility port"
APPLY_ERR=""
APPLY_RC=0
APPLY_ERR="$(git -C "$CHECKOUT" apply --check "$PATCH" 2>&1 >/dev/null)" || APPLY_RC=$?
if [ "$APPLY_RC" -ne 0 ]; then
	printf '%s\n' "$APPLY_ERR" >&2
	die "the RatOS port does not apply to Kalico at $BASE.
    Kalico has changed bed_mesh.py or gcode_macro.py where the port anchors.
    Re-derive it -- see kalico/PROVENANCE.md and docs/MAINTENANCE.md. Do NOT
    ship a partially applied firmware patch."
fi
git -C "$CHECKOUT" apply "$PATCH"

say "Verifying the result"
python3 -m py_compile \
	"$CHECKOUT/klippy/extras/bed_mesh.py" \
	"$CHECKOUT/klippy/extras/gcode_macro.py" ||
	die "patched files do not compile"

# These three are the hard startup blockers. If any is missing the printer will
# not boot Klippy, so check for them explicitly rather than trusting the patch
# to have contained what we think it contained.
grep -q 'def __init__(self, params, name, reactor=None)' "$CHECKOUT/klippy/extras/bed_mesh.py" ||
	die "ZMesh still takes only two arguments -- ratos.py:445 would raise TypeError"
grep -q 'minval=0.001' "$CHECKOUT/klippy/extras/bed_mesh.py" ||
	die "split_delta_z minval was not relaxed -- Klippy would refuse to start"
grep -q '"log_points"' "$CHECKOUT/klippy/extras/bed_mesh.py" ||
	die "log_points option missing -- Klippy would reject beacon.cfg"
note "ZMesh reactor parameter, split_delta_z minval and log_points all present"

# RatOS pins pygam==0.9.1, which caps scipy below 1.12, and no such scipy
# supports numpy 2 -- so a numpy-2 venv makes `import pygam` fail and takes
# [beacon_adaptive_heat_soak] down at config load. Both requirements files the
# fork owns must agree on holding numpy below 2. See docs/RISKS.md section 1.
grep -q "numpy>=1.26.4,<2" "$CHECKOUT/scripts/klippy-requirements.txt" ||
	die "scripts/klippy-requirements.txt does not hold numpy below 2 -- a numpy-2
    venv breaks pygam and the printer will not reach ready"
grep -q "numpy>=1.26.4,<2" "$CHECKOUT/pyproject.toml" ||
	die "pyproject.toml does not hold numpy below 2"
note "numpy held below 2 in both requirements and pyproject"

if command -v ruff >/dev/null 2>&1; then
	(cd "$CHECKOUT" && ruff format --check klippy/extras/bed_mesh.py klippy/extras/gcode_macro.py >/dev/null 2>&1) &&
		note "ruff format: clean" ||
		warn "ruff format reports differences; Kalico CI gates on this"
else
	note "ruff not installed, skipping the format check Kalico CI enforces"
fi

say "Committing"
# Stage exactly what the patch touches, derived from the patch rather than
# hardcoded: a widened patch with a hardcoded add list commits some of its
# files and silently drops the rest.
PATCH_FILES="$(git -C "$CHECKOUT" apply --numstat "$PATCH" | cut -f3)"
[ -n "$PATCH_FILES" ] || die "could not determine which files the patch touches"
# shellcheck disable=SC2086
git -C "$CHECKOUT" add -- $PATCH_FILES
# Pin the dates to the base commit so the build is a pure function of
# (base, patch). The resulting SHA is what moonraker.conf pins, so a rebuild
# that produces a different SHA for identical inputs is a real problem.
BASE_DATE="$(git -C "$CHECKOUT" show -s --format=%aI "$BASE")"
# BOTH dates. `commit --date=` sets only the AUTHOR date; the committer date
# still comes from the clock, and it is part of the SHA -- so pinning one of
# them leaves the build non-reproducible while looking like it is fixed.
GIT_COMMITTER_DATE="$BASE_DATE" \
	git -C "$CHECKOUT" -c user.name="RatOS-Kalico build" \
	-c user.email="noreply@localhost" \
	commit --quiet --date="$BASE_DATE" -m "RatOS compatibility for Kalico: bed_mesh and gcode_macro

Ports the whole delta of Rat-OS/klipper ratos/v2.1.x onto Kalico. RatOS 2.1
does not run on stock Klipper; it runs on that fork, whose entire difference
from upstream Klipper b7233d11 is six commits in two files.

Three parts are hard startup blockers for RatOS on Kalico:

  * ZMesh gains its reactor parameter. Three RatOS call sites pass a third
    argument -- ratos.py:445 in the core extension, plus beacon_mesh.py:519
    and :1285 -- and Kalico's two-argument constructor raises TypeError.
  * split_delta_z minval drops from 0.01 to 0.001. Six V-Core 4 profiles ship
    0.001 and Klippy otherwise refuses to start.
  * log_points / log_points_truncate are introduced. z-probe/beacon.cfg sets
    log_points, and Kalico's error_on_unused_config_options defaults to True.

The remainder is the reactor-yield work those commits exist for: it stops a
large Beacon mesh from blocking the Klippy greenlet and tripping 'Timer too
close'. BedMesh.update_status is additionally made atomic, because adding
yields to the status getters without it widens an existing torn-read race.

This is a hand port of the end state, not a replay of the series: none of the
six patches apply to Kalico, which is ruff-formatted and whose bed_mesh forked
before Klipper's ProbeManager refactor. See kalico/PROVENANCE.md.

Co-authored-by: Tom Glastonbury <t@tg73.net>

Upstream commits ported:
  8c94072b15a2ad502fbe047636f44547b6d9eb12
  93c8ad8a017c45f4502e73ad1dc174cf7d3c3b90
  61db5410da4072e55cb38de78397fabad4be6a7e
  33ffa6276d6b9dce2cecaed06357109dbdf8f27b
  7dd901ecc5c1ba6c1174bff8895ac14f26b26988
  2817b348e23c779b68ae5f27f2b9b9af8cfcf0da"

KALICO_COMMIT="$(git -C "$CHECKOUT" rev-parse HEAD)"

# Moonraker's built-in klipper updater cannot be told which branch to track:
# primary_branch is not overridable and defaults to "master". Its Recover
# button checks that branch out, and Hard Recover rmtree's ~/klipper and clones
# it. Without this alias those buttons strand the printer on unrelated code, or
# on nothing at all.
say "Updating recovery alias branch '$FORK_KALICO_RECOVERY_BRANCH'"
git -C "$CHECKOUT" branch --quiet -f "$FORK_KALICO_RECOVERY_BRANCH" "$KALICO_COMMIT"

mkdir -p "$WORK_DIR"
printf '%s\n' "$KALICO_COMMIT" >"$WORK_DIR/kalico-commit.txt"

if [ "$PUSH" -eq 1 ]; then
	say "Pushing to $FORK_KALICO_URL"
	# Via a named remote, so --force-with-lease has a remote-tracking ref to
	# derive its lease from. --atomic keeps a rejected lease from leaving the
	# recovery branch pointing somewhere the tracked branch does not.
	ensure_fork_remote "$CHECKOUT" "$FORK_KALICO_URL"
	git -C "$CHECKOUT" push --atomic --force-with-lease fork \
		"$FORK_KALICO_BRANCH:$FORK_KALICO_BRANCH" \
		"$FORK_KALICO_RECOVERY_BRANCH:$FORK_KALICO_RECOVERY_BRANCH"
	note "pushed $FORK_KALICO_BRANCH and $FORK_KALICO_RECOVERY_BRANCH"
	warn "Never force-push away a commit a printer has already pinned:"
	note "moonraker reports a vanished pinned_commit as 'up to date' forever."
else
	note "not pushed (pass --push)"
fi

say "Done. Kalico fork commit:"
printf '%s\n' "$KALICO_COMMIT"
