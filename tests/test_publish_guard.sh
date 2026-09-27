#!/usr/bin/env bash
#
# The publish guard, against throwaway repositories: only $PUBLISH_BRANCH may
# publish, only clean and exactly as pushed, and never over a live build whose
# RatOS-kalico commit HEAD does not contain.
#
#   tests/test_publish_guard.sh
#
# Needs git only. No network: every "remote" here is a local repository, and
# the build scripts are exercised from a copy whose fork.conf points at them.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" &>/dev/null && pwd)"
SRC="$(cd -- "$HERE/.." && pwd)"
LIB="$SRC/scripts/lib.sh"
# shellcheck source=/dev/null
REAL_PUBLISH_BRANCH="$(source "$SRC/fork.conf" && printf '%s' "$PUBLISH_BRANCH")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export GIT_CONFIG_NOSYSTEM=1 HOME="$TMP/home"
mkdir -p "$HOME"
git config --global init.defaultBranch main
git config --global advice.detachedHead false

FAILED=0
ok() { printf 'ok: %s\n' "$1"; }
fail() {
	printf '!!! FAILED: %s\n' "$1"
	[ -z "${2:-}" ] || printf '%s\n' "$2" | sed 's/^/    | /'
	FAILED=1
}

# guard <repo> <function> [args...]
#
# Call one lib.sh function as the build scripts do: in a fresh bash, with
# lib.sh sourced (so set -euo pipefail is really in force) and REPO_ROOT
# pointed at a fixture. PUBLISH_BRANCH is "develop" for the fixtures.
guard() {
	local repo="$1"
	shift
	bash -c 'source "$1"; REPO_ROOT="$2"; PUBLISH_BRANCH=develop
		FORK_CONFIGURATOR_URL="${TEST_CONF_URL:-$FORK_CONFIGURATOR_URL}"
		FORK_CONFIGURATOR_BRANCH=v2.1.x-kalico
		shift 2; "$@"' _ "$LIB" "$repo" "$@"
}

# The same, with the configurator fork at <url>, or with --allow-rollback in
# force. Exported only inside expect's or $(...)'s own subshell.
with_conf() {
	export TEST_CONF_URL="$1"
	shift
	guard "$@"
}
with_rollback() {
	export ALLOW_ROLLBACK=1
	guard "$@"
}
with_workdir() {
	export WORK_DIR="$1"
	shift
	guard "$@"
}

# expect allow|refuse <label> <needle> <command...>
#
# A refusal must also say why: the needle has to appear in its output, so a
# refusal for the wrong reason does not pass.
expect() {
	local want="$1" label="$2" needle="$3" out rc=0
	shift 3
	out="$("$@" 2>&1)" || rc=$?
	if [ "$want" = allow ]; then
		if [ "$rc" -eq 0 ] && { [ -z "$needle" ] || grep -F -- "$needle" <<<"$out" >/dev/null; }; then
			ok "$label"
		else
			fail "$label (expected it to be allowed, rc=$rc)" "$out"
		fi
	else
		if [ "$rc" -ne 0 ] && grep -F -- "$needle" <<<"$out" >/dev/null; then
			ok "$label"
		else
			fail "$label (expected a refusal saying '$needle', rc=$rc)" "$out"
		fi
	fi
}

commit() { # commit <repo> <file> <text>
	printf '%s\n' "$3" >"$1/$2"
	git -C "$1" add "$2"
	git -C "$1" commit --quiet -m "$3"
}

# --- fixture: origin with develop A -> B, and a side branch S off A ----------

git init --quiet --bare "$TMP/origin.git"
git clone --quiet "$TMP/origin.git" "$TMP/seed" 2>/dev/null
printf '.work/\n' >"$TMP/seed/.gitignore"
git -C "$TMP/seed" add .gitignore
commit "$TMP/seed" a.txt "A"
A="$(git -C "$TMP/seed" rev-parse HEAD)"
git -C "$TMP/seed" branch --quiet side
commit "$TMP/seed" b.txt "B"
B="$(git -C "$TMP/seed" rev-parse HEAD)"
git -C "$TMP/seed" branch --quiet -m develop
git -C "$TMP/seed" checkout --quiet side
commit "$TMP/seed" s.txt "S"
S="$(git -C "$TMP/seed" rev-parse HEAD)"
git -C "$TMP/seed" push --quiet origin develop side "$A:refs/heads/main"

fresh() { # fresh <name>: a clean clone on develop at B
	git clone --quiet --branch develop "$TMP/origin.git" "$TMP/$1"
	printf '%s' "$TMP/$1"
}

printf '\n--- who may publish ---\n'

R="$(fresh ok)"
expect allow "develop, clean, at origin's tip" "" guard "$R" require_publish_allowed

mkdir -p "$R/.work" && printf 'x\n' >"$R/.work/build-output"
expect allow "ignored build output does not count as dirty" "" guard "$R" require_publish_allowed

R="$(fresh feature)"
git -C "$R" checkout --quiet -b feature
expect refuse "a feature branch" "branch 'feature'" guard "$R" require_publish_allowed

R="$(fresh main)"
git -C "$R" checkout --quiet -B main origin/main
expect refuse "main, which is updated by pull request only" "branch 'main'" guard "$R" require_publish_allowed

R="$(fresh tagged)"
git -C "$R" tag develop
expect allow "develop, with a tag of the same name" "" guard "$R" require_publish_allowed

R="$(fresh hidden-untracked)"
git -C "$R" config status.showUntrackedFiles no
printf 'new\n' >"$R/power-sensors.cfg"
expect refuse "an untracked file, with status.showUntrackedFiles=no" "power-sensors.cfg" \
	guard "$R" require_publish_allowed

R="$(fresh detached)"
git -C "$R" checkout --quiet --detach
expect refuse "a detached HEAD, even at develop's tip" "a detached HEAD" guard "$R" require_publish_allowed

R="$(fresh dirty)"
printf 'edited\n' >>"$R/b.txt"
expect refuse "an edited tracked file" "uncommitted or untracked" guard "$R" require_publish_allowed

R="$(fresh staged)"
printf 'new\n' >"$R/n.txt" && git -C "$R" add n.txt
expect refuse "a staged new file" "uncommitted or untracked" guard "$R" require_publish_allowed

R="$(fresh untracked)"
printf 'new\n' >"$R/stray.cfg"
expect refuse "an untracked file the build could pick up" "stray.cfg" guard "$R" require_publish_allowed

R="$(fresh ahead)"
commit "$R" c.txt "C, not pushed"
expect refuse "a commit origin does not have" "Publish exactly what is pushed" guard "$R" require_publish_allowed

R="$(fresh behind)"
O="$(fresh other)"
commit "$O" d.txt "D, pushed by someone else"
git -C "$O" push --quiet origin develop
expect refuse "origin ahead of this checkout" "Publish exactly what is pushed" guard "$R" require_publish_allowed

git init --quiet --bare "$TMP/empty-origin.git"
R="$(fresh no-remote-branch)"
git -C "$R" remote set-url origin "$TMP/empty-origin.git"
expect refuse "origin without the publish branch" "origin has no branch 'develop'" guard "$R" require_publish_allowed

R="$(fresh unreachable)"
git -C "$R" remote set-url origin "$TMP/does-not-exist.git"
expect refuse "origin unreachable" "cannot reach origin" guard "$R" require_publish_allowed

printf '\n--- never over a live build HEAD lacks ---\n'

R="$(fresh live)"
git -C "$R" fetch --quiet origin side
git -C "$R" branch --quiet side FETCH_HEAD
HEAD_R="$(git -C "$R" rev-parse HEAD)"
TIP=0123456789abcdef0123456789abcdef01234567 # the published branch's own commit
UNKNOWN=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef

expect allow "nothing published yet" "nothing is published" \
	guard "$R" require_descends_from_live "" "" v2.1.x-kalico
expect allow "live build is HEAD itself (a re-publish)" "contained in HEAD" \
	guard "$R" require_descends_from_live "$TIP" "$HEAD_R" v2.1.x-kalico
expect allow "live build is an ancestor of HEAD" "contained in HEAD" \
	guard "$R" require_descends_from_live "$TIP" "$A" v2.1.x-kalico
expect refuse "live build from a branch HEAD never merged" "in the history of HEAD" \
	guard "$R" require_descends_from_live "$TIP" "$S" v2.1.x-kalico
expect refuse "...and the refusal names the branch that has it" "        side" \
	guard "$R" require_descends_from_live "$TIP" "$S" v2.1.x-kalico
expect refuse "live build from a commit this clone has never seen" \
	"git branch -r --contains $UNKNOWN" \
	guard "$R" require_descends_from_live "$TIP" "$UNKNOWN" v2.1.x-kalico
expect refuse "live build without a trailer" "names no RatOS-kalico commit" \
	guard "$R" require_descends_from_live "$TIP" "" v2.1.x-kalico
expect allow "--allow-rollback overrides, and says so" "--allow-rollback: publishing over" \
	with_rollback "$R" require_descends_from_live "$TIP" "$S" v2.1.x-kalico

printf '\n--- reading the live build ---\n'

TRAILER="RatOS-Kalico-Definition: $B"
got="$(printf 'subject\n\nbody\n\n%s\n' "$TRAILER" | guard "$R" definition_trailer)"
[ "$got" = "$B" ] && ok "trailer is read" || fail "trailer is read (got '$got')"
got="$(printf 'subject\n\nRatOS-Kalico-Definition: %s\nRatOS-Kalico-Definition: %s\n' "$A" "$B" |
	guard "$R" definition_trailer)"
[ "$got" = "$A" ] && ok "first of two trailers wins" || fail "first of two trailers wins (got '$got')"
got="$(printf 'subject\n\n  RatOS-Kalico-Definition: %s\n' "$A" | guard "$R" definition_trailer)"
[ -z "$got" ] && ok "a quoted, indented trailer is not a trailer" || fail "indented trailer matched ('$got')"
# 200 kB before the trailer: bigger than a pipe buffer, so a reader that quit
# early would SIGPIPE the writer and fail the pipeline under pipefail.
if got="$(bash -c 'set -o pipefail; source "$1"
		{ head -c 200000 /dev/zero | tr "\0" x; printf "\n%s\n%s\n" "$2" "$2"; } | definition_trailer' \
	_ "$LIB" "$TRAILER")" && [ "$got" = "$B" ]; then
	ok "a huge message does not break the pipeline"
else
	fail "a huge message breaks the pipeline (got '$got')"
fi

git init --quiet --bare "$TMP/conf.git"
C="$(mktemp -d "$TMP/conf.XXXX")"
git -C "$C" init --quiet
git -C "$C" commit --quiet --allow-empty -m "Built from RatOS-kalico

$TRAILER"
LIVE="$(git -C "$C" rev-parse HEAD)"
git -C "$C" push --quiet "$TMP/conf.git" HEAD:refs/heads/v2.1.x-kalico

got="$(with_conf "file://$TMP/conf.git" "$R" eval \
	'read_live_configurator_build; printf "%s %s" "$LIVE_TIP" "$LIVE_DEFINITION"')" || true
[ "$got" = "$LIVE $B" ] && ok "the published build and its trailer are read remotely" ||
	fail "reading the published build (got '$got')"
got="$(with_conf "file://$TMP/empty-origin.git" "$R" eval \
	'read_live_configurator_build; printf "[%s]" "$LIVE_TIP"')" || true
[ "$got" = "[]" ] && ok "no published branch reads as nothing published" ||
	fail "no published branch (got '$got')"
expect refuse "an unreachable fork is not 'nothing published'" "cannot reach" \
	with_conf "file://$TMP/nowhere.git" "$R" read_live_configurator_build

printf '\n--- the firmware pinned must come from HEAD ---\n'

P="$(fresh provenance)"
mkdir -p "$P/kalico"
printf 'the patch\n' >"$P/kalico/0001-ratos-compat-bed_mesh-gcode_macro.patch"
git -C "$P" add kalico && git -C "$P" commit --quiet -m "patch" && git -C "$P" push --quiet origin develop
PATCH_BLOB="$(git -C "$P" rev-parse HEAD:kalico/0001-ratos-compat-bed_mesh-gcode_macro.patch)"
K=1111111111111111111111111111111111111111
mkdir -p "$TMP/wd-none" "$TMP/wd-other" "$TMP/wd-stale" "$TMP/wd-good"
printf '%s %s\n' 2222222222222222222222222222222222222222 "$PATCH_BLOB" >"$TMP/wd-other/kalico-built-from.txt"
printf '%s %s\n' "$K" "$(printf 'an older patch\n' | git hash-object --stdin)" >"$TMP/wd-stale/kalico-built-from.txt"
printf '%s %s\n' "$K" "$PATCH_BLOB" >"$TMP/wd-good/kalico-built-from.txt"
expect refuse "no record of the Kalico build" "no record of which patch" \
	with_workdir "$TMP/wd-none" "$P" require_kalico_built_from_head "$K"
expect refuse "a Kalico commit other than the one built here" "the last Kalico build here produced" \
	with_workdir "$TMP/wd-other" "$P" require_kalico_built_from_head "$K"
expect refuse "a Kalico commit built from an older patch" "built from a different" \
	with_workdir "$TMP/wd-stale" "$P" require_kalico_built_from_head "$K"
expect allow "the Kalico commit built here from HEAD's patch" "built from HEAD's patch" \
	with_workdir "$TMP/wd-good" "$P" require_kalico_built_from_head "$K"

printf '\n--- checked again right before the push ---\n'

HEAD_P="$(git -C "$P" rev-parse HEAD)"
expect allow "nothing changed during the build" "" \
	with_conf "file://$TMP/conf.git" "$P" recheck_before_push "$HEAD_P" "$LIVE"
expect refuse "HEAD moved during the build" "HEAD moved during the build" \
	with_conf "file://$TMP/conf.git" "$P" recheck_before_push "$A" "$LIVE"
expect refuse "someone published during the build" "published v2.1.x-kalico during this build" \
	with_conf "file://$TMP/conf.git" "$P" recheck_before_push "$HEAD_P" "$TIP"
expect refuse "...including a first publish" "published v2.1.x-kalico during this build" \
	with_conf "file://$TMP/conf.git" "$P" recheck_before_push "$HEAD_P" ""

printf '\n--- the lease the configurator push relies on ---\n'

# The push names the tip it checked. Git must refuse it when the branch has
# moved since, and when a branch appeared that the build believed absent.
L="$(fresh lease)"
if ! git -C "$L" push --quiet --force-with-lease="refs/heads/develop:$A" origin "$S:refs/heads/develop" 2>/dev/null; then
	ok "a stale expected tip is refused"
else
	fail "a stale expected tip was accepted"
fi
if ! git -C "$L" push --quiet --force-with-lease="refs/heads/develop:" origin "$S:refs/heads/develop" 2>/dev/null; then
	ok "'must not exist' is refused when the branch exists"
else
	fail "'must not exist' was accepted over an existing branch"
fi
CURRENT="$(git -C "$L" ls-remote origin refs/heads/develop | cut -f1)"
if git -C "$L" push --quiet --force-with-lease="refs/heads/lease-test:" origin "$S:refs/heads/lease-test" 2>/dev/null &&
	git -C "$L" push --quiet --force-with-lease="refs/heads/lease-test:$S" origin "$A:refs/heads/lease-test" 2>/dev/null; then
	ok "the right expected tip, or absence, is accepted"
else
	fail "a correct lease was refused"
fi
[ "$(git -C "$L" ls-remote origin refs/heads/develop | cut -f1)" = "$CURRENT" ] ||
	fail "a refused push changed develop"

printf '\n--- the build scripts refuse before they build ---\n'

git init --quiet --bare "$TMP/fork-kalico-empty.git"

# A copy of this repository as a fixture: its own origin, its fork.conf pointed
# at local repositories, so nothing here can reach -- let alone push to -- the
# real forks.
make_copy() { # make_copy <name> <branch>
	local d="$TMP/copy-$1" o="$TMP/copy-$1.git"
	mkdir -p "$d"
	cp -R "$SRC/scripts" "$SRC/fork.conf" "$SRC/kalico" "$SRC/configurator" "$SRC/tests" "$d/"
	printf '.work/\n__pycache__/\n' >"$d/.gitignore"
	sed -i \
		-e "s|^FORK_CONFIGURATOR_URL=.*|FORK_CONFIGURATOR_URL=\"file://$TMP/conf.git\"|" \
		-e "s|^FORK_KALICO_URL=.*|FORK_KALICO_URL=\"file://$TMP/fork-kalico-empty.git\"|" \
		-e "s|^UPSTREAM_KALICO_URL=.*|UPSTREAM_KALICO_URL=\"file://$TMP/nowhere-upstream.git\"|" \
		-e "s|^UPSTREAM_CONFIGURATOR_URL=.*|UPSTREAM_CONFIGURATOR_URL=\"file://$TMP/nowhere-upstream.git\"|" \
		"$d/fork.conf"
	git -C "$d" init --quiet -b "$2"
	git -C "$d" add -A
	git -C "$d" commit --quiet -m "fixture"
	git init --quiet --bare "$o"
	git -C "$d" remote add origin "$o"
	git -C "$d" push --quiet origin "$2"
	printf '%s' "$d"
}

D="$(make_copy feature feature)"
for invocation in "build-kalico-fork.sh --push" \
	"build-configurator-fork.sh --push --kalico-commit $A"; do
	script="${invocation%% *}"
	# shellcheck disable=SC2086 # the invocation is split on purpose
	expect refuse "$script --push from a feature branch" \
		"only '$REAL_PUBLISH_BRANCH' may publish" \
		env WORK_DIR="$TMP/work-$script" "$D/scripts/"$invocation
	if [ -e "$TMP/work-$script/kalico" ] || [ -e "$TMP/work-$script/configurator" ]; then
		fail "$script started building before it refused"
	fi
done

D="$(make_copy develop "$REAL_PUBLISH_BRANCH")"
printf 'edited\n' >>"$D/kalico/PROVENANCE.md"
expect refuse "build-kalico-fork.sh --push with a dirty tree" "uncommitted or untracked" \
	env WORK_DIR="$TMP/work-dirty" "$D/scripts/build-kalico-fork.sh" --push
git -C "$D" checkout --quiet -- kalico/PROVENANCE.md

# The live configurator build ($LIVE) names $B, which the copy never had.
expect refuse "build-kalico-fork.sh --push over a live build HEAD lacks" \
	"in the history of HEAD" \
	env WORK_DIR="$TMP/work-live" "$D/scripts/build-kalico-fork.sh" --push
[ ! -e "$TMP/work-live/kalico" ] || fail "build-kalico-fork.sh fetched upstream before refusing"

# past_guard <label> <expected-note-or-empty> <args...>
#
# The build must get past the guard and on to cloning upstream, which fails
# here by design (fork.conf points it nowhere). Reaching that clone is the
# proof; failing any earlier, for any reason, is not.
past_guard() {
	local label="$1" note="$2" out
	shift 2
	out="$(WORK_DIR="$TMP/work-past-$RANDOM" "$D/scripts/build-kalico-fork.sh" "$@" 2>&1)" || true
	if grep -F "Cloning file://$TMP/nowhere-upstream.git" <<<"$out" >/dev/null &&
		{ [ -z "$note" ] || grep -F -- "$note" <<<"$out" >/dev/null; }; then
		ok "$label"
	else
		fail "$label" "$out"
	fi
}

past_guard "build-kalico-fork.sh --push --allow-rollback gets past the guard, and says so" \
	"--allow-rollback: publishing over" --push --allow-rollback

# ALLOW_ROLLBACK must come from the flag. An exported variable left over in a
# shell must not silently switch the check off.
expect refuse "an exported ALLOW_ROLLBACK=1 is ignored" "in the history of HEAD" \
	env ALLOW_ROLLBACK=1 WORK_DIR="$TMP/work-env" "$D/scripts/build-kalico-fork.sh" --push

# Without --push nothing is checked: building stays open to every branch.
past_guard "building without --push is not guarded" ""
git -C "$D" checkout --quiet -b some-feature
past_guard "...not even on a feature branch" ""

# The configurator runs the same checks, before it builds anything.
git -C "$D" checkout --quiet "$REAL_PUBLISH_BRANCH"
expect refuse "build-configurator-fork.sh --push over a live build HEAD lacks" \
	"in the history of HEAD" \
	env WORK_DIR="$TMP/work-conf-live" "$D/scripts/build-configurator-fork.sh" --push --kalico-commit "$A"
[ ! -e "$TMP/work-conf-live/configurator" ] || fail "build-configurator-fork.sh fetched upstream before refusing"
expect refuse "...and ignores an exported ALLOW_ROLLBACK=1 as well" "in the history of HEAD" \
	env ALLOW_ROLLBACK=1 WORK_DIR="$TMP/work-conf-env" "$D/scripts/build-configurator-fork.sh" --push --kalico-commit "$A"
expect refuse "...while --allow-rollback gets it to the firmware check" "no record of which patch" \
	env WORK_DIR="$TMP/work-conf-rb" "$D/scripts/build-configurator-fork.sh" --push --allow-rollback --kalico-commit "$A"

printf '\n'
if [ "$FAILED" -eq 0 ]; then
	printf 'publish guard: all checks passed\n'
	exit 0
fi
printf 'publish guard: FAILURES above\n'
exit 1
