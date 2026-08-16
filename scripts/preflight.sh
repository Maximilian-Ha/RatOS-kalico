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
	ok "branch: $(git -C "$KLIPPER_DIR" branch --show-current 2>/dev/null || echo '(detached)')"

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

say "Klippy virtualenv"
PY="$HOME/klippy-env/bin/python"
if [ -x "$PY" ]; then
	ok "venv python: $PY ($("$PY" --version 2>&1))"
	for mod in numpy jinja2; do
		V="$("$PY" -c "import $mod, sys; sys.stdout.write($mod.__version__)" 2>/dev/null || true)"
		if [ -n "$V" ]; then
			ok "$mod $V"
		else
			fail "$mod not importable in the klippy venv"
		fi
	done
	# Kalico imports EVERY module in klippy/extras at startup, not lazily when a
	# section appears. beacon_adaptive_heat_soak.py imports pygam at module
	# level, so a missing pygam is a boot-time failure for everyone -- and it
	# fails silently, surfacing only later at get_init_function.
	if "$PY" -c "import pygam" >/dev/null 2>&1; then
		ok "pygam importable"
	else
		fail "pygam is not importable. Kalico eagerly imports all of
         klippy/extras, so beacon_adaptive_heat_soak.py will fail on every
         boot -- silently, which is worse."
	fi
	NPV="$("$PY" -c "import numpy,sys; sys.stdout.write(numpy.__version__)" 2>/dev/null || true)"
	case "$NPV" in
	2.*) ok "numpy is 2.x, which Kalico requires" ;;
	"") : ;;
	*) attn "numpy $NPV -- Kalico pins 2.x. The upgrade must be tested against
         pygam and the four RatOS modules that use numpy." ;;
	esac
	JV="$("$PY" -c "import jinja2,sys; sys.stdout.write(jinja2.__version__)" 2>/dev/null || true)"
	case "$JV" in
	3.*) ok "jinja2 is 3.x, which Kalico requires" ;;
	"") : ;;
	*) attn "jinja2 $JV -- Kalico requires >=3.1.6. That upgrade changes macro
         template semantics for every RatOS macro. Read docs/RISKS.md." ;;
	esac
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
GEN="$(find "$PRINTER_DATA_DIR/config" -maxdepth 1 -name 'RatOS*.cfg' 2>/dev/null | head -1)"
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
