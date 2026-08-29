#!/usr/bin/env python3
"""Render the service macros the way Klippy does, and check what they emit.

A gcode_macro is a Jinja2 template that Klippy renders in full *before* its
first line executes. A typo in it is not a runtime warning -- it is a config
error at startup, or an exception mid-move on a machine with the nozzles down.
Nothing else in this repo can catch that, so this renders the two macros with a
stand-in for the 600's printer object and checks the coordinates they produce.

Emulates Klippy's gcode_macro.py: the same Jinja delimiters ('{%'/'%}' for
blocks, '{'/'}' for expressions), macro variables exposed as bare names, and
`params` from the call site. Covers MAINTENANCE_MODE, MAINTENANCE_END,
NOZZLE_CHANGE, NOZZLE_CHANGE_END, their two helpers and the timeout's
delayed_gcode.

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
PRINTABLE_Y_MAX = 600.0
Y_BACK = PRINTABLE_Y_MAX - 15   # 585.0
SAFE_DISTANCE = 60.0
SPACING = SAFE_DISTANCE + 10
X_T0 = BED_CENTER_X - SPACING / 2   # 265.0
X_T1 = BED_CENTER_X + SPACING / 2   # 335.0
PARK_T0 = -73.0
PARK_T1 = 673.0

# Nozzle change: 200mm in from the carriage's own parking position, at 300C.
NOZZLE_TEMP = 300
NOZZLE_DISTANCE = 200
NOZZLE_X_T0 = PARK_T0 + NOZZLE_DISTANCE   # 127.0
NOZZLE_X_T1 = PARK_T1 - NOZZLE_DISTANCE   # 473.0
NOZZLE_TIMEOUT = 900

PUBLIC_MACROS = {
    "MAINTENANCE_MODE", "MAINTENANCE_END", "NOZZLE_CHANGE", "NOZZLE_CHANGE_END",
}
INTERNAL_MACROS = {
    "_MAINTENANCE_APPROACH", "_MAINTENANCE_POSITION_TOOLHEADS",
    "_NOZZLE_CHANGE_TIMEOUT",
}


class MacroError(Exception):
    """What action_raise_error() raises, as Klippy does."""


def printer_state(idex_mode="INACTIVE", homed="xyz", printing="standby",
                  hotend_temp=25.0, nozzle_change_tool=-1, filament=None):
    """A stand-in for Klippy's `printer` object, with the 600's config.

    idex_mode is [dual_carriage].carriage_1 as Klippy reports it: INACTIVE when
    T0 is the active carriage, PRIMARY when T1 is, COPY or MIRROR in the
    duplication modes. `filament` names the toolheads whose filament sensor
    reports filament, e.g. [0].
    """
    state = {
        "toolhead": {
            "axis_minimum": {"x": -75.0, "y": Y_MIN, "z": -5.0},
            "axis_maximum": {"x": 600.0, "y": 665.0, "z": Z_MAX},
            "homed_axes": homed,
            "max_accel": 8000,
        },
        "dual_carriage": {"carriage_0": "PRIMARY", "carriage_1": idex_mode},
        "print_stats": {"state": printing},
        "extruder": {"temperature": hotend_temp, "target": 0.0},
        "extruder1": {"temperature": hotend_temp, "target": 0.0},
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
            "printable_y_max": PRINTABLE_Y_MAX,
            "printable_y_min": 0.0,
            "default_toolhead": 0,
        },
        "gcode_macro T0": {"parking_position": PARK_T0, "active": True},
        "gcode_macro T1": {"parking_position": PARK_T1, "active": False},
        # Set by NOZZLE_CHANGE, read by NOZZLE_CHANGE_END and the timeout.
        "gcode_macro NOZZLE_CHANGE": {"tool": nozzle_change_tool},
        # MAINTENANCE_MODE owns the shared defaults the approach helper reads.
        "gcode_macro MAINTENANCE_MODE": {
            "z_height": "auto", "y_position": "auto", "front_margin": 5,
        },
    }
    for tool in filament or []:
        state["filament_switch_sensor toolhead_filament_sensor_t%d" % tool] = {
            "filament_detected": True
        }
    return state


def load_macros(path):
    """{name: (variables, gcode_template)} for every macro in a config file.

    delayed_gcode sections are loaded too, under their own name -- the nozzle
    change's dead man's switch is one, and it has to render as well.
    """
    parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=("#", ";"))
    with open(path, "r") as handle:
        parser.read_file(handle)
    macros = {}
    for section in parser.sections():
        for prefix in ("gcode_macro ", "delayed_gcode "):
            if not section.startswith(prefix):
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
            macros[section[len(prefix):]] = (variables, parser.get(section, "gcode"))
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


def number_in(line, pattern):
    return float(re.search(pattern, line).group(1))


def refuses(macros, name, printer, params=None):
    """True if the macro aborts with action_raise_error rather than emitting."""
    try:
        render(macros, name, printer, params)
    except MacroError:
        return True
    return False


def check(path):
    macros = load_macros(path)
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    missing = (PUBLIC_MACROS | INTERNAL_MACROS) - set(macros)
    expect(not missing, "not defined: %s" % ", ".join(sorted(missing)))
    if missing:
        return failures

    # Mainsail lists macros whose name has no leading underscore. The four entry
    # points must be visible and nothing else may be.
    expect(
        {n for n in macros if not n.startswith("_")} == PUBLIC_MACROS,
        "the set of macros Mainsail would show is not %s" % sorted(PUBLIC_MACROS),
    )

    check_maintenance_mode(macros, expect)
    check_approach(macros, expect)
    check_position_toolheads(macros, expect)
    check_maintenance_end(macros, expect)
    check_nozzle_change(macros, expect)
    check_nozzle_change_end(macros, expect)
    return failures


def check_maintenance_mode(macros, expect):
    lines = render(macros, "MAINTENANCE_MODE", printer_state())

    i_home = index_of(lines, r"^MAYBE_HOME\b")
    i_approach = index_of(lines, r"^_MAINTENANCE_APPROACH\b")
    i_heads = index_of(lines, r"^_MAINTENANCE_POSITION_TOOLHEADS\b")
    expect(i_home < i_approach < i_heads,
           "order must be homing, then bed and gantry, then the toolheads")

    call = lines[i_heads]
    x0 = number_in(call, r"X0=([-\d.]+)")
    x1 = number_in(call, r"X1=([-\d.]+)")
    expect(abs(x0 - X_T0) < 1e-6, "T0 goes to X%s, expected %s" % (x0, X_T0))
    expect(abs(x1 - X_T1) < 1e-6, "T1 goes to X%s, expected %s" % (x1, X_T1))
    expect(abs((x0 + x1) / 2 - BED_CENTER_X) < 1e-6,
           "the toolheads straddle X%s, not the bed centre X%s"
           % ((x0 + x1) / 2, BED_CENTER_X))
    expect(x1 - x0 >= SAFE_DISTANCE,
           "the toolheads end %smm apart, closer than safe_distance %s -- Klipper "
           "would abort the move" % (x1 - x0, SAFE_DISTANCE))

    # An explicit spacing is honoured, but never below safe_distance.
    call = lines_call(macros, "MAINTENANCE_MODE", {"SPACING": "200"})
    expect(abs(number_in(call, r"X1=([-\d.]+)") - number_in(call, r"X0=([-\d.]+)") - 200) < 1e-6,
           "an explicit SPACING was not used (%s)" % call)
    call = lines_call(macros, "MAINTENANCE_MODE", {"SPACING": "5"})
    gap = number_in(call, r"X1=([-\d.]+)") - number_in(call, r"X0=([-\d.]+)")
    expect(gap >= SAFE_DISTANCE,
           "SPACING=5 produced %smm between the carriages, below safe_distance" % gap)

    # Z and Y are passed straight through to the approach helper.
    lines = render(macros, "MAINTENANCE_MODE", printer_state(),
                   params={"Z": "100", "Y": "250"})
    approach = lines[index_of(lines, r"^_MAINTENANCE_APPROACH")]
    expect("Z=100" in approach and "Y=250" in approach,
           "explicit Z/Y did not reach the approach helper (%s)" % approach)

    for state in ("printing", "paused"):
        expect(refuses(macros, "MAINTENANCE_MODE", printer_state(printing=state)),
               "MAINTENANCE_MODE did not refuse to run while %s" % state)


def lines_call(macros, name, params):
    """The _MAINTENANCE_POSITION_TOOLHEADS call a macro emits for these params."""
    lines = render(macros, name, printer_state(), params=params)
    return lines[index_of(lines, r"^_MAINTENANCE_POSITION_TOOLHEADS")]


def check_approach(macros, expect):
    lines = render(macros, "_MAINTENANCE_APPROACH", printer_state(),
                   params={"Z": "auto", "Y": "auto"})
    i_z = index_of(lines, r"^G0 Z")
    i_y = index_of(lines, r"^G0 Y")
    expect(index_of(lines, r"^G90\b") < i_z,
           "the moves must be made in absolute positioning")
    expect(i_z < i_y, "the bed must move before the gantry")
    z = number_in(lines[i_z], r"G0 Z([-\d.]+)")
    y = number_in(lines[i_y], r"G0 Y([-\d.]+)")
    expect(abs(z - Z_MID) < 1e-6,
           "bed goes to Z%s, expected mid travel %s" % (z, Z_MID))
    expect(abs(y - Y_FRONT) < 1e-6,
           "gantry goes to Y%s, expected the front at %s" % (y, Y_FRONT))

    # Targets outside the machine are clamped, not commanded.
    lines = render(macros, "_MAINTENANCE_APPROACH", printer_state(),
                   params={"Z": "9999", "Y": "9999"})
    expect(number_in(lines[index_of(lines, r"^G0 Z")], r"G0 Z([-\d.]+)") <= Z_MAX,
           "Z=9999 was not clamped to the Z limit")
    expect(number_in(lines[index_of(lines, r"^G0 Y")], r"G0 Y([-\d.]+)") <= 665.0,
           "Y=9999 was not clamped to the Y limit")


def check_position_toolheads(macros, expect):
    params = {"X0": str(X_T0), "X1": str(X_T1)}

    for mode, active in (("INACTIVE", 0), ("PRIMARY", 1)):
        lines = render(macros, "_MAINTENANCE_POSITION_TOOLHEADS",
                       printer_state(idex_mode=mode), params=params)
        expect(index_of(lines, r"^PARK_TOOLHEAD\b") < index_of(lines, r"^SET_DUAL_CARRIAGE"),
               "both carriages must be parked before moving to the targets")
        i0 = index_of(lines, r"^SET_DUAL_CARRIAGE CARRIAGE=0\b")
        expect(lines[i0 + 1].startswith("G0 X%s" % X_T0),
               "carriage 0 is not moved to X%s right after being selected" % X_T0)
        i1 = index_of(lines, r"^SET_DUAL_CARRIAGE CARRIAGE=1\b")
        expect(lines[i1 + 1].startswith("G0 X%s" % X_T1),
               "carriage 1 is not moved to X%s right after being selected" % X_T1)
        last = [l for l in lines if l.startswith("SET_DUAL_CARRIAGE")][-1]
        expect(last == "SET_DUAL_CARRIAGE CARRIAGE=%d" % active,
               "with carriage_1=%s the macro leaves %r selected, not carriage %d"
               % (mode, last, active))

    # Copy and mirror move the carriages as a pair, and PARK_TOOLHEAD is a
    # no-op in them -- single mode has to be restored first.
    for mode in ("COPY", "MIRROR"):
        lines = render(macros, "_MAINTENANCE_POSITION_TOOLHEADS",
                       printer_state(idex_mode=mode), params=params)
        expect(index_of(lines, r"^_IDEX_SINGLE\b") < index_of(lines, r"^PARK_TOOLHEAD\b"),
               "%s mode is not reset to single mode before parking" % mode)


def check_maintenance_end(macros, expect):
    lines = render(macros, "MAINTENANCE_END", printer_state())

    i_home = index_of(lines, r"^G28\b")
    i_z = index_of(lines, r"^G0 Z")
    i_y = index_of(lines, r"^G0 Y")
    i_park = index_of(lines, r"^_MAINTENANCE_POSITION_TOOLHEADS\b")
    expect(i_home < i_z < i_y < i_park,
           "order must be re-home, bed, gantry, then park the toolheads")
    expect(not [l for l in lines if l.startswith("MAYBE_HOME")],
           "MAINTENANCE_END must re-home unconditionally -- hands were on the "
           "machine, so the kinematic position is fiction")

    y = number_in(lines[i_y], r"G0 Y([-\d.]+)")
    expect(abs(y - Y_BACK) < 1e-6,
           "gantry goes to Y%s, expected the back at %s" % (y, Y_BACK))

    call = lines[i_park]
    x0 = number_in(call, r"X0=([-\d.]+)")
    x1 = number_in(call, r"X1=([-\d.]+)")
    expect(abs(x0 - PARK_T0) < 1e-6 and abs(x1 - PARK_T1) < 1e-6,
           "the toolheads are left at X%s/X%s, not at their parking positions "
           "%s/%s" % (x0, x1, PARK_T0, PARK_T1))

    # A hot nozzle oozes into the Z reference G28 takes right above the bed.
    expect(refuses(macros, "MAINTENANCE_END", printer_state(hotend_temp=214.0)),
           "MAINTENANCE_END re-homed with a 214C nozzle")
    expect(not refuses(macros, "MAINTENANCE_END", printer_state(hotend_temp=214.0),
                       params={"TEMP_LIMIT": "999"}),
           "TEMP_LIMIT= did not override the hotend temperature gate")
    expect(not refuses(macros, "MAINTENANCE_END", printer_state(hotend_temp=59.0)),
           "MAINTENANCE_END refused at 59C, below its own 60C limit")

    for state in ("printing", "paused"):
        expect(refuses(macros, "MAINTENANCE_END", printer_state(printing=state)),
               "MAINTENANCE_END did not refuse to run while %s" % state)


def check_nozzle_change(macros, expect):
    # T0 is the active carriage when carriage_1 is INACTIVE, so it is the one
    # presented when no T is given.
    for mode, tool, x0, x1, heater in (
        ("INACTIVE", 0, NOZZLE_X_T0, PARK_T1, "extruder"),
        ("PRIMARY", 1, PARK_T0, NOZZLE_X_T1, "extruder1"),
    ):
        lines = render(macros, "NOZZLE_CHANGE", printer_state(idex_mode=mode))
        text = "\n".join(lines)

        i_home = index_of(lines, r"^MAYBE_HOME\b")
        i_heat = index_of(lines, r"^SET_HEATER_TEMPERATURE\b")
        i_heads = index_of(lines, r"^_MAINTENANCE_POSITION_TOOLHEADS\b")
        i_wait = index_of(lines, r"^TEMPERATURE_WAIT\b")
        expect(i_home < i_heat,
               "the machine must home before the nozzle starts heating -- G28 Z "
               "takes its reference right above the bed")
        expect(i_heat < i_heads < i_wait,
               "heating must start before the moves and be waited for after them")

        expect("HEATER=%s " % heater in lines[i_heat],
               "T%d heats %r, expected %s" % (tool, lines[i_heat], heater))
        expect(abs(number_in(lines[i_heat], r"TARGET=([\d.]+)") - NOZZLE_TEMP) < 1e-6,
               "T%d is heated to %r, expected %sC" % (tool, lines[i_heat], NOZZLE_TEMP))
        expect("SENSOR=%s " % heater in lines[i_wait],
               "the wait watches %r, expected %s" % (lines[i_wait], heater))

        call = lines[i_heads]
        got0 = number_in(call, r"X0=([-\d.]+)")
        got1 = number_in(call, r"X1=([-\d.]+)")
        expect(abs(got0 - x0) < 1e-6 and abs(got1 - x1) < 1e-6,
               "presenting T%d moves to X0=%s X1=%s, expected %s/%s -- %smm in "
               "from the parking position, the other carriage left parked"
               % (tool, got0, got1, x0, x1, NOZZLE_DISTANCE))

        # The dead man's switch has to be armed, and know which heater it owns.
        expect(re.search(r"SET_GCODE_VARIABLE MACRO=NOZZLE_CHANGE VARIABLE=tool VALUE=%d" % tool, text),
               "the timeout was not told which toolhead it owns")
        arm = lines[index_of(lines, r"^UPDATE_DELAYED_GCODE ID=_NOZZLE_CHANGE_TIMEOUT")]
        expect(abs(number_in(arm, r"DURATION=([\d.]+)") - NOZZLE_TIMEOUT) < 1e-6,
               "the timeout is armed as %r, expected %ss" % (arm, NOZZLE_TIMEOUT))

    # T= and TEMP= override the defaults.
    lines = render(macros, "NOZZLE_CHANGE", printer_state(), params={"T": "1", "TEMP": "260"})
    heat = lines[index_of(lines, r"^SET_HEATER_TEMPERATURE\b")]
    expect("HEATER=extruder1 " in heat and "TARGET=260" in heat,
           "T=1 TEMP=260 produced %r" % heat)
    call = lines[index_of(lines, r"^_MAINTENANCE_POSITION_TOOLHEADS")]
    expect(abs(number_in(call, r"X1=([-\d.]+)") - NOZZLE_X_T1) < 1e-6,
           "T=1 did not present the right carriage (%s)" % call)

    # DISTANCE= is clamped to what the carriage can reach.
    call = lines_call(macros, "NOZZLE_CHANGE", {"T": "0", "DISTANCE": "9999"})
    expect(number_in(call, r"X0=([-\d.]+)") <= 600.0,
           "DISTANCE=9999 was not clamped to the X limit (%s)" % call)

    # Filament still loaded is worth saying, not worth refusing over.
    lines = render(macros, "NOZZLE_CHANGE", printer_state(filament=[0]))
    expect([l for l in lines if "WARNING" in l and "filament" in l.lower()],
           "a loaded filament sensor produced no warning")
    lines = render(macros, "NOZZLE_CHANGE", printer_state())
    expect(not [l for l in lines if "WARNING" in l],
           "the filament warning fires with no sensor present")

    expect(refuses(macros, "NOZZLE_CHANGE", printer_state(), params={"T": "2"}),
           "NOZZLE_CHANGE accepted T=2")
    for state in ("printing", "paused"):
        expect(refuses(macros, "NOZZLE_CHANGE", printer_state(printing=state)),
               "NOZZLE_CHANGE did not refuse to run while %s" % state)


def check_nozzle_change_end(macros, expect):
    for name in ("NOZZLE_CHANGE_END", "_NOZZLE_CHANGE_TIMEOUT"):
        for tool, heater in ((0, "extruder"), (1, "extruder1")):
            lines = render(macros, name, printer_state(nozzle_change_tool=tool))
            text = "\n".join(lines)
            off = lines[index_of(lines, r"^SET_HEATER_TEMPERATURE\b")]
            expect("HEATER=%s " % heater in off and "TARGET=0" in off,
                   "%s with tool=%d emits %r" % (name, tool, off))
            expect("VARIABLE=tool VALUE=-1" in text,
                   "%s does not disarm itself for tool=%d" % (name, tool))

        # Nothing to turn off when no nozzle change is running -- and in
        # particular, no heater command aimed at a guessed toolhead.
        lines = render(macros, name, printer_state(nozzle_change_tool=-1))
        expect(not [l for l in lines if l.startswith("SET_HEATER_TEMPERATURE")],
               "%s touched a heater with no nozzle change active" % name)

    # Ending the change cancels the timeout rather than leaving it armed.
    lines = render(macros, "NOZZLE_CHANGE_END", printer_state(nozzle_change_tool=0))
    cancel = lines[index_of(lines, r"^UPDATE_DELAYED_GCODE ID=_NOZZLE_CHANGE_TIMEOUT")]
    expect("DURATION=0" in cancel, "the timeout is not cancelled (%s)" % cancel)


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
    print("ok: service macros render. MAINTENANCE_MODE parks at Z%s Y%s X%s/%s, "
          "NOZZLE_CHANGE presents T0 at X%s / T1 at X%s at %sC"
          % (Z_MID, Y_FRONT, X_T0, X_T1, NOZZLE_X_T0, NOZZLE_X_T1, NOZZLE_TEMP))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
