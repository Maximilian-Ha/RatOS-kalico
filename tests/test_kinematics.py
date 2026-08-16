#!/usr/bin/env python3
"""Prove the patched kinematics satisfies Kalico's contract.

The point is not that the file imports. It is that the three things Kalico
does to a kinematics object -- and that upstream RatOS' module cannot survive --
now work, while everything Klipper does still works too.

Runs against a real checkout without Klipper, Kalico or hardware: the Klipper
objects the constructor touches are stubbed, and the module is instantiated the
same way toolhead.py does it, with this printer's real axis limits.

Usage:
    tests/test_kinematics.py <path-to-ratos_hybrid_corexy.py>
"""

import os
import shutil
import sys
import tempfile

# The user's real V-Core 4.1 IDEX envelope, so the numbers in any failure are
# the numbers on the actual machine.
RANGES = {
    "stepper_x": (-75.0, 600.0),
    "stepper_y": (-1.0, 665.0),
    "stepper_z": (-5.0, 655.0),
    "dual_carriage": (0.0, 675.0),
}

STEPPER_STUB = '''
class _Stepper:
    def __init__(self, name):
        self._name = name
        self.itersolve = None
    def get_name(self):
        return self._name
    def setup_itersolve(self, alloc, mode):
        self.itersolve = (alloc, mode)
    def set_trapq(self, trapq):
        self.trapq = trapq
    def generate_steps(self, *a):
        pass
    def add_stepper(self, s):
        pass

class _Endstop:
    def add_stepper(self, s):
        pass

class _Rail:
    def __init__(self, name, rng):
        self._name = name
        self._range = rng
        self.steppers = [_Stepper(name)]
        self._endstops = [(_Endstop(), name)]
    def get_name(self):
        return self._name
    def get_range(self):
        return self._range
    def get_steppers(self):
        return list(self.steppers)
    def get_endstops(self):
        return list(self._endstops)
    def setup_itersolve(self, alloc, mode):
        for s in self.steppers:
            s.setup_itersolve(alloc, mode)
    def set_position(self, newpos):
        self.position = list(newpos)
    def get_homing_info(self):
        class _HI:
            position_endstop = 0.0
            positive_dir = False
        return _HI()

RANGES = %r

def LookupMultiRail(config):
    name = config.get_name()
    return _Rail(name, RANGES[name])
''' % (RANGES,)

IDEX_STUB = '''
class DualCarriagesRail:
    def __init__(self, rail, axis, active):
        self.rail = rail
        self.axis = axis
        self.active = active
    def get_rail(self):
        return self.rail

class DualCarriages:
    def __init__(self, config, rail0, rail1, axis):
        self.rails = [rail0, rail1]
        self.axis = axis
        self.primary = rail0
    def get_primary_rail(self):
        return self.primary
    def get_status(self, eventtime=None):
        return {"carriage_0": "PRIMARY", "carriage_1": "INACTIVE"}
    def home(self, homing_state):
        pass
'''


class StubConfig:
    """Just enough of Klipper's ConfigWrapper for this constructor."""

    def __init__(self, name, printer, sections):
        self._name = name
        self._printer = printer
        self._sections = sections

    def get_name(self):
        return self._name

    def get_printer(self):
        return self._printer

    def has_section(self, name):
        return name in self._sections

    def getsection(self, name):
        return StubConfig(name, self._printer, self._sections)

    def getboolean(self, key, default=None):
        return self._sections.get(self._name, {}).get(key, default)

    def getfloat(self, key, default=None, **kwargs):
        return self._sections.get(self._name, {}).get(key, default)

    def getchoice(self, key, choices, default=None):
        return default

    def error(self, msg):
        return Exception(msg)


class StubPrinter:
    def __init__(self):
        self.handlers = []

    def register_event_handler(self, name, cb):
        self.handlers.append((name, cb))

    def lookup_object(self, name, default=None):
        return default


class StubToolhead:
    """Mirrors what toolhead.py hands a kinematics constructor."""

    @staticmethod
    def Coord(*args, **kwargs):
        return tuple(args) + (kwargs.get("e", 0.0),)

    def get_trapq(self):
        return object()

    def register_step_generator(self, gen):
        pass

    def get_max_velocity(self):
        return (500.0, 10000.0)


def load_kinematics(path, with_dual_carriage=True):
    """Import the module under test from an isolated package."""
    tmp = tempfile.mkdtemp(prefix="ratos-kalico-kin-")
    pkg = os.path.join(tmp, "kinpkg")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(tmp, "stepper.py"), "w") as fh:
        fh.write(STEPPER_STUB)
    with open(os.path.join(pkg, "idex_modes.py"), "w") as fh:
        fh.write(IDEX_STUB)
    shutil.copy(path, os.path.join(pkg, "ratos_hybrid_corexy.py"))

    sys.path.insert(0, tmp)
    for mod in ("stepper", "kinpkg", "kinpkg.idex_modes", "kinpkg.ratos_hybrid_corexy"):
        sys.modules.pop(mod, None)
    import importlib

    module = importlib.import_module("kinpkg.ratos_hybrid_corexy")

    sections = {"ratos_hybrid_corexy": {"inverted": True}}
    if with_dual_carriage:
        sections["dual_carriage"] = {}
    printer = StubPrinter()
    config = StubConfig("printer", printer, sections)
    kin = module.RatOSHybridCoreXYKinematics(StubToolhead(), config)
    return module, kin, tmp


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - a failing check must not stop the run
        RESULTS.append((False, name, "%s: %s" % (type(exc).__name__, exc)))
    else:
        RESULTS.append((True, name, ""))


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(__doc__)
        return 2
    path = argv[1]
    if not os.path.isfile(path):
        sys.stderr.write("ERROR: no such file: %s\n" % path)
        return 2

    try:
        _module, kin, _tmp = load_kinematics(path)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            "FATAL: the kinematics could not even be constructed: %s: %s\n"
            % (type(exc).__name__, exc)
        )
        return 1

    # 1. Kalico's toolhead reads this attribute unguarded before it will accept
    #    a [dual_carriage] section. Upstream RatOS never sets it.
    def dual_carriage_declared():
        assert getattr(kin, "supports_dual_carriage", False) is True, (
            "supports_dual_carriage is not True -- Kalico's toolhead raises "
            "AttributeError during config load with [dual_carriage] present"
        )

    check("declares supports_dual_carriage", dual_carriage_declared)

    # 2. Kalico hands set_position the homed axes as a STRING.
    def set_position_accepts_names():
        kin.limits = [(1.0, -1.0)] * 3
        kin.set_position([0.0, 0.0, 0.0, 0.0], "xyz")
        assert kin.limits[0] == RANGES["stepper_x"], kin.limits[0]
        assert kin.limits[1] == RANGES["stepper_y"], kin.limits[1]
        assert kin.limits[2] == RANGES["stepper_z"], kin.limits[2]

    check("set_position accepts axis names ('xyz')", set_position_accepts_names)

    def set_position_accepts_indices():
        kin.limits = [(1.0, -1.0)] * 3
        kin.set_position([0.0, 0.0, 0.0, 0.0], [2])
        assert kin.limits[2] == RANGES["stepper_z"], kin.limits[2]
        assert kin.limits[0] == (1.0, -1.0), "untouched axes must stay unhomed"

    check("set_position still accepts Klipper's integer indices", set_position_accepts_indices)

    def set_position_defaults():
        # Kalico also calls set_position with no homing_axes at all.
        kin.limits = [(1.0, -1.0)] * 3
        kin.set_position([0.0, 0.0, 0.0, 0.0])
        assert kin.limits == [(1.0, -1.0)] * 3

    check("set_position has a default homing_axes", set_position_defaults)

    def set_position_single_axis():
        kin.limits = [(1.0, -1.0)] * 3
        kin.set_position([0.0, 0.0, 0.0, 0.0], "z")
        assert kin.limits[2] == RANGES["stepper_z"]
        assert kin.limits[0] == (1.0, -1.0)
        assert kin.limits[1] == (1.0, -1.0)

    check("set_position homes exactly the named axis", set_position_single_axis)

    # 3. Kalico calls clear_homing_state from stepper_enable.motor_off on every
    #    M84, from force_move's SET_KINEMATIC_POSITION and from safe_z_home.
    def clear_homing_state_exists():
        assert hasattr(kin, "clear_homing_state"), (
            "no clear_homing_state -- every M84 and every SET_KINEMATIC_POSITION "
            "raises AttributeError on Kalico"
        )

    check("has clear_homing_state", clear_homing_state_exists)

    def clear_homing_state_all():
        kin.set_position([0.0, 0.0, 0.0, 0.0], "xyz")
        kin.clear_homing_state("xyz")
        assert kin.limits == [(1.0, -1.0)] * 3, kin.limits

    check("clear_homing_state('xyz') unhomes everything", clear_homing_state_all)

    def clear_homing_state_selective():
        kin.set_position([0.0, 0.0, 0.0, 0.0], "xyz")
        kin.clear_homing_state("z")
        assert kin.limits[2] == (1.0, -1.0), "z should be cleared"
        assert kin.limits[0] == RANGES["stepper_x"], "x must survive"
        assert kin.limits[1] == RANGES["stepper_y"], "y must survive"

    check("clear_homing_state('z') clears only z", clear_homing_state_selective)

    def note_z_not_homed_still_works():
        kin.set_position([0.0, 0.0, 0.0, 0.0], "xyz")
        kin.note_z_not_homed()
        assert kin.limits[2] == (1.0, -1.0)
        assert kin.limits[0] == RANGES["stepper_x"], (
            "note_z_not_homed must not clear x -- it is the Safe Z Home helper"
        )

    check("note_z_not_homed survives as a wrapper", note_z_not_homed_still_works)

    # The dual carriage rail must still be reachable, because set_position
    # special-cases it and a broken branch there only shows up mid-toolchange.
    def dual_carriage_rail_wired():
        assert kin.dc_module is not None, "no dual carriage module was built"
        kin.limits = [(1.0, -1.0)] * 3
        kin.set_position([0.0, 0.0, 0.0, 0.0], "x")
        assert kin.limits[0] == RANGES["stepper_x"], (
            "with carriage 0 primary, homing x must take stepper_x's range"
        )

    check("dual carriage primary rail is used for the x limit", dual_carriage_rail_wired)

    def status_reports_homed_axes():
        kin.set_position([0.0, 0.0, 0.0, 0.0], "xyz")
        status = kin.get_status(0.0)
        assert status["homed_axes"] == "xyz", status["homed_axes"]
        kin.clear_homing_state("y")
        assert kin.get_status(0.0)["homed_axes"] == "xz"

    check("get_status reflects the homing state", status_reports_homed_axes)

    passed = sum(1 for ok, _n, _m in RESULTS if ok)
    for ok, name, msg in RESULTS:
        print("  %s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "\n       " + msg))
    print("\n%d/%d checks passed" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
