#!/usr/bin/env python3
"""Prove the Kalico bed_mesh port changes behaviour only where intended.

A firmware patch that merely *runs* is worthless. What matters is that the mesh
Kalico computes is bit-for-bit what it computed before, because that mesh is
what the nozzle follows. So this builds the same mesh from pristine Kalico and
from the patched file, with and without a reactor, and compares element by
element.

It also asserts the three changes that are hard startup blockers actually
landed, and that the reactor yielding really happens rather than being dead
code behind a None check.

Needs no Klipper, no Kalico runtime and no hardware -- Kalico's bed_mesh only
imports collections/json/logging/math plus two siblings, both stubbed here.

Usage:
    tests/test_bed_mesh_port.py <pristine-bed_mesh.py> <patched-bed_mesh.py>
"""

import importlib
import os
import shutil
import sys
import tempfile

PROBE_STUB = """
class PrinterProbe:
    pass

class ProbePointsHelper:
    def __init__(self, *a, **kw):
        pass
"""

DANGER_STUB = """
class _Danger:
    log_bed_mesh_at_startup = True
    log_config_file_at_startup = True

def get_danger_options():
    return _Danger()
"""

# A mesh small enough to compare exhaustively but large enough that the
# bicubic interpolation actually has interior points to work on.
MESH_PARAMS = {
    "min_x": 20.0,
    "max_x": 580.0,
    "min_y": 20.0,
    "max_y": 580.0,
    "x_count": 7,
    "y_count": 7,
    "mesh_x_pps": 2,
    "mesh_y_pps": 2,
    "algo": "bicubic",
    "tension": 0.2,
}


def _z_matrix(x_count, y_count):
    """Deterministic, non-trivial, non-symmetric probe data."""
    return [
        [
            0.05 * ((x * 3 + y * 7) % 11) - 0.2 + 0.001 * x * y
            for x in range(x_count)
        ]
        for y in range(y_count)
    ]


class FakeReactor:
    """Counts yields instead of sleeping."""

    def __init__(self):
        self.yields = 0

    def monotonic(self):
        return 0.0

    def pause(self, waketime):
        self.yields += 1


def load(path, alias):
    tmp = tempfile.mkdtemp(prefix="ratos-kalico-mesh-")
    pkg = os.path.join(tmp, alias)
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(pkg, "probe.py"), "w") as fh:
        fh.write(PROBE_STUB)
    with open(os.path.join(pkg, "danger_options.py"), "w") as fh:
        fh.write(DANGER_STUB)
    shutil.copy(path, os.path.join(pkg, "bed_mesh.py"))
    sys.path.insert(0, tmp)
    return importlib.import_module("%s.bed_mesh" % alias)


RESULTS = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((False, name, "%s: %s" % (type(exc).__name__, exc)))
    else:
        RESULTS.append((True, name, ""))


def build(module, params, reactor=None):
    if reactor is None:
        mesh = module.ZMesh(dict(params), "test")
    else:
        mesh = module.ZMesh(dict(params), "test", reactor)
    mesh.build_mesh(_z_matrix(params["x_count"], params["y_count"]))
    return mesh


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    pristine_path, patched_path = argv[1], argv[2]
    for p in (pristine_path, patched_path):
        if not os.path.isfile(p):
            sys.stderr.write("ERROR: no such file: %s\n" % p)
            return 2

    try:
        pristine = load(pristine_path, "kalico_pristine")
        patched = load(patched_path, "kalico_patched")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("FATAL: could not import both modules: %s\n" % exc)
        return 1

    # --- the blockers actually landed --------------------------------------

    def zmesh_takes_reactor():
        # ratos.py:445, beacon_mesh.py:519 and :1285 all pass a third argument.
        patched.ZMesh(dict(MESH_PARAMS), "x", FakeReactor())

    check("patched ZMesh accepts the reactor argument", zmesh_takes_reactor)

    def pristine_rejects_reactor():
        try:
            pristine.ZMesh(dict(MESH_PARAMS), "x", FakeReactor())
        except TypeError:
            return
        raise AssertionError(
            "pristine Kalico accepted three arguments -- this test is not "
            "actually comparing against an unpatched file"
        )

    check("pristine ZMesh rejects it (control)", pristine_rejects_reactor)

    def split_delta_z_relaxed():
        src = open(patched_path).read()
        assert "minval=0.001" in src, (
            "split_delta_z minval was not relaxed; six V-Core 4 profiles ship "
            "0.001 and Klippy refuses to start"
        )

    check("split_delta_z minval relaxed to 0.001", split_delta_z_relaxed)

    def log_points_read():
        src = open(patched_path).read()
        assert '"log_points"' in src, "log_points option not introduced"
        assert '"log_points_truncate"' in src, "log_points_truncate not introduced"

    check("log_points / log_points_truncate are read", log_points_read)

    # --- the mesh is unchanged ---------------------------------------------

    ref = build(pristine, MESH_PARAMS)
    no_reactor = build(patched, MESH_PARAMS)
    reactor = FakeReactor()
    with_reactor = build(patched, MESH_PARAMS, reactor)

    def matrices_identical_no_reactor():
        assert no_reactor.get_mesh_matrix() == ref.get_mesh_matrix(), (
            "patched mesh differs from pristine with no reactor"
        )
        assert no_reactor.get_probed_matrix() == ref.get_probed_matrix()

    check("patched == pristine (no reactor)", matrices_identical_no_reactor)

    def matrices_identical_with_reactor():
        assert with_reactor.get_mesh_matrix() == ref.get_mesh_matrix(), (
            "patched mesh differs from pristine when yielding"
        )
        assert with_reactor.get_probed_matrix() == ref.get_probed_matrix()

    check("patched == pristine (with reactor)", matrices_identical_with_reactor)

    def calc_z_identical():
        # calc_z is what actually moves the nozzle, so sample it densely
        # rather than trusting the matrix comparison alone.
        mismatches = []
        for i in range(11):
            for j in range(11):
                x = 20.0 + i * 56.0
                y = 20.0 + j * 56.0
                a = ref.calc_z(x, y)
                b = with_reactor.calc_z(x, y)
                if a != b:
                    mismatches.append((x, y, a, b))
        assert not mismatches, "calc_z differs at %d/121 points: %r" % (
            len(mismatches),
            mismatches[:3],
        )

    check("calc_z identical at 121 points", calc_z_identical)

    def z_range_identical():
        assert with_reactor.get_z_range() == ref.get_z_range(), (
            "%r != %r" % (with_reactor.get_z_range(), ref.get_z_range())
        )

    check("get_z_range identical", z_range_identical)

    # --- the yielding is real ----------------------------------------------

    def yielding_happens():
        assert reactor.yields > 0, (
            "no reactor yields were recorded -- the CPU-hogging fix is dead code"
        )

    check("building a mesh yields to the reactor", yielding_happens)

    def yielding_scales():
        small = FakeReactor()
        big = FakeReactor()
        p_small = dict(MESH_PARAMS, x_count=5, y_count=5)
        p_big = dict(MESH_PARAMS, x_count=21, y_count=21)
        build(patched, p_small, small)
        build(patched, p_big, big)
        assert big.yields > small.yields, (
            "yield count did not grow with mesh size (%d vs %d) -- the yields "
            "are not in the loops that actually get long" % (small.yields, big.yields)
        )

    check("yield count grows with mesh size", yielding_scales)

    def no_yield_without_reactor():
        # ZMesh is constructed without a reactor in plenty of places; that
        # path must not blow up on a None.
        m = build(patched, MESH_PARAMS)
        m.get_mesh_matrix()
        m.print_mesh(lambda _msg: None)

    check("no reactor: still safe to build and print", no_yield_without_reactor)

    def lagrange_unchanged():
        # RatOS deliberately did not yield in _sample_lagrange. Make sure the
        # port did not quietly change that path either.
        p = dict(MESH_PARAMS, algo="lagrange", x_count=5, y_count=5)
        a = build(pristine, p)
        b = build(patched, p, FakeReactor())
        assert a.get_mesh_matrix() == b.get_mesh_matrix()

    check("algo=lagrange unchanged", lagrange_unchanged)

    def direct_unchanged():
        p = dict(MESH_PARAMS, algo="direct", mesh_x_pps=0, mesh_y_pps=0)
        a = build(pristine, p)
        b = build(patched, p, FakeReactor())
        assert a.get_mesh_matrix() == b.get_mesh_matrix()

    check("algo=direct unchanged", direct_unchanged)

    passed = sum(1 for ok, _n, _m in RESULTS if ok)
    for ok, name, msg in RESULTS:
        print("  %s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "\n       " + msg))
    print("\n%d/%d checks passed" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
