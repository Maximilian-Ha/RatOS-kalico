#!/usr/bin/env bash
#
# Build the RatOS-configurator fork branch: upstream RatOS 2.1 plus the whole
# Kalico delta from configurator/patch_configurator.py.
#
# Run scripts/build-kalico-fork.sh first -- moonraker.conf has to pin the
# Kalico fork's commit, and this script reads it from .work/kalico-commit.txt
# unless you pass --kalico-commit.
#
# Usage:
#   scripts/build-configurator-fork.sh [--push] [--kalico-commit SHA]
#                                      [--base <commit-ish>] [--no-sweeping-period]
#
# NOTE: this produces the SOURCE branch. Moonraker pulls the DEPLOYMENT branch,
# which carries a built Next.js app -- see docs/MAINTENANCE.md, "The deployment
# branch". Building that requires pnpm and RatOS' own CI workflow.

SCRIPT_DIR="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" &>/dev/null && pwd)"
# shellcheck source=scripts/lib.sh
source "$SCRIPT_DIR/lib.sh"

PUSH=0
BASE=""
KALICO_COMMIT=""
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
	case "$1" in
	--push) PUSH=1 ;;
	--kalico-commit)
		shift
		KALICO_COMMIT="${1:-}"
		;;
	--base)
		shift
		BASE="${1:-}"
		;;
	--no-sweeping-period) EXTRA_ARGS+=(--no-sweeping-period) ;;
	-h | --help)
		sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*) die "unknown option: $1" ;;
	esac
	shift
done

need git
need python3

if [ -z "$KALICO_COMMIT" ]; then
	[ -f "$WORK_DIR/kalico-commit.txt" ] ||
		die "no Kalico commit known. Run scripts/build-kalico-fork.sh first, or
    pass --kalico-commit <sha>."
	KALICO_COMMIT="$(cat "$WORK_DIR/kalico-commit.txt")"
fi
say "Pinning klipper to $KALICO_COMMIT"

CHECKOUT="$WORK_DIR/configurator"
ensure_checkout "$CHECKOUT" "$UPSTREAM_CONFIGURATOR_URL" "$UPSTREAM_CONFIGURATOR_BRANCH"
warn_if_upstream_moved "$CHECKOUT" "$VERIFIED_CONFIGURATOR_COMMIT" "RatOS-configurator"

if [ -z "$BASE" ]; then
	BASE="$(git -C "$CHECKOUT" rev-parse FETCH_HEAD)"
fi
say "Building $FORK_CONFIGURATOR_BRANCH on top of $BASE"
git -C "$CHECKOUT" checkout --quiet -B "$FORK_CONFIGURATOR_BRANCH" "$BASE"

say "Applying the Kalico delta"
python3 "$REPO_ROOT/configurator/patch_configurator.py" \
	--checkout "$CHECKOUT" \
	--kalico-upstream-url "$UPSTREAM_KALICO_URL" \
	--kalico-url "$FORK_KALICO_URL" \
	--kalico-branch "$FORK_KALICO_BRANCH" \
	--kalico-commit "$KALICO_COMMIT" \
	--configurator-url "$FORK_CONFIGURATOR_URL" \
	--deployment-branch "$FORK_CONFIGURATOR_DEPLOYMENT_BRANCH" \
	"${EXTRA_ARGS[@]}"

say "Verifying the result"
bash -n "$CHECKOUT/configuration/scripts/klipper-fork-migration.sh" ||
	die "klipper-fork-migration.sh is not valid bash after patching"
bash -n "$CHECKOUT/configuration/scripts/ratos-common.sh" ||
	die "ratos-common.sh is not valid bash after patching"
python3 -m py_compile \
	"$CHECKOUT/configuration/klippy/kinematics/ratos_hybrid_corexy.py" \
	"$CHECKOUT/configuration/klippy/ratos_homing.py" \
	"$CHECKOUT/configuration/klippy/resonance_generator.py" ||
	die "patched klippy modules do not compile"

# The migration script re-reads this value with an awk parser that demands
# exactly 40 hex characters and, thanks to an ERR-trap interaction, reports a
# generic SCRIPT_ERROR rather than the real cause when it fails. Check it here.
PINNED="$(awk '/^\[update_manager klipper\]/{f=1} f && /^pinned_commit:/{gsub(/^pinned_commit:[ \t]*/,"");gsub(/[ \t\r]*$/,"");print;exit}' \
	"$CHECKOUT/configuration/moonraker.conf")"
[ "$PINNED" = "$KALICO_COMMIT" ] ||
	die "moonraker.conf pins '$PINNED' but we built '$KALICO_COMMIT'"
note "moonraker.conf pins the built Kalico commit, and awk-parses cleanly"

say "Committing"
git -C "$CHECKOUT" add -A
git -C "$CHECKOUT" -c user.name="RatOS-Kalico build" \
	-c user.email="noreply@localhost" \
	commit --quiet -m "RatOS 2.1 on Kalico

Repoints RatOS' update machinery at a Kalico-based firmware and adapts the
klippy extensions to Kalico's API.

klipper-fork-migration.sh is the load-bearing change. ratos-update.sh runs it
first on every update, and it decides whether ~/klipper is 'supported' by exact
string comparison of origin against a hardcoded allowlist. A Kalico checkout
matches nothing, hits UNSUPPORTED_REPOSITORY_SOURCE and returns 2, which fails
every RatOS update forever. The fork moves the allowlist to Kalico, lists the
pre-fork Klipper origins as deprecated so already-deployed machines migrate
forward rather than abort, and compares URLs normalized rather than byte-exact.

The klippy changes are Kalico API contract fixes:

  * ratos_hybrid_corexy declares supports_dual_carriage (Kalico reads it
    unguarded and refuses [dual_carriage] without it), accepts axis NAMES in
    set_position, and gains clear_homing_state -- which Kalico calls on every
    M84, from SET_KINEMATIC_POSITION and from safe_z_home.
  * ratos_homing passes homing_axes='z' rather than [2], and prefers
    clear_homing_state.
  * resonance_generator dispatches around Kalico's widened
    ResonanceTestExecutor.run_test signature, which otherwise raises TypeError.

gcode_shell_command.py is no longer registered: Kalico ships and git-tracks its
own, so RatOS' symlink is clobbered by the migration's reset --hard -- silently,
because the verifier only inspects the source path.

sweeping_period is pinned to Klipper's 1.2, because Kalico defaults it to 0.0
and would silently turn every sweep into plain vibration pulses.

Generated by scripts/build-configurator-fork.sh from
$BASE
Klipper pinned to $KALICO_COMMIT ($FORK_KALICO_BRANCH)"

CONFIGURATOR_COMMIT="$(git -C "$CHECKOUT" rev-parse HEAD)"

if [ "$PUSH" -eq 1 ]; then
	say "Pushing to $FORK_CONFIGURATOR_URL"
	git -C "$CHECKOUT" push --force-with-lease "$FORK_CONFIGURATOR_URL" \
		"$FORK_CONFIGURATOR_BRANCH:$FORK_CONFIGURATOR_BRANCH"
	note "pushed $FORK_CONFIGURATOR_BRANCH"
	warn "This is the SOURCE branch. Moonraker pulls"
	note "'$FORK_CONFIGURATOR_DEPLOYMENT_BRANCH', which needs a built app."
	note "See docs/MAINTENANCE.md before pointing a printer at this."
else
	note "not pushed (pass --push)"
fi

say "Done. Configurator fork commit:"
printf '%s\n' "$CONFIGURATOR_COMMIT"
