#!/usr/bin/env bash
#
# RatOS-Kalico preflight, self-contained.
#
# Identical checks to scripts/preflight.sh, but with no dependency on lib.sh,
# fork.conf or a checkout -- so it can be dropped onto a printer with a single
# scp and nothing else. READ-ONLY: it creates no files and changes no state.
#
#   scp scripts/preflight-standalone.sh pi@printer:~/
#   ssh pi@printer 'bash ~/preflight-standalone.sh'
#
# Exit 1 means do not migrate yet.

set -uo pipefail   # deliberately NOT -e: a failing probe must not end the run

KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
CONFIGURATOR_DIR="${CONFIGURATOR_DIR:-$HOME/ratos-configurator}"
PRINTER_DATA_DIR="${PRINTER_DATA_DIR:-$HOME/printer_data}"

FAIL=0
WARNED=0
say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '  [ ok ] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAIL=1; }
attn() { printf '  [note] %s\n' "$*"; WARNED=1; }

say "RatOS-Kalico preflight (read-only)"

# --- platform: every version decision below branches on this ----------------
say "Platform"
if [ -r /etc/os-release ]; then
	ok "os:   $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")"
else
	attn "no /etc/os-release"
fi
ok "arch: $(uname -m)"

# --- installation layout ----------------------------------------------------
say "Installation layout"
[ -d "$KLIPPER_DIR/klippy" ] && ok "klipper at $KLIPPER_DIR" || fail "no klippy/ in $KLIPPER_DIR"
[ -d "$CONFIGURATOR_DIR/configuration" ] && ok "configurator at $CONFIGURATOR_DIR" ||
	fail "no configuration/ in $CONFIGURATOR_DIR"
if [ -L "$PRINTER_DATA_DIR/config/RatOS" ]; then
	ok "config/RatOS -> $(readlink -f "$PRINTER_DATA_DIR/config/RatOS")"
else
	fail "config/RatOS is not a symlink -- this is not a stock RatOS 2.1 install"
fi

# --- klipper checkout: the migration hard-resets it -------------------------
say "Klipper checkout"
if git -C "$KLIPPER_DIR" rev-parse --git-dir >/dev/null 2>&1; then
	ok "origin: $(git -C "$KLIPPER_DIR" remote get-url origin 2>/dev/null || echo '(none)')"
	ok "HEAD:   $(git -C "$KLIPPER_DIR" rev-parse HEAD)"
	BR="$(git -C "$KLIPPER_DIR" branch --show-current 2>/dev/null)"
	ok "branch: ${BR:-(detached HEAD)}"
	printf '         ^ write these two down: they are your rollback target\n'
	if git -C "$KLIPPER_DIR" diff-index --quiet HEAD -- 2>/dev/null; then
		ok "working tree clean"
	else
		fail "working tree modified -- the migration aborts on this
         (KLIPPER_UNCOMMITTED_CHANGES). Inspect:
             git -C $KLIPPER_DIR status --short"
	fi
	if grep -q 'APP_NAME *= *"Kalico"' "$KLIPPER_DIR/klippy/__init__.py" 2>/dev/null; then
		ok "firmware flavour: Kalico"
		attn "already on Kalico -- this would be a re-point, not a migration"
	else
		ok "firmware flavour: Klipper"
	fi
else
	fail "$KLIPPER_DIR is not a git repository"
fi

# --- the klippy venv: where the one hard blocker lives ----------------------
say "Klippy virtualenv"
PY="$HOME/klippy-env/bin/python"
if [ -x "$PY" ]; then
	ok "python: $("$PY" -V 2>&1)"
	ver() { "$PY" -c "import $1,sys;sys.stdout.write(getattr($1,'__version__','?'))" 2>/dev/null; }
	NPV="$(ver numpy)"; SPV="$(ver scipy)"; JV="$(ver jinja2)"; PGV="$(ver pygam)"
	[ -n "$NPV" ] && ok "numpy  $NPV" || fail "numpy not importable"
	[ -n "$SPV" ] && ok "scipy  $SPV" || attn "scipy not importable"
	[ -n "$JV" ]  && ok "jinja2 $JV"  || fail "jinja2 not importable"
	[ -n "$PGV" ] && ok "pygam  $PGV" || attn "pygam not installed"

	# THE check. pygam 0.9.1 caps scipy below 1.12, and no such scipy supports
	# numpy 2 -- so numpy 2 and pygam cannot coexist. On Kalico that is a boot
	# blocker: [beacon_adaptive_heat_soak] imports pygam at module scope.
	case "$NPV" in
	2.*)
		if [ -n "$PGV" ]; then
			fail "numpy $NPV together with pygam $PGV cannot work. Klippy will die at
         config load on [beacon_adaptive_heat_soak]. The fork pins numpy<2."
		else
			attn "numpy $NPV and no pygam. Installing pygam later breaks this venv."
		fi ;;
	1.*) ok "numpy is 1.x -- the combination the fork targets" ;;
	esac

	case "$JV" in
	3.*) ok "jinja2 is 3.x" ;;
	"")  : ;;
	*)   attn "jinja2 $JV. Fine for Kalico's engine, but never upgrade markupsafe
         without jinja2 -- markupsafe 2.x removes soft_unicode." ;;
	esac

	if "$PY" -c "import pygam" >/dev/null 2>&1; then
		ok "pygam imports cleanly"
	elif [ -n "$PGV" ]; then
		fail "pygam installed but does not import -- Kalico imports all of
         klippy/extras at startup, so this takes the printer down"
	fi

	if "$PY" -m pip check >/dev/null 2>&1; then
		ok "pip check: no broken dependencies"
	else
		attn "pip check reports problems:"
		{ "$PY" -m pip check 2>&1 | sed 's/^/         /' | head -8; } || true
	fi
else
	fail "no klippy venv at $HOME/klippy-env"
fi

# --- third-party modules ----------------------------------------------------
# Third-party klippy modules are installed by symlinking one file out of their
# own checkout into klippy/extras, so the extras entry -- not a guessed
# ~/<name>/ path -- is what actually resolves. Guessing silently skipped the
# only Kalico-compat check klipper_tmc_autotune has on a machine whose
# printer.cfg declares [autotune_tmc] seven times. Resolve, then fall back.
resolve_extra() {
	local name="$1"
	shift
	local link="$KLIPPER_DIR/klippy/extras/$name.py" p
	if [ -e "$link" ]; then readlink -f "$link"; return 0; fi
	for p in "$@"; do
		if [ -e "$p" ]; then readlink -f "$p"; return 0; fi
	done
	# find does not follow symlinks, so this cannot wander into klippy/extras
	# or the RatOS symlink and re-find the link we already missed.
	p="$(find "$HOME" -maxdepth 3 -name "$name.py" -not -path '*/klippy-env/*' 2>/dev/null | head -1 || true)"
	if [ -n "$p" ]; then readlink -f "$p"; return 0; fi
	return 1
}

# Does this printer's own config declare the section? "not installed" and
# "installed somewhere I did not look" need different answers, and only the
# config can tell them apart. find does not follow the config/RatOS symlink,
# so the configurator's own templates cannot produce a false positive.
declares() {
	local hits
	hits="$(find "$PRINTER_DATA_DIR/config" -maxdepth 2 -name '*.cfg' -print0 2>/dev/null |
		xargs -0 -r grep -ls -- "$1" 2>/dev/null || true)"
	[ -n "$hits" ]
}

say "Third-party klippy modules"
BEACON="$(resolve_extra beacon "$HOME/beacon/beacon.py" || true)"
if [ -n "$BEACON" ]; then
	if grep -q "def multi_probe_begin" "$BEACON" &&
		grep -q "def run_probe(self, gcmd, \*args" "$BEACON"; then
		ok "beacon speaks the legacy probe protocol Kalico drives"
	else
		fail "beacon does not expose the legacy probe protocol Kalico drives
         (multi_probe_begin / run_probe(gcmd, *args)). Kalico's probe.py
         predates Klipper's probe-session API, so this beacon cannot probe
         on Kalico -- and every mesh, Z-tilt and contact routine goes
         through it.
         This is almost certainly an OUT OF DATE beacon rather than a dead
         end: current beacon_klipper master carries a BeaconProbeWrapper
         that implements BOTH protocols, and its history explicitly
         mentions Kalico support. Update it before migrating:
             cd ~/beacon && git log --oneline -1     # what you have now
             git -C ~/beacon pull
             sudo systemctl restart klipper
         then re-run this preflight. Beacon is managed by moonraker
         ([update_manager beacon], channel dev), so the update button in
         Mainsail does the same thing."
	fi
elif declares '\[beacon\]'; then
	fail "printer.cfg declares [beacon] but beacon.py was not found, so the
         probe-protocol check did NOT run. Kalico drives the legacy probe
         protocol and every mesh, Z-tilt and contact routine goes through
         it. Check it by hand before migrating:
             ls -l ~/klipper/klippy/extras/beacon.py
             grep -n 'def multi_probe_begin\\|def run_probe' \"$(readlink -f ~/klipper/klippy/extras/beacon.py)\""
else
	attn "beacon is neither installed nor declared -- skipping the probe API check"
fi
AT="$(resolve_extra autotune_tmc "$HOME/klipper_tmc_autotune/autotune_tmc.py" || true)"
if [ -n "$AT" ]; then
	grep -q "from klippy.extras import tmc" "$AT" &&
		ok "klipper_tmc_autotune has Kalico support ($AT)" ||
		fail "klipper_tmc_autotune at $AT has no Kalico import path -- update it first"
elif declares '\[autotune_tmc'; then
	fail "printer.cfg declares [autotune_tmc] but autotune_tmc.py was not found,
         so its Kalico check did NOT run. It is installed -- klippy would not
         start otherwise -- just not where this looked. Kalico swallows
         extras import errors silently, so check it by hand:
             ls -l ~/klipper/klippy/extras/autotune_tmc.py
             grep -n 'import tmc' \"\$(readlink -f ~/klipper/klippy/extras/autotune_tmc.py)\"
         The line you want is: from klippy.extras import tmc"
else
	attn "klipper_tmc_autotune is neither installed nor declared -- skipping"
fi

# --- config that regeneration would destroy ---------------------------------
say "Generated config"
GEN="$(find "$PRINTER_DATA_DIR/config" -maxdepth 1 -name 'RatOS*.cfg' 2>/dev/null | head -1 || true)"
if [ -n "$GEN" ]; then
	ok "generated config: $(basename "$GEN")"
	attn "Hand-edits in this file are lost on any regeneration -- above all the
         600mm include. Copy them into printer.cfg BEFORE migrating."
else
	attn "no generated RatOS*.cfg found"
fi

# --- verdict ----------------------------------------------------------------
if [ "$FAIL" -eq 1 ]; then
	say "NOT READY -- fix the [FAIL] items first."
	exit 1
fi
[ "$WARNED" -eq 1 ] && say "Ready, with notes." || say "Ready."
printf '    Preflight only checks what is visible from outside. It cannot tell\n'
printf '    you the printer will move correctly -- that is what the test plan is for.\n'
exit 0
