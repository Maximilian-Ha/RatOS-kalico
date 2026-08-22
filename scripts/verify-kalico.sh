#!/usr/bin/env bash
#
# Post-migration check: is this printer actually RUNNING Kalico?
#
# Read-only. Answers three separate questions, because they can disagree:
#   1. is the CODE in ~/klipper Kalico?
#   2. is the RUNNING process that code?
#   3. did it start cleanly, with the third-party modules intact?
#
#   scp scripts/verify-kalico.sh pi@printer:~/ && ssh pi@printer 'bash ~/verify-kalico.sh'

set -uo pipefail # deliberately not -e: a failing check must not end the run

KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
PRINTER_DATA_DIR="${PRINTER_DATA_DIR:-$HOME/printer_data}"
LOG="$PRINTER_DATA_DIR/logs/klippy.log"

FAIL=0
say() { printf '\n==> %s\n' "$*"; }
ok() { printf '  [ ok ] %s\n' "$*"; }
fail() {
	printf '  [FAIL] %s\n' "$*"
	FAIL=1
}
attn() { printf '  [note] %s\n' "$*"; }

say "1. The code in $KLIPPER_DIR"
# Kalico makes klippy a package and puts its identity in __init__.py. Stock
# Klipper has no klippy/__init__.py at all, so this file existing is already
# most of the answer.
INIT="$KLIPPER_DIR/klippy/__init__.py"
if [ -f "$INIT" ] && grep -q 'APP_NAME *= *"Kalico"' "$INIT"; then
	ok "klippy/__init__.py declares APP_NAME = \"Kalico\""
else
	fail "klippy/__init__.py does not declare APP_NAME = \"Kalico\".
         This checkout is not Kalico. Stock Klipper has no klippy/__init__.py
         at all, so if the file is missing entirely, the migration did not
         change the tree."
fi
[ -f "$KLIPPER_DIR/klippy/compat.py" ] &&
	ok "klippy/compat.py present (Kalico's import shim)" ||
	attn "no klippy/compat.py -- unexpected for Kalico"

if git -C "$KLIPPER_DIR" rev-parse --git-dir >/dev/null 2>&1; then
	ok "HEAD:   $(git -C "$KLIPPER_DIR" rev-parse HEAD)"
	ok "branch: $(git -C "$KLIPPER_DIR" branch --show-current 2>/dev/null || echo '(detached)')"
	printf '         remotes:\n'
	git -C "$KLIPPER_DIR" remote -v | sed 's/^/           /'
	git -C "$KLIPPER_DIR" diff-index --quiet HEAD -- 2>/dev/null &&
		ok "working tree clean" ||
		fail "working tree modified -- the NEXT update will abort on this"
fi

say "2. The process that is actually running"
# printer.py logs this block at every start, and klippy.log is rotated on
# start, so what is in it now describes the running process -- not a past one.
if [ -r "$LOG" ]; then
	if grep -q 'App Name: Kalico' "$LOG"; then
		ok "klippy.log says the running process is Kalico:"
		grep -A6 'App Name:' "$LOG" | head -7 | sed 's/^/           /'
	elif grep -q 'App Name:' "$LOG"; then
		fail "klippy.log reports a different App Name:"
		grep -m1 -A6 'App Name:' "$LOG" | sed 's/^/           /'
	else
		fail "no 'App Name:' line in klippy.log. Stock Klipper never writes one,
         so this is almost certainly still Klipper. Restart klipper and look
         again:  sudo systemctl restart klipper"
	fi
else
	fail "cannot read $LOG"
fi

say "3. Did it start cleanly"
if [ -r "$LOG" ]; then
	BAD="$(grep -nE 'Unknown config object|Option .* is not valid|Internal error|Traceback' "$LOG" | tail -5)"
	if [ -n "$BAD" ]; then
		fail "the log carries config or import errors:"
		printf '%s\n' "$BAD" | sed 's/^/           /'
	else
		ok "no unknown-config-object, invalid-option or traceback lines"
	fi
	grep -q 'Timer too close' "$LOG" &&
		fail "'Timer too close' is in the log -- see docs/RISKS.md and TESTPLAN stage 4" ||
		ok "no 'Timer too close'"
fi

# The modules that had to survive the repo switch.
for f in autotune_tmc beacon; do
	p="$KLIPPER_DIR/klippy/extras/$f.py"
	if [ -e "$p" ]; then
		[ -L "$p" ] && ok "$f.py -> $(readlink "$p")" || ok "$f.py present (regular file)"
	else
		fail "$f.py is GONE from klippy/extras. Klippy cannot load the sections
         that need it. Re-create it: ~/klipper_tmc_autotune/install.sh for
         autotune, or re-run ratos-update.sh for RatOS-registered modules."
	fi
done

say "$([ "$FAIL" -eq 0 ] && echo 'Kalico is running.' || echo 'NOT confirmed -- see the [FAIL] lines.')"
printf '    This says which firmware is running and that it loaded.\n'
printf '    It says nothing about motion. docs/TESTPLAN.md stage 3 does.\n'
exit "$FAIL"
