#!/usr/bin/env bash
#
# End-to-end check of the whole fork definition, from pristine upstreams.
#
# Fetches Kalico and RatOS-configurator, applies both patch sets, then proves
# the results behave. Needs git, python3 and network. Needs no Klipper, no
# Kalico runtime, no printer.
#
#   tests/run-all.sh
#
# Exit 0 means every patch applies to current upstream and every behavioural
# check passes. It does NOT mean the printer will work -- see docs/TESTPLAN.md.

SCRIPT_DIR="$(cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" &>/dev/null && pwd)"
# shellcheck source=scripts/lib.sh
source "$SCRIPT_DIR/../scripts/lib.sh"

FAILED=0
run() {
	local label="$1"
	shift
	printf '\n--- %s ---\n' "$label"
	if "$@"; then
		return 0
	fi
	printf '!!! FAILED: %s\n' "$label"
	FAILED=1
}

need git
need python3
ensure_work_dir

# --- 1. build both forks from pristine upstream ----------------------------

say "Building the Kalico fork from upstream"
"$SCRIPT_DIR/../scripts/build-kalico-fork.sh" >"$WORK_DIR/build-kalico.log" 2>&1 || {
	cat "$WORK_DIR/build-kalico.log" >&2
	die "the Kalico fork does not build against current upstream"
}
KALICO_COMMIT="$(cat "$WORK_DIR/kalico-commit.txt")"
note "Kalico fork: $KALICO_COMMIT"

say "Building the configurator fork from upstream"
"$SCRIPT_DIR/../scripts/build-configurator-fork.sh" \
	--kalico-commit "$KALICO_COMMIT" >"$WORK_DIR/build-configurator.log" 2>&1 || {
	cat "$WORK_DIR/build-configurator.log" >&2
	die "the configurator fork does not build against current upstream"
}
note "configurator fork built"

KALICO="$WORK_DIR/kalico"
CONF="$WORK_DIR/configurator"

# --- 2. bed_mesh: the mesh must be unchanged -------------------------------

PRISTINE_MESH="$WORK_DIR/bed_mesh.pristine.py"
git -C "$KALICO" show "HEAD~1:klippy/extras/bed_mesh.py" >"$PRISTINE_MESH"
run "Kalico bed_mesh port" \
	python3 "$SCRIPT_DIR/test_bed_mesh_port.py" \
	"$PRISTINE_MESH" "$KALICO/klippy/extras/bed_mesh.py"

# --- 3. kinematics: Kalico's contract --------------------------------------

KIN="$CONF/configuration/klippy/kinematics/ratos_hybrid_corexy.py"
run "patched kinematics satisfies Kalico" \
	python3 "$SCRIPT_DIR/test_kinematics.py" "$KIN"

# The same test against pristine upstream must FAIL. If it passes, the test has
# stopped measuring anything and every green run above is meaningless.
PRISTINE_KIN="$WORK_DIR/ratos_hybrid_corexy.pristine.py"
git -C "$CONF" show "HEAD~1:configuration/klippy/kinematics/ratos_hybrid_corexy.py" \
	>"$PRISTINE_KIN"
printf '\n--- control: pristine kinematics must NOT satisfy Kalico ---\n'
if python3 "$SCRIPT_DIR/test_kinematics.py" "$PRISTINE_KIN" >/dev/null 2>&1; then
	printf '!!! FAILED: unpatched RatOS kinematics passed the Kalico contract test.\n'
	printf '    The test is no longer measuring anything -- fix it before trusting this run.\n'
	FAILED=1
else
	printf 'ok: unpatched kinematics fails as expected\n'
fi

# --- 4. everything still parses --------------------------------------------

printf '\n--- syntax ---\n'
for f in \
	"$CONF/configuration/scripts/klipper-fork-migration.sh" \
	"$CONF/configuration/scripts/ratos-common.sh"; do
	bash -n "$f" && printf 'ok: %s\n' "$(basename "$f")" || FAILED=1
done
for f in \
	"$CONF/configuration/klippy/kinematics/ratos_hybrid_corexy.py" \
	"$CONF/configuration/klippy/ratos_homing.py" \
	"$CONF/configuration/klippy/resonance_generator.py" \
	"$KALICO/klippy/extras/bed_mesh.py" \
	"$KALICO/klippy/extras/gcode_macro.py"; do
	python3 -m py_compile "$f" && printf 'ok: %s\n' "$(basename "$f")" || FAILED=1
done

printf '\n--- unbound names ---\n'
python3 "$SCRIPT_DIR/check_undefined_names.py" \
	"$CONF/configuration/klippy/kinematics/ratos_hybrid_corexy.py" \
	"$CONF/configuration/klippy/ratos_homing.py" \
	"$CONF/configuration/klippy/resonance_generator.py" ||
	FAILED=1

# --- 5. the patcher is idempotent ------------------------------------------

printf '\n--- idempotency ---\n'
if python3 "$REPO_ROOT/configurator/patch_configurator.py" \
	--checkout "$CONF" \
	--kalico-upstream-url "$UPSTREAM_KALICO_URL" \
	--kalico-url "$FORK_KALICO_URL" \
	--kalico-branch "$FORK_KALICO_BRANCH" \
	--kalico-commit "$KALICO_COMMIT" \
	--configurator-url "$FORK_CONFIGURATOR_URL" \
	--deployment-branch "$FORK_CONFIGURATOR_DEPLOYMENT_BRANCH" \
	--check; then
	printf 'ok: re-running the patcher is a no-op\n'
else
	printf '!!! FAILED: the patcher does not consider its own output fully patched\n'
	FAILED=1
fi

# --- verdict ---------------------------------------------------------------

printf '\n'
if [ "$FAILED" -eq 0 ]; then
	say "All checks passed."
	note "This proves the patches apply to today's upstream and that the code"
	note "behaves as intended off-printer. It proves nothing about the machine."
	note "docs/TESTPLAN.md is the part that does."
	exit 0
fi
say "FAILURES above."
exit 1
