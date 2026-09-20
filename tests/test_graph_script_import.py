#!/usr/bin/env python3
"""The graph scripts must not drag the printer stack in with them.

RatOS' belt-tension and input-shaper macros invoke Kalico's own
``scripts/graph_accelerometer.py`` and ``scripts/calibrate_shaper.py``
*directly*, so the interpreter is whatever their shebang names -- the system
python3, not the klippy venv. That interpreter has matplotlib and numpy; it does
not have cffi.

Kalico made ``klippy`` a package whose ``__init__`` is ``from .printer import
*``, so the stock ``from klippy.extras import shaper_calibrate`` pulls in
printer -> mcu -> chelper -> ``import cffi`` and every graph dies with
ModuleNotFoundError before drawing anything.

This checks the fix from the outside: execute each script's real import
preamble and assert that shaper_calibrate arrived while the printer stack did
not. matplotlib and numpy are stubbed, because this test has to run anywhere --
the point is the klippy import, not the plotting libraries.

Run against the PATCHED scripts it passes; run against pristine Kalico it fails,
and run-all.sh asserts exactly that so the test cannot decay into a tautology.

Usage: test_graph_script_import.py <script.py> [<script.py> ...]
"""

import pathlib
import sys
import types

# Anything the scripts import for plotting. Stubbed so the test is portable;
# a missing one of these is not what we are testing.
STUB_MODULES = (
    "matplotlib",
    "matplotlib.pyplot",
    "matplotlib.font_manager",
    "matplotlib.ticker",
    "matplotlib.colors",
    "matplotlib.figure",
    "numpy",
)

# The preamble ends at the first real declaration. Both scripts define this
# constant immediately after their imports.
PREAMBLE_END = "MAX_TITLE_LENGTH"


def install_stubs():
    for name in STUB_MODULES:
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__path__ = []
        module.use = lambda *a, **k: None
        sys.modules[name] = module


def drop_klippy():
    for name in [n for n in sys.modules if n == "klippy" or n.startswith("klippy.")]:
        del sys.modules[name]
    sys.modules.pop("cffi", None)


def check(path):
    source = pathlib.Path(path).read_text()
    if PREAMBLE_END not in source:
        return ["%s: no %s, cannot locate the preamble" % (path, PREAMBLE_END)]
    preamble = source.split(PREAMBLE_END)[0]

    drop_klippy()
    namespace = {"__file__": str(path), "__name__": "__import_probe__"}
    try:
        exec(compile(preamble, str(path), "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 - the failure itself is the result
        return ["%s: preamble raised %s: %s" % (path, type(exc).__name__, exc)]

    failures = []
    calibrate = namespace.get("shaper_calibrate")
    if calibrate is None:
        failures.append("%s: shaper_calibrate was not imported" % path)
    elif not getattr(calibrate, "__file__", "").endswith("extras/shaper_calibrate.py"):
        failures.append("%s: shaper_calibrate came from %r" % (path, getattr(calibrate, "__file__", None)))
    elif not hasattr(calibrate, "shaper_defs"):
        failures.append(
            "%s: shaper_calibrate imported but its own `from . import shaper_defs` "
            "did not resolve -- the stub package has no usable __path__" % path
        )

    if "klippy.printer" in sys.modules:
        failures.append(
            "%s: klippy.printer was loaded. The whole printer stack came with the "
            "import, which is what breaks this under the system python3." % path
        )
    if "cffi" in sys.modules:
        failures.append(
            "%s: cffi was loaded. On a stock RatOS image the system python3 does "
            "not have it, so this is the ModuleNotFoundError users see." % path
        )
    return failures


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    install_stubs()

    failures = []
    for path in argv[1:]:
        result = check(path)
        name = pathlib.Path(path).name
        if result:
            print("  FAIL %s" % name)
            failures.extend(result)
        else:
            print("  PASS %s imports shaper_calibrate without the printer stack" % name)

    if failures:
        print()
        for line in failures:
            print("  %s" % line)
        print("\n%d check(s) failed" % len(failures))
        return 1
    print("\n%d script(s) clean" % (len(argv) - 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
