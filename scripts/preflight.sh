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
		ok "beacon implements the legacy probe API Kalico uses, with a
         variadic run_probe that absorbs Kalico's extra retry_session argument"
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
	fail "printer.cfg declares [beacon], but beacon.py was not found -- so the
         probe-protocol check did NOT run. Kalico's probe.py predates
         Klipper's probe-session API and drives the legacy protocol, and
         every mesh, Z-tilt and contact routine goes through it. Locate it
         and check by hand before migrating:
             ls -l ~/klipper/klippy/extras/beacon.py
             grep -n 'def multi_probe_begin\\|def run_probe' \"$(readlink -f ~/klipper/klippy/extras/beacon.py)\""
else
	attn "beacon is neither installed nor declared -- skipping the probe API check"
fi

AT="$(resolve_extra autotune_tmc "$HOME/klipper_tmc_autotune/autotune_tmc.py" || true)"
if [ -n "$AT" ]; then
	if grep -q "from klippy.extras import tmc" "$AT"; then
		ok "klipper_tmc_autotune has explicit Kalico support ($AT)"
	else
		fail "klipper_tmc_autotune at $AT has no Kalico import path. Kalico
         moved the TMC helpers into a klippy package; update it before
         migrating."
	fi
elif declares '\[autotune_tmc'; then
	fail "printer.cfg declares [autotune_tmc], but this script cannot find
         autotune_tmc.py anywhere -- so the one Kalico-compatibility check
         that module has did NOT run. It is installed (klippy would not
         start otherwise), just not where this looked. Kalico imports every
         module in klippy/extras eagerly and swallows import errors
         silently, so a module that is wrong for Kalico will not announce
         itself; it will simply stop tuning. Locate and check it by hand:
             ls -l ~/klipper/klippy/extras/autotune_tmc.py
             grep -n 'import tmc' \"\$(readlink -f ~/klipper/klippy/extras/autotune_tmc.py)\"
         The line you want is: from klippy.extras import tmc"
else
	attn "klipper_tmc_autotune is neither installed nor declared -- skipping"
fi
echo

# --- config that regeneration would destroy ---------------------------------

say "Generated config"
# Report ALL of them, and say which one is live. `head -1` picked an arbitrary
# file, which is wrong the moment a regeneration has left both a fresh
# RatOS.cfg and the older, hand-edited RatOS_4.1.cfg side by side -- exactly
# the state that loses hand edits, and the one this check exists to catch.
PCFG="$PRINTER_DATA_DIR/config/printer.cfg"
GENS=()
while IFS= read -r g; do GENS+=("$g"); done < <(
	find "$PRINTER_DATA_DIR/config" -maxdepth 1 -name 'RatOS*.cfg' 2>/dev/null | sort || true)

if [ "${#GENS[@]}" -eq 0 ]; then
	attn "no generated RatOS*.cfg at the top of config/"
else
	for g in "${GENS[@]}"; do
		b="$(basename "$g")"
		if grep -qs "^\[include $b\]" "$PCFG"; then
			ok "$b -- included by printer.cfg, so this is the live one"
		else
			attn "$b -- present but NOT included by printer.cfg. Either a leftover
         or a fresh regeneration that is not wired up. Do not assume the
         file you edited is the file Klippy reads."
		fi
		# A generated file that includes something out of Custom_settings/ has
		# been hand-edited: the configurator never emits those. On this printer
		# that include is what makes it a 600, and regeneration deletes it.
		HAND="$(grep -n '^\[include Custom_settings/' "$g" 2>/dev/null || true)"
		if [ -n "$HAND" ]; then
			attn "$b contains hand-added includes, which regeneration DELETES:"
			printf '%s\n' "$HAND" | sed 's/^/           /'
			printf '         Move them into printer.cfg before migrating.\n'
		fi
	done
	attn "Every RatOS*.cfg above says it is generated and will be overwritten.
         Any other hand edit -- bed size, run_current, parking positions --
         belongs in printer.cfg too. Diff against the configurator templates
         if unsure."
fi

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
