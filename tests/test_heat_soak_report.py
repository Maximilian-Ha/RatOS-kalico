#!/usr/bin/env python3
"""Prove the adaptive heat soak actually reports to the console while it runs.

The transform this checks (``t_beacon_heat_soak_console_report``) is small, but
the two ways it can silently stop working are not caught by anything else in
the build:

* **Scope drift.** The report must be a direct statement of the wait loop. One
  level deeper -- inside ``if z_rate_ra.is_full():``, the obvious place for it
  to end up after an upstream reshuffle -- and it goes quiet for exactly the
  first few minutes, the part of a soak where the operator has nothing else to
  look at. The file still compiles and the printer still soaks, so nothing
  else notices.
* **A name that is not bound on the path that reads it.**
  ``tests/check_undefined_names.py`` works per file, so a name bound only
  inside some other branch of the same function satisfies it. On a printer that
  is an UnboundLocalError raised out of a G-code command, i.e. a failed print,
  and it would happen five minutes into the first soak rather than at boot.

So this reads the patched module's AST rather than its text: where the report
sits, what it reads, and that both branches actually respond.

It deliberately does not simulate a soak. That would need numpy, pygam and a
beacon stub -- and the arithmetic it would exercise is upstream's, untouched
here.

Usage:
    tests/test_heat_soak_report.py <path-to-beacon_adaptive_heat_soak.py>

Exit codes:
    0  the module reports as intended
    1  it does not
    2  bad invocation / unparseable file
"""

import ast
import builtins
import sys

CLASS = "BeaconAdaptiveHeatSoak"
COMMAND = "cmd_BEACON_WAIT_FOR_PRINTER_HEAT_SOAK"
INTERVAL = "console_report_interval"
CLOCK = "next_console_report"


def fail(msg):
    print("FAIL: %s" % msg)
    return False


def find_command(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == CLASS:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == COMMAND:
                    return item
    return None


def find_wait_loop(func):
    """The one `while True:` in the command -- the soak's wait loop."""
    loops = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.While)
        and isinstance(n.test, ast.Constant)
        and n.test.value is True
    ]
    return loops[0] if len(loops) == 1 else None


def bound_before(func, loop):
    """Names bound anywhere in the command before the wait loop, plus the
    names bound at the loop's own top level -- i.e. re-bound on every
    iteration, before anything nested runs."""
    names = {"self"}
    names.update(a.arg for a in func.args.args)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.lineno < loop.lineno:
                names.add(node.id)
    for stmt in loop.body:
        if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            for target in ast.walk(stmt):
                if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
                    names.add(target.id)
    return names


def responds(node):
    """Does this branch call gcmd.respond_info?"""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "respond_info"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "gcmd"
        ):
            return True
    return False


def check(path):
    with open(path, "r") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        print("FAIL: %s does not parse: %s" % (path, exc))
        return False

    ok = True

    func = find_command(tree)
    if func is None:
        return fail("%s.%s is gone -- upstream has restructured the module" % (CLASS, COMMAND))

    # 1. the interval is configurable, with a per-call override.
    init = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == CLASS:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    init = item
    if init is None or "'%s'" % INTERVAL not in ast.dump(init).replace('"', "'"):
        ok = fail("[%s] does not read a '%s' config option" % (CLASS, INTERVAL))
    else:
        print("ok: '%s' is a config option" % INTERVAL)

    if "REPORT_INTERVAL" not in ast.dump(func):
        ok = fail("%s does not accept REPORT_INTERVAL=" % COMMAND)
    else:
        print("ok: REPORT_INTERVAL= overrides it per call")

    # 2. the report runs every iteration of the wait loop, not only once the
    #    moving average exists.
    loop = find_wait_loop(func)
    if loop is None:
        return fail("could not identify a single `while True:` wait loop in %s" % COMMAND)

    reports = [
        stmt
        for stmt in loop.body
        if isinstance(stmt, ast.If)
        and any(
            isinstance(n, ast.Name) and n.id == INTERVAL for n in ast.walk(stmt.test)
        )
    ]
    nested = [
        n
        for n in ast.walk(loop)
        if isinstance(n, ast.If)
        and n not in reports
        and any(isinstance(x, ast.Name) and x.id == INTERVAL for x in ast.walk(n.test))
    ]
    if not reports:
        if nested:
            return fail(
                "the console report is nested inside the wait loop rather than a "
                "statement of it -- it would go quiet exactly while there is nothing "
                "else to look at"
            )
        return fail("the wait loop does not report to the console at all")
    if len(reports) > 1:
        ok = fail("%d console reports in the wait loop, expected 1" % len(reports))
    else:
        print("ok: the console report runs on every iteration of the wait loop")

    report = reports[0]

    # 3. both branches actually say something: before the first moving average
    #    exists, and after.
    two_phase = [
        n
        for n in ast.walk(report)
        if isinstance(n, ast.If)
        and n.orelse
        and responds(ast.Module(body=n.body, type_ignores=[]))
        and responds(ast.Module(body=n.orelse, type_ignores=[]))
    ]
    if not two_phase:
        ok = fail(
            "the console report does not respond in both phases of the soak -- it "
            "needs one message before the first moving average exists and one after"
        )
    else:
        print("ok: both phases of the soak report")

    # 4. every name it reads is bound on the path that reads it.
    known = bound_before(func, loop)
    reads = sorted(
        {
            n.id
            for n in ast.walk(report)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
    )
    unbound = [n for n in reads if n not in known and not hasattr(builtins, n)]
    if unbound:
        ok = fail(
            "the console report reads %s, which nothing binds before it runs "
            "(UnboundLocalError mid-soak)" % ", ".join(unbound)
        )
    else:
        print("ok: all %d names it reads are bound before it runs" % len(reads))

    # 5. the clock advances, or the report fires once and never again.
    if not any(
        isinstance(n, ast.Name) and n.id == CLOCK and isinstance(n.ctx, ast.Store)
        for n in ast.walk(report)
    ):
        ok = fail("the report does not advance %s -- it would fire once only" % CLOCK)
    else:
        print("ok: the report re-arms its own clock")

    return ok


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: %s <beacon_adaptive_heat_soak.py>\n" % argv[0])
        return 2
    return 0 if check(argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
