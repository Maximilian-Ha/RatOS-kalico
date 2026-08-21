#!/usr/bin/env bash
#
# Read-only readiness check, to be run ON THE PRINTER before switching it to
# RatOS-Kalico. Changes nothing. Exit 1 means do not proceed yet.
#
# Everything it reports is something that has bitten this migration in analysis:
# a dirty klipper tree aborts the migration permanently, a missing pygam breaks
# every boot because Kalico imports all of klippy/extras eagerly, and a
# hand-edited generated config is destroyed by the first regeneration.

SCRIPT_DIR="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" &>/dev/null && pwd)"
# shellcheck source=scripts/lib.sh
source "$SCRIPT_DIR/lib.sh"

FAIL=0
WARNED=0

fail() {
	printf '  [FAIL] %s\n' "$*"
	FAIL=1
}
ok() { printf '  [ ok ] %s\n' "$*"; }
attn() {
	printf '  [note] %s\n' "$*"
	WARNED=1
}

say "RatOS-Kalico preflight"
note "This only reads. Nothing is modified."
echo

# --- the machine is what we think it is -----------------------------------

say "Installation layout"
[ -d "$KLIPPER_DIR/klippy" ] && ok "klipper at $KLIPPER_DIR" || fail "no klippy/ in $KLIPPER_DIR"
[ -d "$CONFIGURATOR_DIR/configuration" ] && ok "configurator at $CONFIGURATOR_DIR" ||
	fail "no configuration/ in $CONFIGURATOR_DIR"
[ -d "$PRINTER_DATA_DIR/config" ] && ok "printer data at $PRINTER_DATA_DIR" ||
	fail "no config/ in $PRINTER_DATA_DIR"

RATOS_LINK="$PRINTER_DATA_DIR/config/RatOS"
if [ -L "$RATOS_LINK" ]; then
	ok "config/RatOS is a symlink -> $(readlink -f "$RATOS_LINK")"
else
	fail "config/RatOS is not a symlink. RatOS 2.1 replaces it with one pointing
         at $CONFIGURATOR_DIR/configuration; a real directory means this is not
         a stock 2.1 install."
fi
echo

# --- git state, because the migration hard-resets ---------------------------

say "Klipper checkout"
if git -C "$KLIPPER_DIR" rev-parse --git-dir >/dev/null 2>&1; then
	ORIGIN="$(git -C "$KLIPPER_DIR" remote get-url origin 2>/dev/null || echo '(none)')"
	ok "origin: $ORIGIN"
	ok "HEAD:   $(git -C "$KLIPPER_DIR" rev-parse HEAD)"
	BR="$(git -C "$KLIPPER_DIR" branch --show-current 2>/dev/null)"
	ok "branch: ${BR:-(detached HEAD)}"

	# klipper-fork-migration.sh aborts with KLIPPER_UNCOMMITTED_CHANGES and
	# never recovers on its own. RatOS' own extension symlinks are excluded via
	# .git/info/exclude, so they do not count -- anything else does.
	if git -C "$KLIPPER_DIR" diff-index --quiet HEAD -- 2>/dev/null; then
		ok "working tree clean"
	else
		fail "working tree has modifications. The migration aborts on this
         (KLIPPER_UNCOMMITTED_CHANGES). Inspect with:
             git -C $KLIPPER_DIR status --short"
	fi

	APP="Klipper"
	if grep -q 'APP_NAME *= *"Kalico"' "$KLIPPER_DIR/klippy/__init__.py" 2>/dev/null; then
		APP="Kalico"
	fi
	ok "firmware flavour: $APP"
	[ "$APP" = "Kalico" ] && attn "already on Kalico -- this would be a re-point, not a migration"
else
	fail "$KLIPPER_DIR is not a git repository"
fi
echo

# --- the python environment Kalico needs ------------------------------------

say "Platform"
# Every version decision below branches on this. RatOS 2.1 images are built on
# Debian 11 / Raspberry Pi OS Bullseye, which means Python 3.9 -- not the 3.11
# it is easy to assume. Read it, do not guess it.
if [ -r /etc/os-release ]; then
	# shellcheck disable=SC1091
	ok "os: $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
else
	attn "no /etc/os-release"
fi
ok "arch: $(uname -m)"
echo

say "Klippy virtualenv"
PY="$HOME/klippy-env/bin/python"
if [ -x "$PY" ]; then
	ok "venv python: $("$PY" -V 2>&1)"

	ver() { "$PY" -c "import $1,sys;sys.stdout.write(getattr($1,'__version__','?'))" 2>/dev/null || true; }
	NPV="$(ver numpy)"
	SPV="$(ver scipy)"
	JV="$(ver jinja2)"
	PGV="$(ver pygam)"

	[ -n "$NPV" ] && ok "numpy  $NPV" || fail "numpy is not importable in the klippy venv"
	[ -n "$SPV" ] && ok "scipy  $SPV" || attn "scipy is not importable (beacon and pygam both want it)"
	[ -n "$JV" ] && ok "jinja2 $JV" || fail "jinja2 is not importable in the klippy venv"
	[ -n "$PGV" ] && ok "pygam  $PGV" || PGV=""

	# THE combination that matters. RatOS pins pygam==0.9.1, whose metadata caps
	# scipy at <1.12, and no scipy below 1.12 supports numpy 2. So numpy 2 and
	# pygam cannot coexist. On Kalico that is a boot blocker rather than a
	# nuisance: [beacon_adaptive_heat_soak] imports pygam at module scope and is
	# declared unconditionally for this printer, so klippy dies at config load.
	case "$NPV" in
	2.*)
		if [ -n "$PGV" ]; then
			fail "numpy $NPV together with pygam $PGV cannot work. pygam caps scipy
         below 1.12 and no such scipy supports numpy 2. Klippy will fail at
         config load on [beacon_adaptive_heat_soak]. The fork pins
         numpy<2 in both requirements files -- see docs/RISKS.md section 1."
		else
			attn "numpy $NPV, and pygam is not installed. If pygam is ever installed
         (ratos-update.sh does it on every configurator merge) this venv
         breaks. The fork pins numpy<2 for exactly this reason."
		fi
		;;
	1.*)
		ok "numpy is 1.x -- the combination the fork targets"
		;;
	esac

	# jinja2 is NOT the risk it was once thought to be: only gcode_macro.py
	# touches the API, and Kalico's own is written for 3.x. But Kalico does
	# require >= 3.1.6, and markupsafe must move with it.
	case "$JV" in
	3.*) ok "jinja2 is 3.x, which Kalico requires" ;;
	"") : ;;
	*) attn "jinja2 $JV -- Kalico requires >= 3.1.6. Upgrade it together with
         markupsafe, never separately: markupsafe 2.x removes soft_unicode and
         would break jinja2 2.11.3 on its own." ;;
	esac

	# Kalico eagerly imports EVERY module in klippy/extras at startup, so a
	# broken pygam is a boot-time problem for every beacon user, not just those
	# using adaptive heat soak.
	if "$PY" -c "import pygam" >/dev/null 2>&1; then
		ok "pygam imports cleanly"
	elif [ -n "$PGV" ]; then
		fail "pygam is installed but does not import. Kalico imports all of
         klippy/extras at startup, so this takes the printer down."
	else
		attn "pygam is not installed -- [beacon_adaptive_heat_soak] would fail"
	fi

	if "$PY" -m pip check >/dev/null 2>&1; then
		ok "pip check: no broken dependencies"
	else
		attn "pip check reports problems:"
		# Guarded: under set -euo pipefail this pipeline's non-zero status would
		# abort the script before the beacon gate and the verdict ever print --
		# and it is non-zero precisely when the venv is broken.
		{ "$PY" -m pip check 2>&1 | sed 's/^/         /' | head -8; } || true
	fi
else
	fail "no klippy venv at $HOME/klippy-env"
fi
echo

# --- third-party modules ----------------------------------------------------

say "Third-party klippy modules"
if [ -f "$HOME/beacon/beacon.py" ]; then
	if grep -q "def multi_probe_begin" "$HOME/beacon/beacon.py" &&
		grep -q "def run_probe(self, gcmd, \*args" "$HOME/beacon/beacon.py"; then
		ok "beacon implements the legacy probe API Kalico uses, with a
         variadic run_probe that absorbs Kalico's extra retry_session argument"
	else
		fail "beacon does NOT expose the legacy probe protocol Kalico drives
         (multi_probe_begin / run_probe(gcmd, *args)). Kalico's probe.py
         predates Klipper's probe-session API. Do not proceed."
	fi
else
	attn "no ~/beacon/beacon.py found -- skipping the probe API check"
fi

AT="$HOME/klipper_tmc_autotune/autotune_tmc.py"
if [ -f "$AT" ]; then
	if grep -q "from klippy.extras import tmc" "$AT"; then
		ok "klipper_tmc_autotune has explicit Kalico support"
	else
		fail "klipper_tmc_autotune has no Kalico import path. Kalico renamed the
         TMC current helpers; update it before migrating."
	fi
else
	attn "klipper_tmc_autotune not found at $AT -- skipping"
fi
echo

# --- config that regeneration would destroy ---------------------------------

say "Generated config"
# `find | head` under pipefail exits non-zero when the directory is missing,
# which would kill the whole script before it prints its verdict.
GEN="$(find "$PRINTER_DATA_DIR/config" -maxdepth 1 -name 'RatOS*.cfg' 2>/dev/null | head -1 || true)"
if [ -n "$GEN" ]; then
	ok "generated config: $(basename "$GEN")"
	attn "This file says it is generated and will be overwritten. If you have
         hand-edited it -- bed size, run_current, parking positions, extra
         includes -- copy those edits into printer.cfg BEFORE migrating.
         Diff it against the configurator's templates if unsure."
else
	attn "no generated RatOS*.cfg found at the top of config/"
fi
echo

# --- verdict ----------------------------------------------------------------

if [ "$FAIL" -eq 1 ]; then
	say "NOT READY -- fix the [FAIL] items above first."
	exit 1
fi
if [ "$WARNED" -eq 1 ]; then
	say "Ready, with notes. Read the [note] lines before proceeding."
else
	say "Ready."
fi
note "Preflight only checks what can be checked from the outside."
note "It cannot tell you the printer will move correctly. Work through"
note "docs/TESTPLAN.md before the first print."
exit 0
