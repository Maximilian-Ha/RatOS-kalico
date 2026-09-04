#!/usr/bin/env python3
"""Flag names a function reads but nothing ever binds.

`python -m py_compile` accepts `X, Y, Z = pos[:3]` followed by a use of `E`
perfectly happily -- the NameError only happens at runtime, and on a printer
Klippy turns that into an emergency shutdown. A transform that renames or
re-shapes an assignment is exactly the kind of edit that leaves a dangling
reader behind, so the build checks for it.

Uses only the standard library: `symtable` gives, per scope, which names are
assigned and which are merely read. Anything read but never assigned anywhere
in the file, and not a builtin or an import, is reported.

This is deliberately conservative -- it is a tripwire for our own patches, not
a linter. It will not catch conditional binding, and it does not try to.

Usage:
    check_undefined_names.py FILE [FILE ...]

Exit codes:
    0  nothing suspicious
    1  at least one name is read but never bound
    2  bad invocation / unparseable file
"""

import builtins
import symtable
import sys

BUILTINS = set(dir(builtins))

# Bound by the import machinery rather than by any statement in the file, so
# symtable reports them as free reads. `__file__` is how a klippy extension
# finds data files shipped beside it -- beacon_adaptive_heat_soak.py loads its
# model training CSV that way -- and flagging it is a false alarm, which is the
# one thing a tripwire may not do.
MODULE_DUNDERS = {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__builtins__",
    "__debug__",
}


def bound_names(table, acc):
    """Every name bound anywhere in the file, at any scope depth."""
    for name in table.get_identifiers():
        try:
            sym = table.lookup(name)
        except KeyError:
            continue
        if sym.is_assigned() or sym.is_imported() or sym.is_parameter():
            acc.add(name)
    if table.get_type() == "function":
        # Parameters count as bound even when never re-assigned.
        acc.update(table.get_parameters())
    for child in table.get_children():
        bound_names(child, acc)
    return acc


def free_reads(table, bound, out, path):
    """Names read in a scope that nothing in the file ever binds."""
    if table.get_type() == "function":
        for name in table.get_identifiers():
            try:
                sym = table.lookup(name)
            except KeyError:
                continue
            if sym.is_assigned() or sym.is_parameter() or sym.is_imported():
                continue
            if not sym.is_referenced():
                continue
            if name in BUILTINS or name in MODULE_DUNDERS or name in bound:
                continue
            out.append((path, table.get_name(), name))
    for child in table.get_children():
        free_reads(child, bound, out, path)
    return out


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2

    findings = []
    for path in argv[1:]:
        try:
            with open(path, "r") as handle:
                source = handle.read()
            table = symtable.symtable(source, path, "exec")
        except (OSError, SyntaxError) as exc:
            sys.stderr.write("ERROR: cannot analyse %s: %s\n" % (path, exc))
            return 2
        free_reads(table, bound_names(table, set()), findings, path)

    if not findings:
        print("ok: no unbound names in %d file(s)" % (len(argv) - 1))
        return 0

    for path, scope, name in findings:
        sys.stderr.write(
            "%s: in %s(): '%s' is read but never bound -- this is a NameError "
            "at runtime, which Klippy escalates to an emergency shutdown\n"
            % (path, scope, name)
        )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
