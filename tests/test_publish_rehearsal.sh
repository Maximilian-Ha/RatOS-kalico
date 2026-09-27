#!/usr/bin/env bash
#
# A dress rehearsal of publishing: both build scripts, run for real with
# --push, against local stand-ins for every repository involved -- including
# a second publisher who pushes while a build is running.
#
#   tests/test_publish_rehearsal.sh <kalico-checkout> <configurator-checkout>
#
# The two arguments are the upstream checkouts run-all.sh has just built in
# (.work/kalico, .work/configurator). They are read, never written: the stand-in
# repositories borrow their objects through alternates, so nothing is copied
# and nothing here can reach the network or the real forks.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" &>/dev/null && pwd)"
SRC="$(cd -- "$HERE/.." && pwd)"
KSRC="$(cd -- "${1:?usage: $0 <kalico-checkout> <configurator-checkout>}" && pwd)"
CSRC="$(cd -- "${2:?usage: $0 <kalico-checkout> <configurator-checkout>}" && pwd)"

# shellcheck source=/dev/null
eval "$(source "$SRC/fork.conf" && declare -p PUBLISH_BRANCH FORK_KALICO_BRANCH \
	FORK_KALICO_RECOVERY_BRANCH FORK_CONFIGURATOR_BRANCH UPSTREAM_KALICO_BRANCH \
	UPSTREAM_CONFIGURATOR_BRANCH)"
KB="$FORK_KALICO_BRANCH" RB="$FORK_KALICO_RECOVERY_BRANCH" CB="$FORK_CONFIGURATOR_BRANCH"

# The upstream commits the checkouts were built on: each build is one commit on
# top of its base.
KBASE="$(git -C "$KSRC" rev-parse "refs/heads/$KB~1")"
CBASE="$(git -C "$CSRC" rev-parse "refs/heads/$CB~1")"
REAL_GIT="$(command -v git)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# No GIT_AUTHOR_* here: the Kalico build sets its own identity with -c, and an
# environment identity would override it and change the commit it produces.
export HOME="$TMP/home" GIT_CONFIG_NOSYSTEM=1
mkdir -p "$HOME"
git config --global user.name rehearsal
git config --global user.email rehearsal@example.invalid
git config --global init.defaultBranch main
git config --global advice.detachedHead false

FAILED=0
ok() { printf 'ok: %s\n' "$1"; }
fail() {
	printf '!!! FAILED: %s\n' "$1"
	[ -z "${2:-}" ] || printf '%s\n' "$2" | tail -25 | sed 's/^/    | /'
	FAILED=1
}

# --- stand-ins ----------------------------------------------------------------

# borrow <src-checkout> <dst.git>: an empty bare repository that can see every
# object of <src> without copying one, and advertises them to pushers.
borrow() {
	git init --quiet --bare "$2"
	printf '%s\n' "$1/.git/objects" >"$2/objects/info/alternates"
}
borrow "$KSRC" "$TMP/up-kalico.git"
git -C "$TMP/up-kalico.git" update-ref "refs/heads/$UPSTREAM_KALICO_BRANCH" "$KBASE"
borrow "$CSRC" "$TMP/up-conf.git"
git -C "$TMP/up-conf.git" update-ref "refs/heads/$UPSTREAM_CONFIGURATOR_BRANCH" "$CBASE"
borrow "$KSRC" "$TMP/fork-kalico.git"
borrow "$CSRC" "$TMP/fork-conf.git"

# The definition repository: this working tree, committed, on the publish
# branch, with fork.conf pointed at the stand-ins.
D="$TMP/def"
mkdir -p "$D"
cp -R "$SRC/scripts" "$SRC/fork.conf" "$SRC/kalico" "$SRC/configurator" "$SRC/tests" "$D/"
printf '.work/\n__pycache__/\n' >"$D/.gitignore"
sed -i \
	-e "s|^UPSTREAM_KALICO_URL=.*|UPSTREAM_KALICO_URL=\"$TMP/up-kalico.git\"|" \
	-e "s|^UPSTREAM_CONFIGURATOR_URL=.*|UPSTREAM_CONFIGURATOR_URL=\"$TMP/up-conf.git\"|" \
	-e "s|^FORK_KALICO_URL=.*|FORK_KALICO_URL=\"$TMP/fork-kalico.git\"|" \
	-e "s|^FORK_CONFIGURATOR_URL=.*|FORK_CONFIGURATOR_URL=\"$TMP/fork-conf.git\"|" \
	"$D/fork.conf"
git -C "$D" init --quiet -b "$PUBLISH_BRANCH"
git -C "$D" add -A
git -C "$D" commit --quiet -m "rehearsal definition"
git init --quiet --bare "$TMP/def-origin.git"
git -C "$D" remote add origin "$TMP/def-origin.git"
git -C "$D" push --quiet origin "$PUBLISH_BRANCH"

W="$TMP/work"

# --- helpers ------------------------------------------------------------------

tip() { git -C "$1" rev-parse --quiet --verify "refs/heads/$2" || true; }
definition_of() { # the RatOS-Kalico-Definition trailer of the live configurator build
	git -C "$TMP/fork-conf.git" log -1 --format=%B "refs/heads/$CB" |
		sed -n 's/^RatOS-Kalico-Definition: //p'
}

# publish <label> <allow|refuse> <needle> <script> [args...]
#
# Run a build script with --push. With INTRUDE_ON set, a stand-in for another
# session runs INTRUDE_CMD the first time the build invokes git with that word
# as an argument -- i.e. in the middle of the build.
publish() {
	local label="$1" want="$2" needle="$3" script="$4" out rc=0 shim="$TMP/shim"
	shift 4
	mkdir -p "$shim"
	cat >"$shim/git" <<'EOF'
#!/usr/bin/env bash
if [ -n "${INTRUDE_ON:-}" ] && [ ! -e "$INTRUDE_DONE" ]; then
	case " $* " in
	*" $INTRUDE_ON "*)
		: >"$INTRUDE_DONE"
		bash -c "$INTRUDE_CMD" >&2
		;;
	esac
fi
exec "$REAL_GIT" "$@"
EOF
	chmod +x "$shim/git"
	rm -f "$TMP/intruded"
	out="$(PATH="$shim:$PATH" REAL_GIT="$REAL_GIT" INTRUDE_DONE="$TMP/intruded" \
		INTRUDE_ON="${INTRUDE_ON:-}" INTRUDE_CMD="${INTRUDE_CMD:-}" \
		WORK_DIR="$W" "$D/scripts/$script" --push "$@" 2>&1)" || rc=$?
	if [ -n "${INTRUDE_ON:-}" ] && [ ! -e "$TMP/intruded" ]; then
		fail "$label (the intruder never ran -- the test is not testing anything)" "$out"
		return 0
	fi
	if { [ "$want" = allow ] && [ "$rc" -eq 0 ]; } ||
		{ [ "$want" = refuse ] && [ "$rc" -ne 0 ]; }; then
		if [ -z "$needle" ] || grep -F -- "$needle" <<<"$out" >/dev/null; then
			ok "$label"
			return 0
		fi
	fi
	fail "$label (expected $want${needle:+ saying '$needle'}, rc=$rc)" "$out"
}

commit_and_push() { # commit_and_push <message>
	git -C "$D" commit --quiet -am "$1"
	git -C "$D" push --quiet origin "$PUBLISH_BRANCH"
}

# --- 1. the ordinary path -----------------------------------------------------

printf '\n--- publishing from develop ---\n'

publish "Kalico, first publish" allow "nothing is published on $CB yet" build-kalico-fork.sh
K1="$(cat "$W/kalico-commit.txt")"
[ "$(tip "$TMP/fork-kalico.git" "$KB")" = "$K1" ] && [ "$(tip "$TMP/fork-kalico.git" "$RB")" = "$K1" ] &&
	ok "both Kalico branches point at the build" ||
	fail "Kalico branches: $KB=$(tip "$TMP/fork-kalico.git" "$KB") $RB=$(tip "$TMP/fork-kalico.git" "$RB"), built $K1"

publish "configurator, first publish" allow "nothing is published on $CB yet" build-configurator-fork.sh
[ "$(definition_of)" = "$(git -C "$D" rev-parse HEAD)" ] &&
	ok "the published configurator build names HEAD" ||
	fail "the published configurator build names '$(definition_of)'"

publish "Kalico, again from the same HEAD" allow "is contained in HEAD" build-kalico-fork.sh
# The pin logic relies on this: the same inputs give the same commit, so an
# unchanged firmware never looks like an update. (Not across machines, though:
# a machine whose git signs commits puts the signature into the commit.)
[ "$(cat "$W/kalico-commit.txt")" = "$K1" ] &&
	ok "...and rebuilds the very same Kalico commit" ||
	fail "...but rebuilt a different Kalico commit: $(cat "$W/kalico-commit.txt"), first $K1"
publish "configurator, again from the same HEAD" allow "is contained in HEAD" build-configurator-fork.sh
C_GOOD="$(tip "$TMP/fork-conf.git" "$CB")"

# --- 2. somebody else publishes while a build runs ----------------------------

printf '\n--- a second publisher, mid-build ---\n'

# The Kalico branch is overwritten while the Kalico build commits.
INTRUDE_ON=commit INTRUDE_CMD="'$REAL_GIT' -C '$TMP/up-kalico.git' push --quiet --force '$TMP/fork-kalico.git' '$KBASE:refs/heads/$KB'" \
	publish "Kalico push over a Kalico publish that landed mid-build" refuse "stale info" build-kalico-fork.sh
[ "$(tip "$TMP/fork-kalico.git" "$KB")" = "$KBASE" ] && [ "$(tip "$TMP/fork-kalico.git" "$RB")" = "$K1" ] &&
	ok "...the other publish survives, and neither branch moved" ||
	fail "...Kalico branches after the refused push: $KB=$(tip "$TMP/fork-kalico.git" "$KB") $RB=$(tip "$TMP/fork-kalico.git" "$RB")"
git -C "$TMP/fork-kalico.git" update-ref "refs/heads/$KB" "$K1"

# The configurator is published while the Kalico build commits: the Kalico
# push would move the firmware out from under the pin that publish just set.
INTRUDE_ON=commit INTRUDE_CMD="'$REAL_GIT' -C '$TMP/fork-conf.git' update-ref 'refs/heads/$CB' '$CBASE'" \
	publish "Kalico push after a configurator publish that landed mid-build" refuse "during this build" build-kalico-fork.sh
[ "$(tip "$TMP/fork-kalico.git" "$KB")" = "$K1" ] &&
	ok "...nothing was pushed" || fail "...the Kalico branch moved"
git -C "$TMP/fork-conf.git" update-ref "refs/heads/$CB" "$C_GOOD"

# The configurator is published early in the configurator build, before it
# reads the live build for its changelog.
INTRUDE_ON=clean INTRUDE_CMD="'$REAL_GIT' -C '$TMP/fork-conf.git' update-ref 'refs/heads/$CB' '$CBASE'" \
	publish "configurator build after a publish that landed before the changelog" refuse "not the build checked at the start" build-configurator-fork.sh
[ "$(tip "$TMP/fork-conf.git" "$CB")" = "$CBASE" ] &&
	ok "...the other publish survives" || fail "...the other publish was overwritten"
git -C "$TMP/fork-conf.git" update-ref "refs/heads/$CB" "$C_GOOD"

# ...and late, while the configurator build commits.
INTRUDE_ON=commit INTRUDE_CMD="'$REAL_GIT' -C '$TMP/fork-conf.git' update-ref 'refs/heads/$CB' '$CBASE'" \
	publish "configurator push over a publish that landed mid-build" refuse "during this build" build-configurator-fork.sh
[ "$(tip "$TMP/fork-conf.git" "$CB")" = "$CBASE" ] &&
	ok "...the other publish survives" || fail "...the other publish was overwritten"
git -C "$TMP/fork-conf.git" update-ref "refs/heads/$CB" "$C_GOOD"

# HEAD moves during the build: a pull, in another terminal.
INTRUDE_ON=commit INTRUDE_CMD="printf 'x\n' >>'$D/kalico/PROVENANCE.md' && '$REAL_GIT' -C '$D' commit --quiet -am moved && '$REAL_GIT' -C '$D' push --quiet origin '$PUBLISH_BRANCH'" \
	publish "Kalico push after HEAD moved mid-build" refuse "HEAD moved during the build" build-kalico-fork.sh
[ "$(tip "$TMP/fork-kalico.git" "$KB")" = "$K1" ] &&
	ok "...nothing was pushed" || fail "...the Kalico branch moved"

# --- 3. the firmware must come from HEAD's patch ------------------------------

printf '\n--- firmware provenance ---\n'

# HEAD's patch changes after the Kalico build: the configurator must not pin
# the old firmware under the new name.
printf '\n' >>"$D/kalico/0001-ratos-compat-bed_mesh-gcode_macro.patch"
commit_and_push "the firmware patch changes"
publish "configurator pinning firmware built from an older patch" refuse "built from a different" build-configurator-fork.sh
git -C "$D" revert --quiet --no-edit HEAD
git -C "$D" push --quiet origin "$PUBLISH_BRANCH"
publish "...and once the patch matches again, it publishes" allow "was built from HEAD's patch" build-configurator-fork.sh
C_GOOD="$(tip "$TMP/fork-conf.git" "$CB")"

# A Kalico commit that was not built here -- say, published by someone else and
# pinned with --kalico-commit, as the published-tip error suggests.
publish "configurator pinning a Kalico commit not built here" refuse "the last Kalico build here produced" \
	build-configurator-fork.sh --kalico-commit "$KBASE"

# --- 4. somebody published from another branch --------------------------------

printf '\n--- a publish from another branch ---\n'

# What an old copy of the scripts on a feature branch would leave behind: a
# configurator build naming a commit develop has never had.
git -C "$D" checkout --quiet -b feature
printf 'feature work\n' >"$D/kalico/FEATURE.md"
git -C "$D" add kalico/FEATURE.md
git -C "$D" commit --quiet -m "feature work"
FEATURE="$(git -C "$D" rev-parse HEAD)"
git -C "$D" push --quiet origin feature
git -C "$D" checkout --quiet "$PUBLISH_BRANCH"
FOREIGN="$(git -C "$TMP/fork-conf.git" commit-tree "$C_GOOD^{tree}" -p "$C_GOOD" \
	-m "built on a feature branch" -m "RatOS-Kalico-Definition: $FEATURE")"
git -C "$TMP/fork-conf.git" update-ref "refs/heads/$CB" "$FOREIGN"

publish "Kalico from develop over it" refuse "in the history of HEAD" build-kalico-fork.sh
publish "configurator from develop over it" refuse "in the history of HEAD" build-configurator-fork.sh
[ "$(tip "$TMP/fork-conf.git" "$CB")" = "$FOREIGN" ] &&
	ok "...the feature branch's build is still live" || fail "...it was overwritten"

# The documented way out: merge the branch, push, publish. --no-ff, so HEAD is
# a new commit the live build can only name if this publish really happened.
git -C "$D" merge --quiet --no-ff --no-edit feature
git -C "$D" push --quiet origin "$PUBLISH_BRANCH"
publish "after merging that branch into develop, it publishes" allow "is contained in HEAD" build-configurator-fork.sh
[ "$(definition_of)" = "$(git -C "$D" rev-parse HEAD)" ] &&
	ok "...naming the merge" || fail "...naming '$(definition_of)'"

printf '\n'
if [ "$FAILED" -eq 0 ]; then
	printf 'publish rehearsal: all checks passed\n'
	exit 0
fi
printf 'publish rehearsal: FAILURES above\n'
exit 1
