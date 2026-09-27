#!/usr/bin/env bash
#
# Shared helpers for the RatOS-Kalico build and install scripts.
# Source this; do not execute it.

# shellcheck disable=SC2034

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")/.." &>/dev/null && pwd)"

# shellcheck source=/dev/null
source "$REPO_ROOT/fork.conf"

WORK_DIR="${WORK_DIR:-$REPO_ROOT/.work}"

# The firmware delta, relative to REPO_ROOT. build-kalico-fork.sh applies it;
# build-configurator-fork.sh checks the Kalico commit it pins came from it.
KALICO_PATCH_REL="kalico/0001-ratos-compat-bed_mesh-gcode_macro.patch"

# Not created on source: preflight.sh runs on the printer and promises to
# change nothing. The build scripts create it themselves.
ensure_work_dir() { mkdir -p "$WORK_DIR"; }

say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

need() {
	command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not on PATH"
}

# ensure_checkout <dir> <url> <branch>
#
# Produce a clean checkout of <url>@<branch> at <dir>, reusing and updating it
# if it already exists. Deliberately refuses to touch a dirty tree: silently
# discarding someone's work in progress is never the helpful choice.
ensure_checkout() {
	local dir="$1" url="$2" branch="$3"

	if [ -d "$dir/.git" ]; then
		local origin
		origin="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
		if [ "$origin" != "$url" ]; then
			die "$dir already exists but its origin is '$origin', expected '$url'.
    Remove it or point WORK_DIR somewhere else."
		fi
		if ! git -C "$dir" diff --quiet || ! git -C "$dir" diff --cached --quiet; then
			die "$dir has uncommitted changes. Refusing to overwrite them."
		fi
		say "Updating $dir"
		git -C "$dir" fetch --quiet origin "$branch"
	else
		say "Cloning $url ($branch) into $dir"
		mkdir -p "$(dirname "$dir")"
		git clone --quiet --branch "$branch" "$url" "$dir"
		git -C "$dir" fetch --quiet origin "$branch"
	fi
}

# warn_if_upstream_moved <dir> <verified-sha> <label>
#
# The patches were written against one exact upstream commit. Upstream moving
# does not necessarily break them -- the transforms assert their own anchors --
# but it is the single best predictor of a patch that needs re-review, so say
# so before, not after, someone flashes a printer.
warn_if_upstream_moved() {
	local dir="$1" verified="$2" label="$3"
	local tip
	tip="$(git -C "$dir" rev-parse FETCH_HEAD)"
	if [ "$tip" != "$verified" ]; then
		warn "$label upstream has moved since these patches were verified."
		note "verified against: $verified"
		note "current tip:      $tip"
		note "The transforms assert their own anchors and will abort rather than"
		note "half-apply, but re-read docs/MAINTENANCE.md before shipping this."
	fi
}

# ensure_fork_remote <dir> <url>
#
# Register the fork as a real named remote and fetch it.
#
# This is not cosmetic. `git push --force-with-lease` with no explicit expected
# value derives its lease from a remote-tracking ref. Pushing to a bare URL
# gives it nothing to derive from, so git silently treats the lease as
# satisfied -- which is the opposite of what --force-with-lease is for, and
# would let a build clobber a branch another machine had moved.
ensure_fork_remote() {
	local dir="$1" url="$2"
	if git -C "$dir" remote get-url fork >/dev/null 2>&1; then
		git -C "$dir" remote set-url fork "$url"
	else
		git -C "$dir" remote add fork "$url"
	fi
	# A brand new fork repository has no refs yet; that is not an error.
	# --prune: a branch deleted on the fork must not linger here as if live.
	git -C "$dir" fetch --quiet --prune fork 2>/dev/null || true
}

# confirm <prompt>
#
# Returns 0 only on an explicit "yes". Honours ASSUME_YES for unattended runs.
confirm() {
	if [ "${ASSUME_YES:-0}" = "1" ]; then
		return 0
	fi
	local reply
	printf '%s [yes/NO] ' "$1"
	read -r reply
	[ "$reply" = "yes" ]
}

# --- publishing -------------------------------------------------------------
#
# A publish force-pushes branches that printers update from, rebuilt from
# pristine upstream plus whatever this checkout contains. Whoever publishes
# last therefore wins outright, and two branches that both publish take turns
# deleting each other's work. Nothing notices until a printer loads a config
# that includes a file which no longer exists -- which is exactly how
# power-sensors.cfg once vanished from a running printer. These helpers make
# that refuse instead. They run on --push only; building never is restricted.

# require_publish_allowed
#
# Only $PUBLISH_BRANCH may publish, only from a clean tree, and only from the
# very commit that is pushed to origin. The last condition matters as much as
# the first: the build names HEAD in what it publishes, and a commit only this
# machine has is one nobody else can merge, diff or roll back to.
require_publish_allowed() {
	local ref listing remote_tip head

	# The full ref, not --short: a tag or remote branch of the same name makes
	# --short answer "heads/<name>" and would refuse the right branch.
	ref="$(git -C "$REPO_ROOT" symbolic-ref --quiet HEAD 2>/dev/null || true)"
	if [ "$ref" != "refs/heads/$PUBLISH_BRANCH" ]; then
		die "only '$PUBLISH_BRANCH' may publish, and this checkout is on
    $(if [ -n "$ref" ]; then printf "branch '%s'" "${ref#refs/heads/}"; else printf 'a detached HEAD'; fi).
    Everything that reaches a printer goes through that one branch: merge this
    work into it, push, and publish from there. Building without --push works
    from any branch. See docs/MAINTENANCE.md, 'Who may publish'."
	fi

	# --untracked-files=all overrides status.showUntrackedFiles=no, which would
	# otherwise hide exactly the file a build reads and HEAD does not have.
	if [ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=all)" ]; then
		die "this checkout has uncommitted or untracked changes. A publish is built
    from the working tree but labelled with HEAD, so it has to be clean:
$(git -C "$REPO_ROOT" status --short --untracked-files=all | sed 's/^/        /')"
	fi

	listing="$(git -C "$REPO_ROOT" ls-remote origin "refs/heads/$PUBLISH_BRANCH")" ||
		die "cannot reach origin to check that '$PUBLISH_BRANCH' is pushed"
	remote_tip="${listing%%[[:space:]]*}"
	head="$(git -C "$REPO_ROOT" rev-parse HEAD)"
	if [ -z "$remote_tip" ]; then
		die "origin has no branch '$PUBLISH_BRANCH'. Push it before publishing."
	fi
	if [ "$head" != "$remote_tip" ]; then
		die "HEAD is ${head:0:12}, but origin/$PUBLISH_BRANCH is ${remote_tip:0:12}.
    Publish exactly what is pushed: 'git pull' if origin is ahead, 'git push'
    if this checkout is, then run this again."
	fi
	note "publishing from $PUBLISH_BRANCH at ${head:0:12}, which is what origin has"
}

# definition_trailer
#
# Read a configurator build's commit message on stdin and print the RatOS-kalico
# commit its RatOS-Kalico-Definition trailer names, or nothing. Reads all of its
# input on purpose: quitting early would SIGPIPE the writer, and under pipefail
# that fails the caller.
definition_trailer() {
	local found
	found="$(sed -n 's/^RatOS-Kalico-Definition: \([0-9a-f]\{40\}\)$/\1/p')"
	printf '%s' "${found%%$'\n'*}"
}

# read_live_configurator_build
#
# Set LIVE_TIP and LIVE_DEFINITION from the configurator fork's published source
# branch, for scripts that have no checkout of it. Fetches that one commit and
# nothing else -- no trees, no files -- because only its message is needed.
# LIVE_TIP is empty when the branch does not exist yet.
read_live_configurator_build() {
	local listing tmp
	LIVE_TIP=""
	LIVE_DEFINITION=""
	listing="$(git ls-remote "$FORK_CONFIGURATOR_URL" "refs/heads/$FORK_CONFIGURATOR_BRANCH")" ||
		die "cannot reach $FORK_CONFIGURATOR_URL to check what is published"
	[ -n "$listing" ] || return 0

	tmp="$(mktemp -d)"
	if ! git clone --quiet --bare --depth=1 --filter=tree:0 --single-branch \
		--branch "$FORK_CONFIGURATOR_BRANCH" "$FORK_CONFIGURATOR_URL" "$tmp/live" 2>/dev/null; then
		rm -rf "$tmp"
		die "cannot fetch the published $FORK_CONFIGURATOR_BRANCH to check what it was built from"
	fi
	LIVE_TIP="$(git -C "$tmp/live" rev-parse HEAD)"
	LIVE_DEFINITION="$(git -C "$tmp/live" log -1 --format=%B HEAD | definition_trailer)"
	rm -rf "$tmp"
}

# require_descends_from_live <live-tip> <live-definition> <where>
#
# Refuse to publish over a build this checkout does not contain. <live-tip> is
# the commit on the published branch <where>; empty means nothing is published
# there yet, so nothing can be lost. <live-definition> is the RatOS-kalico
# commit that build names in its trailer. Publishing is safe only if that commit
# is HEAD or one of its ancestors: then everything live goes out again.
#
# ALLOW_ROLLBACK=1 (--allow-rollback) skips the check, loudly, for the rare case
# where discarding what is live is the point. Merging is almost always the
# better answer.
require_descends_from_live() {
	local live_tip="$1" live="$2" where="$3" head holders shallow_hint=""
	head="$(git -C "$REPO_ROOT" rev-parse HEAD)"

	if [ -z "$live_tip" ]; then
		note "nothing is published on $where yet, so nothing can be overwritten"
		return 0
	fi
	if [ "${ALLOW_ROLLBACK:-0}" = "1" ]; then
		warn "--allow-rollback: publishing over $where without checking what that discards."
		note "The live build came from RatOS-kalico ${live:-<unrecorded>}."
		return 0
	fi
	if [ -z "$live" ]; then
		die "the build live on $where (${live_tip:0:12}) names no RatOS-kalico commit
    -- it has no RatOS-Kalico-Definition trailer -- so there is no telling what
    publishing over it would discard. This repository's build script did not
    make it. Find out who did; to overwrite it anyway, pass --allow-rollback."
	fi
	if git -C "$REPO_ROOT" merge-base --is-ancestor "$live" "$head" 2>/dev/null; then
		note "the live build (RatOS-kalico ${live:0:12}) is contained in HEAD"
		return 0
	fi

	holders=""
	if git -C "$REPO_ROOT" cat-file -e "${live}^{commit}" 2>/dev/null; then
		holders="$(git -C "$REPO_ROOT" branch --all --format='%(refname:short)' \
			--contains "$live" | sed 's/^/        /')"
	fi
	if [ "$(git -C "$REPO_ROOT" rev-parse --is-shallow-repository)" = "true" ]; then
		shallow_hint="
    This clone is shallow, which can hide the connection: run
    'git fetch --unshallow origin' and try again."
	fi
	die "the build live on $where came from RatOS-kalico ${live:0:12}, which is not
    in the history of HEAD (${head:0:12}). Publishing now would delete whatever
    that commit has and HEAD lacks. Someone published from another branch.${holders:+
    Branches here that contain it:
$holders}
    To find it:  git fetch origin && git branch -r --contains $live
    Merge that into $PUBLISH_BRANCH, push, and publish again. Only if throwing
    away what is live is really what you want: --allow-rollback.$shallow_hint"
}

# remote_branch_tip <url> <branch>
#
# Print the commit <branch> is at on <url>, or nothing if it does not exist.
# An unreachable remote is an error, never "does not exist".
remote_branch_tip() {
	local listing
	listing="$(git ls-remote "$1" "refs/heads/$2")" ||
		die "cannot reach $1 to read $2"
	printf '%s' "${listing%%[[:space:]]*}"
}

# require_kalico_built_from_head <kalico-commit>
#
# A configurator publish pins <kalico-commit> and is labelled with HEAD, so that
# commit has to be what HEAD's firmware patch produces -- not a build from before
# the last pull, and not one somebody else published. build-kalico-fork.sh
# records which patch each build applied; compare that with the patch in HEAD.
require_kalico_built_from_head() {
	local want="$1" record="$WORK_DIR/kalico-built-from.txt" built="" patch="" head_patch
	if [ ! -f "$record" ]; then
		die "there is no record of which patch Kalico $want was built from.
    Run scripts/build-kalico-fork.sh first (with --push if what it builds is
    not published yet); it records that, and --kalico-commit cannot."
	fi
	read -r built patch <"$record" || true
	if [ "$built" != "$want" ]; then
		die "this would pin Kalico ${want:0:12}, but the last Kalico build here produced
    ${built:0:12}. Only a commit built here from HEAD's patch can be pinned under
    HEAD's name. Run scripts/build-kalico-fork.sh (--push if needed) first."
	fi
	head_patch="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "HEAD:$KALICO_PATCH_REL")" ||
		die "HEAD has no $KALICO_PATCH_REL"
	if [ "$patch" != "$head_patch" ]; then
		die "Kalico ${want:0:12} was built from a different $KALICO_PATCH_REL than the
    one in HEAD, so the firmware would go out under a label it does not match.
    Run scripts/build-kalico-fork.sh --push from here first."
	fi
	note "Kalico ${want:0:12} was built from HEAD's patch"
}

# recheck_before_push <head-at-start> <live-tip-at-start>
#
# A build takes minutes and everything it checked at the start can change in
# that time: a pull moves HEAD, someone else publishes. Check again, last thing
# before the push. The pushes are additionally leased on what was checked; this
# is what turns the rarer cases into a clear refusal instead of a raw "stale
# info" from git, and what covers the configurator build for the Kalico push,
# which no lease of its own can.
recheck_before_push() {
	local start_head="$1" start_live="$2"
	require_publish_allowed
	[ "$(git -C "$REPO_ROOT" rev-parse HEAD)" = "$start_head" ] ||
		die "HEAD moved during the build (it started at ${start_head:0:12}). What was
    built is not what HEAD is; run it again."
	read_live_configurator_build
	[ "$LIVE_TIP" = "$start_live" ] ||
		die "someone published $FORK_CONFIGURATOR_BRANCH during this build (it was at
    '${start_live:0:12}', it is now at '${LIVE_TIP:0:12}'). Nothing was pushed.
    Run it again; the check at the start will say whether that is safe."
}
