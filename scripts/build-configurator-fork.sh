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
#                                      [--changelog-since <commit-ish>]
#
# --changelog-since overrides where the printer-facing changelog starts. The
# range normally comes from the RatOS-Kalico-Definition trailer of the last
# published build; pass this when that trailer is missing (the first build
# after changelogs were introduced) or wrong, and the bullets are the
# RatOS-kalico commits from there to HEAD instead.
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
CHANGELOG_SINCE=""
EXTRA_ARGS=()

while [ $# -gt 0 ]; do
	case "$1" in
	--push) PUSH=1 ;;
	--kalico-commit)
		shift
		KALICO_COMMIT="${1:-}"
		[ -n "$KALICO_COMMIT" ] || die "--kalico-commit needs a sha"
		;;
	--base)
		shift
		BASE="${1:-}"
		[ -n "$BASE" ] || die "--base needs a commit-ish"
		;;
	--changelog-since)
		shift
		CHANGELOG_SINCE="${1:-}"
		[ -n "$CHANGELOG_SINCE" ] || die "--changelog-since needs a commit-ish"
		;;
	--no-sweeping-period) EXTRA_ARGS+=(--no-sweeping-period) ;;
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

if [ -z "$KALICO_COMMIT" ]; then
	[ -f "$WORK_DIR/kalico-commit.txt" ] ||
		die "no Kalico commit known. Run scripts/build-kalico-fork.sh first, or
    pass --kalico-commit <sha>."
	KALICO_COMMIT="$(cat "$WORK_DIR/kalico-commit.txt")"
fi
say "Pinning klipper to $KALICO_COMMIT"

# The pinned commit MUST already be published, or the printer is pointed at a
# commit that does not exist: klipper-fork-migration.sh's `git cat-file -e`
# fails and it exits 7, while moonraker just reports klipper "up to date"
# forever. Since the fork branch is rebuilt as a single commit each time, the
# invariant is simply that we pin its current tip.
say "Checking $KALICO_COMMIT is published on $FORK_KALICO_BRANCH"
REMOTE_TIP="$(git ls-remote "$FORK_KALICO_URL" "refs/heads/$FORK_KALICO_BRANCH" | cut -f1)"
if [ -z "$REMOTE_TIP" ]; then
	die "$FORK_KALICO_URL has no branch $FORK_KALICO_BRANCH.
    Run scripts/build-kalico-fork.sh --push first."
fi
if [ "$REMOTE_TIP" != "$KALICO_COMMIT" ]; then
	die "would pin $KALICO_COMMIT, but $FORK_KALICO_BRANCH is at $REMOTE_TIP.
    The printer would be pointed at a commit that is not published: the
    migration exits 7 and moonraker reports klipper 'up to date' forever.
    Run scripts/build-kalico-fork.sh --push to publish it, or pass
    --kalico-commit $REMOTE_TIP to pin what is already there."
fi
note "published tip matches"

CHECKOUT="$WORK_DIR/configurator"
ensure_checkout "$CHECKOUT" "$UPSTREAM_CONFIGURATOR_URL" "$UPSTREAM_CONFIGURATOR_BRANCH"
warn_if_upstream_moved "$CHECKOUT" "$VERIFIED_CONFIGURATOR_COMMIT" "RatOS-configurator"

if [ -z "$BASE" ]; then
	BASE="$(git -C "$CHECKOUT" rev-parse FETCH_HEAD)"
fi
say "Building $FORK_CONFIGURATOR_BRANCH on top of $BASE"
git -C "$CHECKOUT" checkout --quiet -B "$FORK_CONFIGURATOR_BRANCH" "$BASE"
# Actually remove strays, rather than asserting they are absent. `checkout -B`
# resets the index but leaves untracked files on disk, and the staging step
# below is `add -A` -- so anything left under these directories by an earlier
# run gets published. That is how three __pycache__/*.pyc reached the branch
# that ships to printers. Scoped to the two directories the fork writes, so a
# pnpm node_modules under src/ is never touched.
git -C "$CHECKOUT" clean --quiet -ffdx -- configuration .github
# src/ cannot be cleaned wholesale -- a local pnpm run leaves node_modules
# there -- but running python over a patched script under src/scripts
# leaves __pycache__ behind, which `add -u` does not stage and the commit
# guard then rejects. Clean exactly that, by pathspec.
git -C "$CHECKOUT" clean --quiet -ffdx -- 'src/**/__pycache__'

say "Applying the Kalico delta"
python3 "$REPO_ROOT/configurator/patch_configurator.py" \
	--checkout "$CHECKOUT" \
	--kalico-upstream-url "$UPSTREAM_KALICO_URL" \
	--kalico-url "$FORK_KALICO_URL" \
	--kalico-branch "$FORK_KALICO_BRANCH" \
	--kalico-commit "$KALICO_COMMIT" \
	--configurator-url "$FORK_CONFIGURATOR_URL" \
	--source-branch "$FORK_CONFIGURATOR_BRANCH" \
	--deployment-branch "$FORK_CONFIGURATOR_DEPLOYMENT_BRANCH" \
	"${EXTRA_ARGS[@]}"

say "Verifying the result"
bash -n "$CHECKOUT/configuration/scripts/klipper-fork-migration.sh" ||
	die "klipper-fork-migration.sh is not valid bash after patching"
bash -n "$CHECKOUT/configuration/scripts/ratos-common.sh" ||
	die "ratos-common.sh is not valid bash after patching"
PYTHONPYCACHEPREFIX="$WORK_DIR/pycache" python3 -m py_compile \
	"$CHECKOUT/configuration/klippy/kinematics/ratos_hybrid_corexy.py" \
	"$CHECKOUT/configuration/klippy/ratos_homing.py" \
	"$CHECKOUT/configuration/klippy/resonance_generator.py" \
	"$CHECKOUT/configuration/klippy/beacon_adaptive_heat_soak.py" ||
	die "patched klippy modules do not compile"
# py_compile cannot see a name that is read but never bound -- exactly the
# shape of bug a re-shaped assignment leaves behind, and on a printer Klippy
# escalates a NameError to an emergency shutdown.
python3 "$REPO_ROOT/tests/check_undefined_names.py" \
	"$CHECKOUT/configuration/klippy/kinematics/ratos_hybrid_corexy.py" \
	"$CHECKOUT/configuration/klippy/ratos_homing.py" \
	"$CHECKOUT/configuration/klippy/resonance_generator.py" \
	"$CHECKOUT/configuration/klippy/beacon_adaptive_heat_soak.py" ||
	die "a patched klippy module reads a name nothing binds"
# The heat soak blocks the G-code queue for up to 90 minutes; the report is the
# only thing that says whether it is converging. Its failure mode is silence,
# so check it structurally rather than trusting that the file compiles.
python3 "$REPO_ROOT/tests/test_heat_soak_report.py" \
	"$CHECKOUT/configuration/klippy/beacon_adaptive_heat_soak.py" ||
	die "the patched heat soak does not report progress to the console"

# The migration script re-reads this value with an awk parser that demands
# exactly 40 hex characters and, thanks to an ERR-trap interaction, reports a
# generic SCRIPT_ERROR rather than the real cause when it fails. Check it here.
PINNED="$(awk '/^\[update_manager klipper\]/{f=1} f && /^pinned_commit:/{gsub(/^pinned_commit:[ \t]*/,"");gsub(/[ \t\r]*$/,"");print;exit}' \
	"$CHECKOUT/configuration/moonraker.conf")"
[ "$PINNED" = "$KALICO_COMMIT" ] ||
	die "moonraker.conf pins '$PINNED' but we built '$KALICO_COMMIT'"
note "moonraker.conf pins the built Kalico commit, and awk-parses cleanly"

[ -f "$CHECKOUT/.github/workflows/publish-kalico.yml" ] ||
	die "the fork's publish workflow was not installed"
if compgen -G "$CHECKOUT/.github/workflows/publish[-.]*" >/dev/null &&
	[ "$(basename "$(compgen -G "$CHECKOUT/.github/workflows/publish[-.]*" | head -1)")" != "publish-kalico.yml" ]; then
	die "an upstream publish workflow survived -- it would push to RatOS' branch names"
fi
note "publish workflow installed, upstream's removed"

# The configurator derives the axis limits from bedMargin in the printer
# definition, while position_max/endstop live in the size .cfg. If those two
# ever disagree the printer homes into its own frame, so cross-check them.
P600="$CHECKOUT/configuration/printers/v-core-4-1-idex-600"
if [ -d "$P600" ]; then
	python3 "$REPO_ROOT/tests/verify_size_cfg.py" \
		"$P600/printer-definition.json" --size 600 \
		--cfg "$CHECKOUT/configuration/printers/v-core-4-1-idex/600.cfg" ||
		die "the 600 size .cfg and its printer definition disagree"
	[ -f "$P600/v-core-4-idex.png" ] ||
		die "the 600 printer type has no image"

	# 600.cfg pulls the service macros in by a relative path, which is how they
	# reach the printer without an edit to printer.cfg. That path only resolves
	# in the SHIPPED layout -- the two files sit in one directory in this repo
	# and in two directories on the printer -- so it can only be checked here.
	SIZE_CFG="$CHECKOUT/configuration/printers/v-core-4-1-idex/600.cfg"
	while read -r spec; do
		[ -n "$spec" ] || continue
		target="$(dirname "$SIZE_CFG")/$spec"
		[ -f "$target" ] ||
			die "600.cfg includes '$spec', which does not exist in the shipped
    tree ($target). Klipper would refuse to start."
		note "600.cfg's include of '$spec' resolves"
	done < <(sed -n 's/^\[include \(.*\)\]$/\1/p' "$SIZE_CFG")
fi

# --- the changelog a printer owner actually sees ---------------------------
#
# Mainsail's update dialog lists the DEPLOYMENT branch's commits and shows each
# subject, with the body behind the "..." expander. That message is written by
# publish-kalico.yml, which lifts it out of the "Printer changelog:" section of
# THIS commit -- so this block is the only place a printer's changelog can come
# from. Keep the marker line in step with the workflow template;
# tests/test_changelog_message.py fails if the two drift apart.
#
# The bullets are the RatOS-kalico commits since the previously published
# build, which is identified by the RatOS-Kalico-Definition trailer this script
# wrote into that build's message. If that commit is unknown -- a first build,
# or a checkout without it -- say so rather than printing a changelog that
# might be wrong. A changelog nobody can trust is worse than none.
ensure_fork_remote "$CHECKOUT" "$FORK_CONFIGURATOR_URL"
DEFINITION_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
PREV_MESSAGE="$(git -C "$CHECKOUT" log -1 --format=%B \
	"fork/$FORK_CONFIGURATOR_BRANCH" 2>/dev/null || true)"
PREV_DEFINITION="$(printf '%s\n' "$PREV_MESSAGE" |
	sed -n 's/^RatOS-Kalico-Definition: \([0-9a-f]\{40\}\)$/\1/p' | head -1)"
PREV_KALICO="$(printf '%s\n' "$PREV_MESSAGE" |
	sed -n 's/^Klipper pinned to \([0-9a-f]\{40\}\).*$/\1/p' | head -1)"

if [ -n "$CHANGELOG_SINCE" ]; then
	PREV_DEFINITION="$(git -C "$REPO_ROOT" rev-parse --verify "${CHANGELOG_SINCE}^{commit}" 2>/dev/null)" ||
		die "--changelog-since '$CHANGELOG_SINCE' is not a commit in this repository"
	note "changelog starts at $CHANGELOG_SINCE (${PREV_DEFINITION:0:12}), overriding the trailer"
fi

# Every bullet stays on ONE line. The subject is derived from the first one,
# and a wrapped bullet would truncate it mid-sentence -- which is exactly the
# unreadable line this whole section exists to replace.
SUBJECT=""
if [ -z "$PREV_DEFINITION" ]; then
	SUBJECT="RatOS-Kalico configurator update (no changelog recorded)"
	BULLETS="- the previously published build predates changelogs, so what changed since it is not recorded"
elif ! git -C "$REPO_ROOT" cat-file -e "${PREV_DEFINITION}^{commit}" 2>/dev/null; then
	SUBJECT="RatOS-Kalico configurator update (no changelog recorded)"
	BULLETS="- the previous build came from RatOS-kalico ${PREV_DEFINITION:0:12}, which this checkout does not contain"
else
	BULLETS="$(git -C "$REPO_ROOT" log --no-merges --max-count=25 \
		--format='- %s' "$PREV_DEFINITION..HEAD" || true)"
	if [ -z "$BULLETS" ]; then
		SUBJECT="Rebuilt against current upstream (fork definition unchanged)"
		BULLETS="- no changes to the fork definition since the last published build"
	fi
fi

# A Klipper pin change means the NEXT update also moves the firmware, which is
# the one thing on this list worth reading before pressing update.
if [ -z "$PREV_KALICO" ]; then
	KLIPPER_LINE="Klipper firmware: ${KALICO_COMMIT:0:12}"
elif [ "$PREV_KALICO" = "$KALICO_COMMIT" ]; then
	KLIPPER_LINE="Klipper firmware: unchanged (${KALICO_COMMIT:0:12})"
else
	KLIPPER_LINE="Klipper firmware: MOVED ${PREV_KALICO:0:12} -> ${KALICO_COMMIT:0:12};
the klipper entry will offer an update too"
fi

# The first line becomes the deployment commit's subject -- the one line
# Mainsail shows without expanding anything -- so it has to carry the most
# useful thing on its own.
if [ -z "$SUBJECT" ]; then
	CHANGE_COUNT="$(printf '%s\n' "$BULLETS" | grep -c '^- ' || true)"
	FIRST_CHANGE="$(printf '%s\n' "$BULLETS" | sed -n '1s/^- //p')"
	if [ "$CHANGE_COUNT" -gt 1 ]; then
		SUBJECT="$FIRST_CHANGE (+$((CHANGE_COUNT - 1)) more)"
	else
		SUBJECT="$FIRST_CHANGE"
	fi
fi

PRINTER_CHANGELOG="$SUBJECT

$BULLETS

$KLIPPER_LINE
Built from RatOS-kalico ${DEFINITION_SHA:0:12}, RatOS ${BASE:0:12}"

say "Committing"
# Stage everything the patcher touched. An explicit path list is how the numpy
# pin silently failed to ship once already: the transform wrote the file, the
# list did not name it, and the commit went out without it while the working
# tree looked correct. Safe to use -A here only because these two directories
# were cleaned above -- without that, -A publishes strays.
git -C "$CHECKOUT" add -A -- configuration .github
# src/ is NOT cleaned above and must not be `add -A`ed: a local pnpm run
# leaves node_modules there. `add -u` stages modifications to tracked
# files only, so a transform under src/ ships while an untracked tree
# cannot. Anything this still misses trips the porcelain guard below
# rather than being published silently.
git -C "$CHECKOUT" add -u -- src

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
Klipper pinned to $KALICO_COMMIT ($FORK_KALICO_BRANCH)

RatOS-Kalico-Definition: $DEFINITION_SHA

Printer changelog:
$PRINTER_CHANGELOG"

# Nothing may be left behind. If the patcher wrote a file the commit did not
# take, the working tree still looks right while the published branch is wrong
# -- which is exactly how the numpy pin went missing.
LEFTOVER="$(git -C "$CHECKOUT" status --porcelain)"
if [ -n "$LEFTOVER" ]; then
	printf '%s\n' "$LEFTOVER" >&2
	die "files changed by the build were not committed (listed above).
    The published branch would not match what was built."
fi

# The changelog is assembled here and consumed by publish-kalico.yml, which
# cannot fail loudly: a broken marker just quietly restores "Deploy <sha>" as
# the only thing a printer owner sees. Check it while both halves are in reach.
python3 "$REPO_ROOT/tests/test_changelog_message.py" "$CHECKOUT" \
	"$REPO_ROOT/configurator/publish-kalico.yml.in" ||
	die "the printer-facing changelog is not usable"

CONFIGURATOR_COMMIT="$(git -C "$CHECKOUT" rev-parse HEAD)"

if [ "$PUSH" -eq 1 ]; then
	say "Pushing to $FORK_CONFIGURATOR_URL"
	# Named remote, so --force-with-lease has a remote-tracking ref to derive
	# its lease from; against a bare URL the lease is silently a no-op.
	ensure_fork_remote "$CHECKOUT" "$FORK_CONFIGURATOR_URL"
	git -C "$CHECKOUT" push --force-with-lease fork \
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
