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
	git -C "$dir" fetch --quiet fork 2>/dev/null || true
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
