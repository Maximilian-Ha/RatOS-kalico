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

# Lubrication: three stations per axis, derived from the limits with a margin.
LUBE_MARGIN = 10.0
LUBE_SWEEPS = 2
LUBE_DELAY = 3
LUBE_SPACING = SAFE_DISTANCE + 10          # the IDEX pair moves as a block
LUBE_STATIONS = {
    # Z is nozzle-to-bed distance: near = bed at the TOP. Measured from 0
    # rather than axis_minimum (-5), which is probe territory.
    "Z": [0.0 + LUBE_MARGIN, None, Z_MAX - LUBE_MARGIN],
    # Y stops at the printable area, not the mechanical limit: the VAOC camera
    # sits in the last stretch of travel.
    "Y": [Y_MIN + LUBE_MARGIN, None, PRINTABLE_Y_MAX - LUBE_MARGIN],
    # X stations are the LEFT carriage's position; the right one follows a
    # spacing behind, so far is the right limit minus margin minus spacing.
    "X": [-75.0 + LUBE_MARGIN, None, 675.0 - LUBE_MARGIN - LUBE_SPACING],
}
for _stations in LUBE_STATIONS.values():
    _stations[1] = (_stations[0] + _stations[2]) / 2

# The lubrication record counts PRINT hours, not calendar time.
LUBE_SAVE_AFTER = 360      # seconds of print time that pile up before a disk write
LUBE_SAMPLE_INTERVAL = 60  # seconds between samples

# The bed is one plate over four heaters: heater_bed is Klipper's own, the rest
# are [heater_generic] sections.
BED_ZONES = ["heater_bed", "BED_VR", "BED_HL", "BED_HR"]
BED_TEMP = 80
BED_COOL_BELOW = 40

# Blowing the plate off: T0's part fan, at the printable area's full extent.
BLOW_Z = 25.0
BLOW_SPACING = 50.0
BLOW_FAN = "part_fan_t0"
BLOW_LINES = int(PRINTABLE_Y_MAX // BLOW_SPACING) + 1   # 0, 50, .. 600

PUBLIC_MACROS = {
    "MAINTENANCE_MODE", "MAINTENANCE_END", "NOZZLE_CHANGE", "NOZZLE_CHANGE_END",
    "LUBE_X", "LUBE_Y", "LUBE_Z", "LUBE_NEXT", "LUBE_ABORT",
    "LUBE_STATUS", "LUBE_MARK",
    "PID_TUNE_BEDS", "PID_TUNE_BED", "BLOW_BED",
}
INTERNAL_MACROS = {
    "_MAINTENANCE_APPROACH", "_MAINTENANCE_POSITION_TOOLHEADS",
    "_NOZZLE_CHANGE_TIMEOUT",
    "_LUBE_START", "_LUBE_ADVANCE", "_LUBE_FINISH", "_LUBE_MOVE",
    "_LUBE_CLEAR", "_LUBE_PROMPT",
    "_LUBE_RECORD", "_LUBE_HOURS", "_LUBE_HOURS_TICK",
    "_PID_TUNE_BED_WARN", "_PID_TUNE_BED_ZONE",
}


class MacroError(Exception):
    """What action_raise_error() raises, as Klippy does."""


def printer_state(idex_mode="INACTIVE", homed="xyz", printing="standby",
                  hotend_temp=25.0, nozzle_change_tool=-1, filament=None,
                  lube_axis="", lube_stations=None, lube_travel=None,
                  saved=None, print_duration=0.0, unsaved=0.0,
                  last_duration=0.0, bed_zones=None, bed_zones_cfg=False,
                  part_fans=(BLOW_FAN,)):
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
        "print_stats": {"state": printing, "print_duration": print_duration},
        # What SAVE_VARIABLE persists across restarts. The configurator writes
        # [save_variables] into printer.cfg, so this is always present.
        "save_variables": {"variables": dict(saved or {})},
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
        "gcode_macro _LUBE_HOURS": {
            "last_duration": last_duration,
            "unsaved": unsaved,
            "interval": LUBE_SAMPLE_INTERVAL,
            "save_after": LUBE_SAVE_AFTER,
        },
        "gcode_macro LUBE_STATUS": {"interval_hours": 100, "show_dates": True},
        "gcode_macro PID_TUNE_BEDS": {
            "zones": ",".join(BED_ZONES),
            "temp": BED_TEMP,
            "cool_below": BED_COOL_BELOW,
        },
        # _LUBE_START carries the state of a lubrication run across the
        # separate commands that make one up.
        "gcode_macro _LUBE_START": {
            "axis": lube_axis,
            "stations": list(lube_stations if lube_stations is not None else []),
            "travel": list(lube_travel if lube_travel is not None else []),
            "total": 3,
            "margin": LUBE_MARGIN,
            "sweeps": LUBE_SWEEPS,
            "move_delay": LUBE_DELAY,
        },
    }
    zones = BED_ZONES if bed_zones is None else bed_zones
    for zone in zones:
        if zone == "heater_bed":
            state["heater_bed"] = {"temperature": 25.0, "target": 0.0}
        else:
            state["heater_generic %s" % zone] = {"temperature": 25.0, "target": 0.0}
    for name in part_fans:
        state["fan_generic %s" % name] = {"speed": 0.0}
    if bed_zones_cfg:
        # The unrelated bed-zones.cfg, which mirrors heater_bed onto the rest.
        state["gcode_macro _BED_ZONES"] = {"mode": "all"}
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


def render(macros, name, printer, params=None, variables=None):
    """Render one macro, returning its emitted g-code lines.

    `variables` overrides the macro's own variable_ defaults, the way
    SET_GCODE_VARIABLE does at runtime.
    """
    macro_variables, template_text = macros[name]
    variables = dict(macro_variables, **(variables or {}))
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


def refuses(macros, name, printer, params=None, variables=None):
    """True if the macro aborts with action_raise_error rather than emitting."""
    try:
        render(macros, name, printer, params, variables)
    except MacroError:
        return True
    return False


def refusal_message(macros, name, printer, params=None, variables=None):
    """The message a macro aborts with, or None if it did not abort."""
    try:
        render(macros, name, printer, params, variables)
    except MacroError as exc:
        return str(exc)
    return None


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
    check_lube_stations(macros, expect)
    check_lube_advance(macros, expect)
    check_lube_move(macros, expect)
    check_lube_finish(macros, expect)
    check_lube_prompt(macros, expect)
    check_lube_hours(macros, expect)
    check_lube_record(macros, expect)
    check_lube_status(macros, expect)
    check_bed_pid_all(macros, expect)
    check_bed_pid_zone(macros, expect)
    check_bed_pid_single(macros, expect)
    check_blow_bed(macros, expect)
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


def parse_list(line):
    """The list literal out of a SET_GCODE_VARIABLE ... VALUE="[...]" line."""
    return ast.literal_eval(re.search(r'VALUE="(\[[^"]*\])"', line).group(1))


def check_lube_stations(macros, expect):
    """Every station is derived from the axis limits, not from a 600 number."""
    for axis, expected in sorted(LUBE_STATIONS.items()):
        lines = render(macros, "_LUBE_START", printer_state(), params={"AXIS": axis})
        text = "\n".join(lines)

        i_home = index_of(lines, r"^MAYBE_HOME\b")
        i_state = index_of(lines, r"VARIABLE=axis\b")
        i_advance = index_of(lines, r"^_LUBE_ADVANCE\b")
        expect(i_home < i_state < i_advance,
               "%s: must home, then record the run, then move" % axis)

        stations = parse_list(lines[index_of(lines, r"VARIABLE=stations")])
        expect([round(v, 3) for v in stations] == [round(v, 3) for v in expected],
               "LUBE_%s stops at %s, expected %s" % (axis, stations, expected))
        travel = parse_list(lines[index_of(lines, r"VARIABLE=travel")])
        expect([round(v, 3) for v in travel] == [round(expected[0], 3), round(expected[2], 3)],
               "LUBE_%s sweeps over %s, expected the two ends %s"
               % (axis, travel, [expected[0], expected[2]]))

        # Whatever is not being greased gets out of the way first.
        if axis == "Z":
            expect(index_of(lines, r"^G0 Y") < i_advance,
                   "Z: the gantry is not moved out of the way before the run")
            expect("_MAINTENANCE_POSITION_TOOLHEADS" in text,
                   "Z: the carriages are not parked before the run")
        else:
            expect("_MAINTENANCE_APPROACH" in text,
                   "%s: the bed is not lowered for access before the run" % axis)
        if axis == "X":
            expect("_MAINTENANCE_POSITION_TOOLHEADS" not in text,
                   "X: the carriages are parked in the preparation of a run "
                   "whose stations move them anyway")

    # Guards.
    expect(refuses(macros, "_LUBE_START", printer_state(), params={"AXIS": "A"}),
           "_LUBE_START accepted AXIS=A")
    expect(refuses(macros, "_LUBE_START", printer_state(), params={}),
           "_LUBE_START accepted a missing AXIS")
    for state in ("printing", "paused"):
        expect(refuses(macros, "_LUBE_START", printer_state(printing=state),
                       params={"AXIS": "Z"}),
               "a lubrication run started while %s" % state)
    # A second run while one is open would overwrite the first one's state.
    expect(refuses(macros, "_LUBE_START", printer_state(), params={"AXIS": "Y"},
                   variables={"axis": "Z"}),
           "_LUBE_START started a Y run on top of an open Z run")


def check_lube_advance(macros, expect):
    stations = LUBE_STATIONS["Z"]
    lines = render(macros, "_LUBE_ADVANCE",
                   printer_state(lube_axis="Z", lube_stations=stations))

    # Warn, wait, then move: hands have to be able to come out.
    i_warn = index_of(lines, r"^RATOS_ECHO.*Station 1 of 3")
    i_dwell = index_of(lines, r"^G4 P")
    i_move = index_of(lines, r"^_LUBE_MOVE\b")
    expect(i_warn < i_dwell < i_move,
           "the warning and the dwell must both come before the move")
    dwell = number_in(lines[i_dwell], r"G4 P([\d.]+)")
    expect(abs(dwell - LUBE_DELAY * 1000) < 1e-6,
           "the dwell is %sms, expected %ss" % (dwell, LUBE_DELAY))

    move = lines[i_move]
    expect("AXIS=Z" in move and abs(number_in(move, r"POS=([-\d.]+)") - stations[0]) < 1e-6,
           "the first station move is %r, expected Z to %s" % (move, stations[0]))

    # The station just visited is consumed, so LUBE_NEXT cannot repeat it.
    rest = parse_list(lines[index_of(lines, r"VARIABLE=stations")])
    expect([round(v, 3) for v in rest] == [round(v, 3) for v in stations[1:]],
           "after station 1 the remaining stations are %s, expected %s" % (rest, stations[1:]))
    prompt = lines[index_of(lines, r"^_LUBE_PROMPT\b")]
    expect("STATION=1" in prompt and "TOTAL=3" in prompt,
           "the dialog is not told which station it is showing (%s)" % prompt)

    # Mid-run and end-of-run.
    lines = render(macros, "_LUBE_ADVANCE",
                   printer_state(lube_axis="Z", lube_stations=stations[2:]))
    expect([l for l in lines if "Station 3 of 3" in l],
           "the last station is not numbered 3 of 3")
    lines = render(macros, "_LUBE_ADVANCE",
                   printer_state(lube_axis="Z", lube_stations=[]))
    expect(lines == ["_LUBE_FINISH"],
           "with no stations left the run must finish, got %s" % lines)
    expect(refuses(macros, "_LUBE_ADVANCE", printer_state()),
           "_LUBE_ADVANCE ran with no run in progress")

    # LUBE_NEXT is what the dialog's Continue button sends.
    lines = render(macros, "LUBE_NEXT", printer_state(lube_axis="Z", lube_stations=stations))
    expect(lines == ["_LUBE_ADVANCE"], "LUBE_NEXT does not advance the run (%s)" % lines)
    lines = render(macros, "LUBE_NEXT", printer_state())
    expect(not [l for l in lines if l.startswith("_LUBE_ADVANCE")],
           "LUBE_NEXT advanced a run that does not exist")

    # Aborting forgets the run and takes the dialog down.
    lines = render(macros, "LUBE_ABORT", printer_state(lube_axis="Z", lube_stations=stations))
    expect("_LUBE_CLEAR" in lines, "LUBE_ABORT does not clear the run state")
    expect([l for l in lines if "prompt_end" in l], "LUBE_ABORT leaves the dialog up")
    cleared = render(macros, "_LUBE_CLEAR", printer_state(lube_axis="Z"))
    expect([l for l in cleared if 'VARIABLE=axis VALUE="\'\'"' in l],
           "_LUBE_CLEAR does not reset the axis (%s)" % cleared)


def check_lube_move(macros, expect):
    lines = render(macros, "_LUBE_MOVE", printer_state(), params={"AXIS": "Z", "POS": "645"})
    expect(abs(number_in(lines[index_of(lines, r"^G0 Z")], r"G0 Z([-\d.]+)") - 645) < 1e-6,
           "a Z move did not go to Z645 (%s)" % lines)
    lines = render(macros, "_LUBE_MOVE", printer_state(), params={"AXIS": "Y", "POS": "590"})
    expect(abs(number_in(lines[index_of(lines, r"^G0 Y")], r"G0 Y([-\d.]+)") - 590) < 1e-6,
           "a Y move did not go to Y590 (%s)" % lines)

    # X moves the pair, and the pair must stay a safe_distance apart at both
    # ends of the travel -- including where clamping bites.
    for pos in LUBE_STATIONS["X"]:
        lines = render(macros, "_LUBE_MOVE", printer_state(),
                       params={"AXIS": "X", "POS": str(pos)})
        call = lines[index_of(lines, r"^_MAINTENANCE_POSITION_TOOLHEADS")]
        x0 = number_in(call, r"X0=([-\d.]+)")
        x1 = number_in(call, r"X1=([-\d.]+)")
        expect(x1 - x0 >= SAFE_DISTANCE,
               "at X station %s the carriages end %smm apart, below safe_distance"
               % (pos, x1 - x0))
        expect(-75.0 <= x0 <= 600.0 and 0.0 <= x1 <= 675.0,
               "at X station %s a carriage is driven outside its limits (%s/%s)"
               % (pos, x0, x1))


def check_lube_finish(macros, expect):
    near, _mid, far = LUBE_STATIONS["Z"]
    lines = render(macros, "_LUBE_FINISH",
                   printer_state(lube_axis="Z", lube_travel=[near, far]))
    moves = [number_in(l, r"POS=([-\d.]+)") for l in lines if l.startswith("_LUBE_MOVE")]
    expect(moves == [far, near] * LUBE_SWEEPS,
           "the closing sweeps are %s, expected %s full passes ending at the "
           "start (%s)" % (moves, LUBE_SWEEPS, [far, near] * LUBE_SWEEPS))
    expect(index_of(lines, r"^G4 P") < index_of(lines, r"^_LUBE_MOVE"),
           "the sweeps start without a warning dwell")
    expect("_LUBE_CLEAR" in lines, "the run state survives the end of the run")
    expect([l for l in lines if "prompt_end" in l], "the dialog is left up at the end")


def check_lube_prompt(macros, expect):
    lines = render(macros, "_LUBE_PROMPT", printer_state(),
                   params={"AXIS": "Z", "STATION": "1", "TOTAL": "3", "POS": "10.0"})
    text = "\n".join(lines)
    for needed in ("action:prompt_begin", "action:prompt_text",
                   "action:prompt_footer_button", "action:prompt_show"):
        expect(needed in text, "the dialog is missing %s" % needed)
    expect("Continue|LUBE_NEXT" in text,
           "the dialog's Continue button does not send LUBE_NEXT")
    expect("Abort|LUBE_ABORT" in text,
           "the dialog has no way out that ends the run")
    expect(index_of(lines, r"prompt_end") < index_of(lines, r"prompt_begin"),
           "a previous dialog is not closed before the next one opens")
    # Z is the axis whose direction is easy to get backwards, so the dialog
    # says where the bed is, not just a number.
    expect("bed at the top" in text,
           "the Z dialog does not say where the bed physically is (%s)" % text)
    lines = render(macros, "_LUBE_PROMPT", printer_state(),
                   params={"AXIS": "Z", "STATION": "3", "TOTAL": "3", "POS": "645.0"})
    expect("bed at the bottom" in "\n".join(lines),
           "the last Z station is not described as the bed at the bottom")


def variable_set(lines, name):
    """The value a SET_GCODE_VARIABLE line assigns."""
    line = lines[index_of(lines, r"VARIABLE=%s\b" % name)]
    return float(re.search(r"VALUE=([-\d.]+)", line).group(1))


def saved_value(lines, name):
    """The value a SAVE_VARIABLE line writes."""
    line = lines[index_of(lines, r"SAVE_VARIABLE VARIABLE=%s\b" % name)]
    return float(re.search(r"VALUE=([-\d.]+)", line).group(1))


def check_lube_hours(macros, expect):
    """The print-hour counter: Klipper has no lifetime total, so we keep one."""
    # Mid-print sample: the delta is counted, nothing is written to disk yet.
    lines = render(macros, "_LUBE_HOURS", printer_state(printing="printing", print_duration=160.0),
                   variables={"last_duration": 100.0, "unsaved": 0.0})
    expect(abs(variable_set(lines, "last_duration") - 160.0) < 1e-6,
           "the sample point did not move to the current print_duration")
    expect(abs(variable_set(lines, "unsaved") - 60.0) < 1e-6,
           "a 60s delta was not counted (%s)" % lines)
    expect(not [l for l in lines if l.startswith("SAVE_VARIABLE")],
           "every sample writes to disk -- that is a file rewrite a minute")

    # print_duration resets when the next job starts. A smaller value is a new
    # job, not time travelling backwards.
    lines = render(macros, "_LUBE_HOURS", printer_state(printing="printing", print_duration=20.0),
                   variables={"last_duration": 500.0, "unsaved": 0.0})
    expect(abs(variable_set(lines, "unsaved") - 20.0) < 1e-6,
           "a new job's first sample was counted wrong (%s)" % lines)

    # Three reasons to actually write: asked to, printing stopped, enough piled up.
    total = 10.0
    for label, state, unsaved, params in (
        ("FLUSH=1", "printing", 60.0, {"FLUSH": "1"}),
        ("printing stopped", "complete", 60.0, {}),
        ("batch full", "printing", LUBE_SAVE_AFTER, {}),
    ):
        lines = render(macros, "_LUBE_HOURS",
                       printer_state(printing=state, print_duration=160.0,
                                     saved={"lube_print_hours": total}),
                       params=params, variables={"last_duration": 100.0, "unsaved": unsaved})
        written = saved_value(lines, "lube_print_hours")
        expected = total + (unsaved + 60.0) / 3600
        expect(abs(written - expected) < 1e-3,
               "%s wrote %s hours, expected %s" % (label, written, expected))
        expect(abs(variable_set(lines, "unsaved")) < 1e-6,
               "%s did not reset the unsaved counter" % label)

    # Nothing to write, nothing written.
    lines = render(macros, "_LUBE_HOURS", printer_state(printing="standby", print_duration=0.0),
                   variables={"last_duration": 0.0, "unsaved": 0.0})
    expect(not [l for l in lines if l.startswith("SAVE_VARIABLE")],
           "an idle sample with nothing counted still wrote to disk")

    # The tick has to re-arm itself or the counter stops after one sample.
    lines = render(macros, "_LUBE_HOURS_TICK", printer_state())
    expect("_LUBE_HOURS" in lines, "the tick does not sample")
    rearm = lines[index_of(lines, r"^UPDATE_DELAYED_GCODE ID=_LUBE_HOURS_TICK")]
    expect(abs(number_in(rearm, r"DURATION=([\d.]+)") - LUBE_SAMPLE_INTERVAL) < 1e-6,
           "the tick re-arms at %r, expected every %ss" % (rearm, LUBE_SAMPLE_INTERVAL))


def check_lube_record(macros, expect):
    lines = render(macros, "_LUBE_RECORD",
                   printer_state(saved={"lube_print_hours": 412.5}), params={"AXIS": "Z"})
    expect(abs(saved_value(lines, "lube_z_at") - 412.5) < 1e-3,
           "the greasing was recorded at the wrong hour count (%s)" % lines)
    expect([l for l in lines if "RUN_SHELL_COMMAND CMD=lube_stamp" in l and "PARAMS=z" in l],
           "no calendar date is stamped for the axis (%s)" % lines)

    # Recording by hand, for a greasing done without the guided run.
    lines = render(macros, "LUBE_MARK", printer_state(), params={"AXIS": "ALL"})
    expect(index_of(lines, r"^_LUBE_HOURS FLUSH=1") < index_of(lines, r"^_LUBE_RECORD"),
           "the counter is not flushed before the snapshot is taken, so the "
           "snapshot lags the number LUBE_STATUS shows")
    expect(len([l for l in lines if l.startswith("_LUBE_RECORD")]) == 3,
           "AXIS=ALL did not record all three axes (%s)" % lines)
    expect(refuses(macros, "LUBE_MARK", printer_state(), params={"AXIS": "Q"}),
           "LUBE_MARK accepted AXIS=Q")
    expect(refuses(macros, "LUBE_MARK", printer_state(), params={}),
           "LUBE_MARK accepted a missing AXIS")

    # A finished run records itself -- the whole point of the guided flow.
    lines = render(macros, "_LUBE_FINISH",
                   printer_state(lube_axis="Z", lube_travel=list(LUBE_STATIONS["Z"][::2])))
    i_flush = index_of(lines, r"^_LUBE_HOURS FLUSH=1")
    i_record = index_of(lines, r"^_LUBE_RECORD AXIS=Z")
    expect(index_of(lines, r"^_LUBE_MOVE") < i_flush < i_record < index_of(lines, r"^_LUBE_CLEAR"),
           "a finished run must record itself after the sweeps and before the "
           "state is cleared (%s)" % lines)


def check_lube_status(macros, expect):
    # Greased once, printed since. The unsaved seconds count too, or the
    # display lags reality by up to a batch.
    saved = {"lube_print_hours": 400.0, "lube_z_at": 375.0}
    lines = render(macros, "LUBE_STATUS", printer_state(saved=saved, unsaved=3600.0))
    text = "\n".join(lines)
    expect("401.0" in text, "the pending, unwritten hours are not counted in the "
                            "total (%s)" % text)
    expect("375.0" in text and "26.0" in text,
           "Z should read as greased at 375.0h, 26.0 print hours ago (%s)" % text)
    expect("to go" in text, "no interval verdict for an axis still inside it")

    # Past the interval it has to say so, not quietly count on.
    saved = {"lube_print_hours": 500.0, "lube_z_at": 375.0}
    text = "\n".join(render(macros, "LUBE_STATUS", printer_state(saved=saved)))
    expect("OVERDUE" in text, "125 print hours on a 100 hour interval is not "
                              "flagged as overdue (%s)" % text)

    # Never greased is a state of its own, not "0 hours ago".
    text = "\n".join(render(macros, "LUBE_STATUS", printer_state(saved={"lube_print_hours": 40.0})))
    expect(text.count("never greased") == 3,
           "an axis with no record must say so for all three axes (%s)" % text)
    expect("0.0 print hours ago" not in text,
           "a missing record was rendered as a greasing at hour zero")

    # An empty save file is the state a fresh printer is in.
    text = "\n".join(render(macros, "LUBE_STATUS", printer_state()))
    expect("Print hours on this machine: 0.0" in text,
           "a printer with no saved variables does not start at zero (%s)" % text)

    # The dates cannot come from a macro; they come back as shell output.
    expect([l for l in render(macros, "LUBE_STATUS", printer_state())
            if "RUN_SHELL_COMMAND CMD=lube_dates" in l],
           "LUBE_STATUS does not ask for the calendar dates")
    text = "\n".join(render(macros, "LUBE_STATUS", printer_state(),
                            variables={"show_dates": False}))
    expect("RUN_SHELL_COMMAND" not in text,
           "show_dates=False still runs the shell command")


def check_bed_pid_all(macros, expect):
    """All four zones in sequence, and exactly one save at the end."""
    lines = render(macros, "PID_TUNE_BEDS", printer_state())
    calls = [l for l in lines if l.startswith("_PID_TUNE_BED_ZONE")]
    expect(len(calls) == len(BED_ZONES),
           "%d zones tuned, expected %d (%s)" % (len(calls), len(BED_ZONES), calls))
    for i, (call, zone) in enumerate(zip(calls, BED_ZONES), start=1):
        expect("HEATER=%s " % zone in call + " ",
               "run %d tunes %r, expected %s" % (i, call, zone))
        expect("STEP=%d" % i in call and "TOTAL=%d" % len(BED_ZONES) in call,
               "run %d is not numbered %d of %d (%s)" % (i, i, len(BED_ZONES), call))
        expect("TEMP=%d" % BED_TEMP in call, "run %d does not carry the target (%s)" % (i, call))

    # One SAVE_CONFIG hint, at the end: Klipper accumulates the pending config
    # of all four calibrations, so one save writes them all and the printer
    # restarts once instead of four times.
    saves = [i for i, l in enumerate(lines) if l == "_CONSOLE_SAVE_CONFIG"]
    expect(len(saves) == 1,
           "%d save prompts, expected exactly one -- a save between runs "
           "restarts the printer and drops the rest" % len(saves))
    if saves:
        expect(saves[0] > lines.index(calls[-1]),
               "the save prompt comes before the last zone is tuned")

    # A machine without the generic zones tunes what it has.
    lines = render(macros, "PID_TUNE_BEDS", printer_state(bed_zones=["heater_bed"]))
    calls = [l for l in lines if l.startswith("_PID_TUNE_BED_ZONE")]
    expect(len(calls) == 1 and "HEATER=heater_bed " in calls[0] + " ",
           "a printer with only heater_bed did not tune just that (%s)" % calls)
    expect("TOTAL=1" in calls[0], "the run count was not adjusted (%s)" % calls[0])

    # No zones at all is a config error, not a silent no-op.
    expect(refuses(macros, "PID_TUNE_BEDS", printer_state(bed_zones=[])),
           "PID_TUNE_BEDS did nothing, and said nothing, with no zones present")

    for state in ("printing", "paused"):
        expect(refuses(macros, "PID_TUNE_BEDS", printer_state(printing=state)),
               "a bed PID run started while %s" % state)


def check_bed_pid_zone(macros, expect):
    """One zone: cool the whole plate first, tune, leave it off."""
    lines = render(macros, "_PID_TUNE_BED_ZONE", printer_state(),
                   params={"HEATER": "BED_VR", "TEMP": str(BED_TEMP),
                           "COOL_BELOW": str(BED_COOL_BELOW),
                           "STEP": "2", "TOTAL": "4"})

    i_cal = index_of(lines, r"^PID_CALIBRATE\b")
    offs = [i for i, l in enumerate(lines)
            if l.startswith("SET_HEATER_TEMPERATURE") and "TARGET=0" in l]
    waits = [i for i, l in enumerate(lines) if l.startswith("TEMPERATURE_WAIT")]

    # Every zone is switched off, before anything is measured: a neighbour
    # still heating is heat flowing into the zone under test.
    expect(len([i for i in offs if i < i_cal]) == len(BED_ZONES),
           "expected all %d zones switched off before the calibration, got %d"
           % (len(BED_ZONES), len([i for i in offs if i < i_cal])))
    expect(waits and max(waits) < i_cal and min(waits) > min(offs),
           "the cooldown must sit between switching the zones off and the "
           "calibration (%s)" % lines)

    # TEMPERATURE_WAIT matches the FULL section name; PID_CALIBRATE and
    # SET_HEATER_TEMPERATURE take the bare heater name. Getting that backwards
    # is an error mid-run, hours into the procedure.
    text = "\n".join(lines)
    expect('SENSOR="heater_bed"' in text,
           "heater_bed is not waited on by its section name (%s)" % text)
    for zone in BED_ZONES:
        if zone == "heater_bed":
            continue
        expect('SENSOR="heater_generic %s"' % zone in text,
               "%s is waited on by its bare name, which TEMPERATURE_WAIT does "
               "not accept" % zone)
    for i in waits:
        expect("MAXIMUM=%d" % BED_COOL_BELOW in lines[i],
               "a cooldown wait is not bounded from above (%s)" % lines[i])

    cal = lines[i_cal]
    expect("HEATER=BED_VR" in cal and "TARGET=%d" % BED_TEMP in cal,
           "the calibration line is %r" % cal)
    expect([l for l in lines[i_cal + 1:]
            if l.startswith("SET_HEATER_TEMPERATURE") and "HEATER=BED_VR" in l
            and "TARGET=0" in l],
           "the zone is left heating after its calibration (%s)" % lines)

    # COOL_BELOW=0 is the documented way to skip the wait.
    lines = render(macros, "_PID_TUNE_BED_ZONE", printer_state(),
                   params={"HEATER": "heater_bed", "TEMP": "80", "COOL_BELOW": "0"})
    expect(not [l for l in lines if l.startswith("TEMPERATURE_WAIT")],
           "COOL_BELOW=0 still waited for the plate to cool")


def check_bed_pid_single(macros, expect):
    for arg, zone in (("1", "heater_bed"), ("4", "BED_HR"),
                      ("BED_HL", "BED_HL"), ("bed_hl", "BED_HL")):
        lines = render(macros, "PID_TUNE_BED", printer_state(), params={"ZONE": arg})
        call = lines[index_of(lines, r"^_PID_TUNE_BED_ZONE")]
        expect("HEATER=%s " % zone in call + " ",
               "ZONE=%s tuned %r, expected %s" % (arg, call, zone))
        expect(len([l for l in lines if l == "_CONSOLE_SAVE_CONFIG"]) == 1,
               "ZONE=%s did not end with exactly one save prompt" % arg)

    for arg in ("9", "0", "", "BED_XX"):
        msg = refusal_message(macros, "PID_TUNE_BED", printer_state(), params={"ZONE": arg})
        expect(msg is not None, "PID_TUNE_BED accepted ZONE=%r" % arg)
        # Refusing is half the job; the operator has to learn what to type
        # instead, without going to read the config file.
        expect(msg is None or all(z in msg for z in BED_ZONES),
               "ZONE=%r was refused with %r, which does not name the zones that "
               "would have worked" % (arg, msg))
    expect(refuses(macros, "PID_TUNE_BED", printer_state(), params={}),
           "PID_TUNE_BED accepted a missing ZONE")
    # Named zone that this printer does not have.
    expect(refuses(macros, "PID_TUNE_BED", printer_state(bed_zones=["heater_bed"]),
                   params={"ZONE": "BED_VR"}),
           "PID_TUNE_BED tuned a heater the printer does not have")
    for state in ("printing", "paused"):
        expect(refuses(macros, "PID_TUNE_BED", printer_state(printing=state),
                       params={"ZONE": "1"}),
               "a bed PID run started while %s" % state)

    # The one warning that decides whether the numbers mean anything.
    text = "\n".join(render(macros, "_PID_TUNE_BED_WARN", printer_state()))
    expect("mirror" in text.lower(),
           "nothing warns that a heater_bed mirror ruins the calibration (%s)" % text)
    text = "\n".join(render(macros, "_PID_TUNE_BED_WARN",
                            printer_state(bed_zones_cfg=True)))
    expect("bed-zones.cfg" in text,
           "the warning does not name bed-zones.cfg when it is installed")


def check_blow_bed(macros, expect):
    lines = render(macros, "BLOW_BED", printer_state())
    text = "\n".join(lines)

    i_home = index_of(lines, r"^MAYBE_HOME\b")
    i_z = index_of(lines, r"^G0 Z")
    i_fan_on = index_of(lines, r"^SET_FAN_SPEED FAN=%s SPEED=1" % BLOW_FAN)
    expect(i_home < i_z < i_fan_on,
           "order must be home, bed to sweep height, then the fan")
    z = number_in(lines[i_z], r"G0 Z([-\d.]+)")
    expect(abs(z - BLOW_Z) < 1e-6, "the plate is swept at Z%s, expected %s" % (z, BLOW_Z))

    # The fan blows from the head that moves: carriage 0, selected explicitly
    # rather than trusting whatever the machine thought was active.
    selected = "SET_DUAL_CARRIAGE CARRIAGE=0" in lines
    expect(selected,
           "carriage 0 is not selected, so the moving head may not be the one "
           "with the running fan")
    expect(not selected or lines.index("SET_DUAL_CARRIAGE CARRIAGE=0") < i_fan_on,
           "the fan starts before the sweeping head is selected")

    # Fan up to speed before the first sweep move, and off at the end.
    i_dwell = index_of(lines, r"^G4 P")
    sweeps = [i for i, l in enumerate(lines) if re.match(r"^G0 [XY]", l) and i > i_fan_on]
    expect(i_fan_on < i_dwell < min(sweeps),
           "the fan gets no spin-up time before the sweep starts")
    i_off = index_of(lines, r"^SET_FAN_SPEED FAN=%s SPEED=0\b" % BLOW_FAN)
    expect(i_off > max(sweeps), "the fan is switched off before the sweep ends")

    # One line per spacing step, covering the plate, ends alternating so every
    # line is actually swept rather than travelled to.
    ys = [number_in(l, r"G0 Y([-\d.]+)") for l in lines[i_fan_on:] if l.startswith("G0 Y")]
    xs = [number_in(l, r"G0 X([-\d.]+)") for l in lines[i_fan_on:] if l.startswith("G0 X")]
    expect(len(ys) == BLOW_LINES,
           "%d sweep lines, expected %d at %smm spacing" % (len(ys), BLOW_LINES, BLOW_SPACING))
    expect(ys[0] == 0.0 and abs(ys[-1] - PRINTABLE_Y_MAX) < 1e-6,
           "the sweep runs Y%s..Y%s, expected the whole plate 0..%s"
           % (ys[0], ys[-1], PRINTABLE_Y_MAX))
    expect(all(abs((b - a) - BLOW_SPACING) < 1e-6 for a, b in zip(ys, ys[1:])),
           "the sweep lines are not evenly spaced: %s" % ys)
    expect(len(xs) == len(ys) and all(x != y for x, y in zip(xs, xs[1:])),
           "consecutive lines sweep to the same end, so half of them are "
           "travel rather than sweep: %s" % xs)
    expect(set(xs) == {0.0, 600.0},
           "the sweep does not reach both ends of the plate: %s" % set(xs))

    # A second pass must not repeat the direction of the last line of the first.
    lines2 = render(macros, "BLOW_BED", printer_state(), params={"PASSES": "2"})
    xs2 = [number_in(l, r"G0 X([-\d.]+)") for l in lines2 if l.startswith("G0 X")]
    xs2 = xs2[1:] if len(xs2) > 2 * BLOW_LINES else xs2   # drop the approach move
    expect(len(xs2) == 2 * BLOW_LINES,
           "PASSES=2 produced %d sweeps, expected %d" % (len(xs2), 2 * BLOW_LINES))
    expect(all(x != y for x, y in zip(xs2, xs2[1:])),
           "the second pass repeats the end the first one finished at, so its "
           "first line is never swept: %s" % xs2)

    # Overrides.
    lines = render(macros, "BLOW_BED", printer_state(), params={"Z": "40", "SPACING": "100"})
    expect(abs(number_in(lines[index_of(lines, r"^G0 Z")], r"G0 Z([-\d.]+)") - 40) < 1e-6,
           "Z= was not used")
    ys = [number_in(l, r"G0 Y([-\d.]+)") for l in lines if l.startswith("G0 Y")]
    expect(len(ys) == int(PRINTABLE_Y_MAX // 100) + 1,
           "SPACING=100 gave %d lines, expected %d" % (len(ys), int(PRINTABLE_Y_MAX // 100) + 1))

    # A printer without that fan is told which variable to change, not left
    # sweeping a cold plate with nothing blowing.
    msg = refusal_message(macros, "BLOW_BED", printer_state(part_fans=()))
    expect(msg is not None and "variable_fan" in msg,
           "a missing part fan was not reported with the fix (%r)" % msg)

    # A hot nozzle drips on what it is cleaning: worth saying, not refusing.
    lines = render(macros, "BLOW_BED", printer_state(hotend_temp=210.0))
    expect([l for l in lines if "WARNING" in l and "drip" in l.lower()],
           "nothing warns that a hot nozzle drips on the plate")
    expect([l for l in lines if l.startswith("G0 Y")],
           "a hot nozzle stopped the sweep instead of warning about it")

    for state in ("printing", "paused"):
        expect(refuses(macros, "BLOW_BED", printer_state(printing=state)),
               "BLOW_BED swept the plate while %s" % state)


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
    except (AssertionError, ValueError, KeyError, IndexError) as exc:
        sys.stderr.write("FAIL: %s: %s\n" % (type(exc).__name__, exc))
        return 1
    if failures:
        for message in failures:
            sys.stderr.write("FAIL: %s\n" % message)
        return 1
    print("ok: service macros render. MAINTENANCE_MODE parks at Z%s Y%s X%s/%s, "
          "NOZZLE_CHANGE presents T0 at X%s / T1 at X%s at %sC, lubrication "
          "stops at Z%s Y%s X%s"
          % (Z_MID, Y_FRONT, X_T0, X_T1, NOZZLE_X_T0, NOZZLE_X_T1, NOZZLE_TEMP,
             LUBE_STATIONS["Z"], LUBE_STATIONS["Y"], LUBE_STATIONS["X"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
