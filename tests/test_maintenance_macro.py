#!/usr/bin/env python3
"""Render MAINTENANCE_MODE the way Klippy does, and check what it emits.

A gcode_macro is a Jinja2 template that Klippy renders in full *before* its
first line executes. A typo in it is not a runtime warning -- it is a config
error at startup, or an exception mid-move on a machine with the nozzles down.
Nothing else in this repo can catch that, so this renders the two macros with a
stand-in for the 600's printer object and checks the coordinates they produce.

Emulates Klippy's gcode_macro.py: the same Jinja delimiters ('{%'/'%}' for
blocks, '{'/'}' for expressions), macro variables exposed as bare names, and
`params` from the call site.

Usage: test_maintenance_macro.py [path/to/maintenance.cfg]
"""

import ast
import configparser
import os
import re
import sys

try:
    import jinja2
except ImportError:  # pragma: no cover - reported, not worked around
    sys.stderr.write("jinja2 is not installed; cannot render the macro\n")
    sys.exit(2)

DEFAULT_CFG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "configurator", "printer-600", "maintenance.cfg",
)

# The 600's real numbers, from 600.cfg and printer.cfg.overrides.
BED_CENTER_X = 300.0
Z_MAX = 655.0
Z_MID = Z_MAX / 2          # 327.5
Y_MIN = -1.0
Y_FRONT = Y_MIN + 5        # 4.0
SAFE_DISTANCE = 60.0
SPACING = SAFE_DISTANCE + 10
X_T0 = BED_CENTER_X - SPACING / 2   # 265.0
X_T1 = BED_CENTER_X + SPACING / 2   # 335.0


class MacroError(Exception):
    """What action_raise_error() raises, as Klippy does."""


def printer_state(idex_mode="INACTIVE", homed="xyz", printing="standby"):
    """A stand-in for Klippy's `printer` object, with the 600's config.

    idex_mode is [dual_carriage].carriage_1 as Klippy reports it: INACTIVE when
    T0 is the active carriage, PRIMARY when T1 is, COPY or MIRROR in the
    duplication modes.
    """
    return {
        "toolhead": {
            "axis_minimum": {"x": -75.0, "y": Y_MIN, "z": -5.0},
            "axis_maximum": {"x": 600.0, "y": 665.0, "z": Z_MAX},
            "homed_axes": homed,
            "max_accel": 8000,
        },
        "dual_carriage": {"carriage_0": "PRIMARY", "carriage_1": idex_mode},
        "print_stats": {"state": printing},
        "configfile": {
            "settings": {
                "stepper_x": {"position_min": -75.0, "position_max": 600.0},
                "dual_carriage": {
                    "position_min": 0.0,
                    "position_max": 675.0,
                    "safe_distance": SAFE_DISTANCE,
                },
            }
        },
        "gcode_macro RatOS": {
            "macro_travel_speed": 150.0,
            "macro_z_speed": 15.0,
            "printable_x_max": 600.0,
            "printable_y_max": 600.0,
            "printable_y_min": 0.0,
            "default_toolhead": 0,
        },
        "gcode_macro T0": {"parking_position": -73.0, "active": True},
        "gcode_macro T1": {"parking_position": 673.0, "active": False},
    }


def load_macros(path):
    """{macro_name: (variables, gcode_template)} from a Klipper config file."""
    parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=("#", ";"))
    with open(path, "r") as handle:
        parser.read_file(handle)
    macros = {}
    for section in parser.sections():
        if not section.startswith("gcode_macro "):
            continue
        variables = {}
        for key, value in parser.items(section):
            if not key.startswith("variable_"):
                continue
            text = value.strip()
            try:
                variables[key[len("variable_"):]] = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                variables[key[len("variable_"):]] = text
        macros[section[len("gcode_macro "):]] = (variables, parser.get(section, "gcode"))
    return macros


def render(macros, name, printer, params=None):
    """Render one macro, returning its emitted g-code lines."""
    variables, template_text = macros[name]
    env = jinja2.Environment("{%", "%}", "{", "}", extensions=["jinja2.ext.do"])

    def raise_error(msg):
        raise MacroError(msg)

    context = dict(variables)
    context.update({
        "printer": printer,
        "params": params or {},
        "rawparams": "",
        "action_raise_error": raise_error,
        "action_respond_info": lambda msg: None,
        "action_emergency_stop": lambda msg="": raise_error(msg),
        "action_call_remote_method": lambda *a, **k: None,
    })
    text = env.from_string(template_text).render(context)
    return [line.strip() for line in text.split("\n") if line.strip()]


def index_of(lines, pattern):
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            return i
    raise AssertionError("no line matching %r in:\n  %s" % (pattern, "\n  ".join(lines)))


def check(path):
    macros = load_macros(path)
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    expect("MAINTENANCE_MODE" in macros, "MAINTENANCE_MODE is not defined")
    expect(
        "_MAINTENANCE_CENTER_TOOLHEADS" in macros,
        "_MAINTENANCE_CENTER_TOOLHEADS is not defined",
    )
    if failures:
        return failures

    # Mainsail lists macros whose name has no leading underscore. The entry
    # point must be visible; the helper must not be.
    expect(
        not [n for n in macros if n.startswith("_") and n != "_MAINTENANCE_CENTER_TOOLHEADS"],
        "an unexpected internal macro appeared in maintenance.cfg",
    )

    # --- 1. the service position, T0 active ---------------------------------
    lines = render(macros, "MAINTENANCE_MODE", printer_state())

    i_home = index_of(lines, r"^MAYBE_HOME\b")
    i_z = index_of(lines, r"^G0 Z")
    i_y = index_of(lines, r"^G0 Y")
    i_x = index_of(lines, r"^_MAINTENANCE_CENTER_TOOLHEADS\b")

    expect(i_home < i_z, "homing must happen before the first absolute move")
    expect(i_z < i_y < i_x, "order must be bed height, then gantry, then toolheads")
    expect(
        index_of(lines, r"^G90\b") < i_z,
        "the moves must be made in absolute positioning",
    )

    z_line = lines[i_z]
    expect(
        abs(float(re.search(r"G0 Z([-\d.]+)", z_line).group(1)) - Z_MID) < 1e-6,
        "bed goes to %s, expected mid travel %s (%s)" % (z_line, Z_MID, z_line),
    )
    y_line = lines[i_y]
    expect(
        abs(float(re.search(r"G0 Y([-\d.]+)", y_line).group(1)) - Y_FRONT) < 1e-6,
        "gantry goes to %s, expected the front at Y%s" % (y_line, Y_FRONT),
    )
    x0 = float(re.search(r"X0=([-\d.]+)", lines[i_x]).group(1))
    x1 = float(re.search(r"X1=([-\d.]+)", lines[i_x]).group(1))
    expect(abs(x0 - X_T0) < 1e-6, "T0 goes to X%s, expected %s" % (x0, X_T0))
    expect(abs(x1 - X_T1) < 1e-6, "T1 goes to X%s, expected %s" % (x1, X_T1))
    expect(
        abs((x0 + x1) / 2 - BED_CENTER_X) < 1e-6,
        "the toolheads straddle X%s, not the bed centre X%s" % ((x0 + x1) / 2, BED_CENTER_X),
    )
    expect(
        x1 - x0 >= SAFE_DISTANCE,
        "the toolheads end %smm apart, closer than safe_distance %s -- Klipper "
        "would abort the move" % (x1 - x0, SAFE_DISTANCE),
    )

    # --- 2. overrides -------------------------------------------------------
    lines = render(
        macros, "MAINTENANCE_MODE", printer_state(),
        params={"Z": "100", "Y": "250", "SPACING": "200"},
    )
    expect(
        "G0 Z100.0" in " ".join(lines) or "G0 Z100" in " ".join(lines),
        "an explicit Z was not used",
    )
    expect(
        re.search(r"G0 Y250(\.0)? ", " ".join(lines)) is not None,
        "an explicit Y was not used",
    )
    call = lines[index_of(lines, r"^_MAINTENANCE_CENTER_TOOLHEADS")]
    x0 = float(re.search(r"X0=([-\d.]+)", call).group(1))
    x1 = float(re.search(r"X1=([-\d.]+)", call).group(1))
    expect(abs(x1 - x0 - 200) < 1e-6, "an explicit SPACING was not used (%s)" % call)

    # A spacing under safe_distance must be raised, not passed through.
    lines = render(macros, "MAINTENANCE_MODE", printer_state(), params={"SPACING": "5"})
    call = lines[index_of(lines, r"^_MAINTENANCE_CENTER_TOOLHEADS")]
    x0 = float(re.search(r"X0=([-\d.]+)", call).group(1))
    x1 = float(re.search(r"X1=([-\d.]+)", call).group(1))
    expect(
        x1 - x0 >= SAFE_DISTANCE,
        "SPACING=5 produced %smm between the carriages, below safe_distance"
        % (x1 - x0),
    )

    # Targets outside the machine must be clamped, not commanded.
    lines = render(macros, "MAINTENANCE_MODE", printer_state(), params={"Z": "9999"})
    z = float(re.search(r"G0 Z([-\d.]+)", lines[index_of(lines, r"^G0 Z")]).group(1))
    expect(z <= Z_MAX, "Z=9999 was not clamped to the Z limit (got %s)" % z)

    # --- 3. it must refuse to run through a print ---------------------------
    for state in ("printing", "paused"):
        try:
            render(macros, "MAINTENANCE_MODE", printer_state(printing=state))
        except MacroError:
            pass
        else:
            failures.append("MAINTENANCE_MODE did not refuse to run while %s" % state)

    # --- 4. the carriage moves ---------------------------------------------
    params = {"X0": str(X_T0), "X1": str(X_T1)}

    for mode, active in (("INACTIVE", 0), ("PRIMARY", 1)):
        lines = render(
            macros, "_MAINTENANCE_CENTER_TOOLHEADS", printer_state(idex_mode=mode),
            params=params,
        )
        expect(
            index_of(lines, r"^PARK_TOOLHEAD\b") < index_of(lines, r"^SET_DUAL_CARRIAGE"),
            "both carriages must be parked before closing in on the centre",
        )
        # Each carriage is selected, then moved.
        i0 = index_of(lines, r"^SET_DUAL_CARRIAGE CARRIAGE=0\b")
        expect(
            lines[i0 + 1].startswith("G0 X%s" % X_T0),
            "carriage 0 is not moved to X%s right after being selected" % X_T0,
        )
        i1 = index_of(lines, r"^SET_DUAL_CARRIAGE CARRIAGE=1\b")
        expect(
            lines[i1 + 1].startswith("G0 X%s" % X_T1),
            "carriage 1 is not moved to X%s right after being selected" % X_T1,
        )
        # Whichever carriage was active before must be active after.
        last = [l for l in lines if l.startswith("SET_DUAL_CARRIAGE")][-1]
        expect(
            last == "SET_DUAL_CARRIAGE CARRIAGE=%d" % active,
            "with carriage_1=%s the macro leaves %r selected, not carriage %d"
            % (mode, last, active),
        )

    # Copy and mirror move the carriages as a pair, and PARK_TOOLHEAD is a
    # no-op in them -- single mode has to be restored first.
    for mode in ("COPY", "MIRROR"):
        lines = render(
            macros, "_MAINTENANCE_CENTER_TOOLHEADS", printer_state(idex_mode=mode),
            params=params,
        )
        expect(
            index_of(lines, r"^_IDEX_SINGLE\b") < index_of(lines, r"^PARK_TOOLHEAD\b"),
            "%s mode is not reset to single mode before parking" % mode,
        )

    return failures


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT_CFG
    if not os.path.isfile(path):
        sys.stderr.write(
            "FAIL: %s does not exist -- if this is a built checkout, the "
            "patcher did not ship maintenance.cfg\n" % path
        )
        return 1
    try:
        failures = check(path)
    except jinja2.TemplateError as exc:
        # What Klippy reports as a config error at startup.
        sys.stderr.write("FAIL: maintenance.cfg does not render: %s\n" % exc)
        return 1
    except AssertionError as exc:
        sys.stderr.write("FAIL: %s\n" % exc)
        return 1
    if failures:
        for message in failures:
            sys.stderr.write("FAIL: %s\n" % message)
        return 1
    print("ok: MAINTENANCE_MODE renders, and parks at Z%s Y%s X%s/%s"
          % (Z_MID, Y_FRONT, X_T0, X_T1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
