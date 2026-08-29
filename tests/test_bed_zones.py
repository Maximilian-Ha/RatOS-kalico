#!/usr/bin/env python3
"""Run machine/bed-zones.cfg off-printer and check what it does to the heaters.

The macros in that file decide which of the four bed heaters get a target and
which get zero. Getting that wrong is not a config typo you notice: the print
starts on a cold quarter of the plate and peels off two layers in. So the
macros are executed here rather than read.

The simulator is deliberately small and mirrors only what Klipper actually
does with a [gcode_macro]:

  * jinja2 with Klipper's delimiters -- '{%' for blocks, '{' for expressions
  * ``variable_`` options parsed with ast.literal_eval, as Klipper does
  * one render of the whole template, then the emitted commands run in order,
    so a macro cannot see a variable it set in the same render -- the ordering
    bug this file is most likely to grow
  * SET_HEATER_TEMPERATURE / TEMPERATURE_WAIT / SET_GCODE_VARIABLE /
    UPDATE_DELAYED_GCODE / M190.1 as builtins, everything else resolves to a
    [gcode_macro] section or fails the test as an unknown command

It proves the selection logic, not the hardware. Which physical quarter of the
bed each heater sits under is an assumption of the config, checked on the
machine with BED_ZONES_TEST -- see docs/BED-ZONES.md.

Usage:
    tests/test_bed_zones.py [--cfg machine/bed-zones.cfg]
                            [--definition configurator/printer-600/printer-definition.json]
"""

import argparse
import ast
import json
import os
import re
import shlex
import sys

try:
    import jinja2
except ImportError:
    print("SKIP: jinja2 is not installed, cannot execute the macros")
    raise SystemExit(0)

ZONE_HEATERS = ("heater_bed", "BED_VR", "BED_HL", "BED_HR")


class MacroError(Exception):
    """action_raise_error() from inside a macro."""


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def parse_cfg(path):
    """Klipper-ish config parse: {section: {option: value}}, continuations kept."""
    sections = {}
    section = None
    option = None
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(";"):
                continue
            header = re.match(r"^\[([^\]]+)\]\s*$", line)
            if header:
                section = header.group(1).strip()
                sections.setdefault(section, {})
                option = None
                continue
            if section is None:
                continue
            if line[0] in " \t" and option is not None:
                sections[section][option] += "\n" + line
                continue
            if ":" in line:
                option, value = line.split(":", 1)
                option = option.strip()
                sections[section][option] = value.strip()
    return sections


def strip_comments(text):
    """Klipper strips '#' comments from option values, but not inside gcode."""
    out = []
    for line in text.split("\n"):
        hit = line.find("#")
        if hit != -1:
            line = line[:hit]
        out.append(line.rstrip())
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# the simulator
# ---------------------------------------------------------------------------


class Printer:
    def __init__(self, cfg, extra_status=None):
        self.cfg = cfg
        self.macros = {}       # NAME -> template text
        self.variables = {}    # macro name -> {variable: value}
        self.delayed = {}      # NAME -> template text
        self.renamed = {}      # NAME -> original name it shadowed
        self.heaters = {}
        self.waits = []
        self.wait_minimums = {}
        self.responses = []
        self.commands = []
        self.saved = {}
        self.timers = {}
        self.extra_status = extra_status or {}
        self.env = jinja2.Environment("{%", "%}", "{", "}",
                                      undefined=jinja2.Undefined)
        for section, options in cfg.items():
            if section.startswith("gcode_macro "):
                name = section.split(" ", 1)[1]
                self.macros[name.upper()] = options.get("gcode", "")
                self.variables[name] = {
                    key[len("variable_"):]: ast.literal_eval(strip_comments(value))
                    for key, value in options.items()
                    if key.startswith("variable_")
                }
                if "rename_existing" in options:
                    self.renamed[options["rename_existing"].strip().upper()] = name.upper()
            elif section.startswith("delayed_gcode "):
                name = section.split(" ", 1)[1]
                self.delayed[name.upper()] = options.get("gcode", "")
        for name in ZONE_HEATERS:
            self.heaters[name] = {"temperature": 25.0, "target": 0.0, "power": 0.0}

    # -- status ------------------------------------------------------------

    def status(self):
        status = {}
        for name, heater in self.heaters.items():
            key = "heater_bed" if name == "heater_bed" else "heater_generic %s" % name
            status[key] = dict(heater)
        for name, variables in self.variables.items():
            status["gcode_macro %s" % name] = dict(variables)
        status.update(self.extra_status)
        return status

    # -- rendering ---------------------------------------------------------

    def render(self, text, params, rawparams):
        def respond(message):
            self.responses.append(str(message))
            return ""

        def raise_error(message):
            raise MacroError(str(message))

        context = {
            "printer": self.status(),
            "params": params,
            "rawparams": rawparams,
            "action_respond_info": respond,
            "action_raise_error": raise_error,
            "action_emergency_stop": raise_error,
        }
        return self.env.from_string(text).render(context)

    # -- execution ---------------------------------------------------------

    def run(self, line, depth=0):
        if depth > 20:
            raise AssertionError("macro recursion too deep at %r" % line)
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            return
        self.commands.append(line)
        parts = line.split(None, 1)
        command = parts[0].upper()
        argtext = parts[1] if len(parts) > 1 else ""

        if re.match(r"^M\d+(\.\d+)?$", command):
            params = {m.group(1).upper(): m.group(2)
                      for m in re.finditer(r"([A-Za-z])\s*(-?[0-9.]+)", argtext)}
        else:
            params = {}
            for token in shlex.split(argtext):
                if "=" in token:
                    key, value = token.split("=", 1)
                    params[key.upper()] = value

        builtin = getattr(self, "_do_" + command.replace(".", "_"), None)
        if builtin is not None and command not in self.macros:
            builtin(params)
            return
        if command in self.macros:
            body = self.render(self.macros[command], params, argtext)
            for emitted in body.split("\n"):
                self.run(emitted, depth + 1)
            return
        raise AssertionError("unknown command %r emitted by a bed-zone macro" % command)

    def run_delayed(self, name):
        name = name.upper()
        body = self.render(self.delayed[name], {}, "")
        for emitted in body.split("\n"):
            self.run(emitted, 1)

    # -- builtins ----------------------------------------------------------

    def _do_SET_HEATER_TEMPERATURE(self, params):
        heater = params["HEATER"]
        if heater not in self.heaters:
            raise AssertionError("no such heater: %r" % heater)
        self.heaters[heater]["target"] = float(params["TARGET"])

    def _do_TEMPERATURE_WAIT(self, params):
        sensor = params["SENSOR"]
        name = sensor.split(" ", 1)[1] if sensor.startswith("heater_generic ") else sensor
        if name not in self.heaters:
            raise AssertionError("no such sensor: %r" % sensor)
        if self.heaters[name]["target"] <= 0:
            raise AssertionError("waiting on %s, which is switched off" % name)
        self.waits.append(name)
        self.wait_minimums[name] = float(params["MINIMUM"])
        self.heaters[name]["temperature"] = self.heaters[name]["target"]

    def _do_M190_1(self, params):
        target = float(params.get("S", 0))
        self.heaters["heater_bed"]["target"] = target
        if target > 0:
            self.waits.append("heater_bed")
            self.heaters["heater_bed"]["temperature"] = target

    def _do_SET_GCODE_VARIABLE(self, params):
        macro = params["MACRO"]
        if macro not in self.variables:
            raise AssertionError("no such macro: %r" % macro)
        if params["VARIABLE"] not in self.variables[macro]:
            raise AssertionError(
                "%s has no variable_%s" % (macro, params["VARIABLE"]))
        self.variables[macro][params["VARIABLE"]] = ast.literal_eval(params["VALUE"])

    def _do_SAVE_VARIABLE(self, params):
        self.saved[params["VARIABLE"]] = ast.literal_eval(params["VALUE"])

    def _do_UPDATE_DELAYED_GCODE(self, params):
        if params["ID"].upper() not in self.delayed:
            raise AssertionError("no such delayed_gcode: %r" % params["ID"])
        self.timers[params["ID"].upper()] = float(params["DURATION"])

    # -- helpers for the checks -------------------------------------------

    def targets(self):
        return {name: heater["target"] for name, heater in self.heaters.items()}

    def hot(self):
        return sorted(n for n, h in self.heaters.items() if h["target"] > 0)

    def state(self, key):
        return self.variables["_BED_ZONES"][key]

    def reset_log(self):
        self.waits = []
        self.wait_minimums = {}
        self.responses = []
        self.commands = []


def fresh(cfg, mode="all", **extra):
    extra.setdefault("save_variables", {"variables": {}})
    printer = Printer(cfg, extra_status=extra)
    printer.run_delayed("_BED_ZONES_INIT")
    if mode != "all":
        printer.run("BED_ZONES_MODE MODE=%s" % mode.upper())
    printer.reset_log()
    return printer


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

CHECKS = []


def check(name):
    def register(fn):
        CHECKS.append((name, fn))
        return fn
    return register


@check("zone table tiles the whole bed exactly once")
def _(cfg, definition):
    zones = Printer(cfg).state("zones")
    assert sorted(zones) == sorted(ZONE_HEATERS), sorted(zones)
    size = definition["sizes"]["600"]
    area = 0
    for name, zone in zones.items():
        x0, x1 = zone["x"]
        y0, y1 = zone["y"]
        assert 0 <= x0 < x1 <= size["x"], "%s x %s" % (name, zone["x"])
        assert 0 <= y0 < y1 <= size["y"], "%s y %s" % (name, zone["y"])
        area += (x1 - x0) * (y1 - y0)
    assert area == size["x"] * size["y"], "zones cover %d of %d mm2" % (
        area, size["x"] * size["y"])
    for a, za in zones.items():
        for b, zb in zones.items():
            if a >= b:
                continue
            overlap_x = min(za["x"][1], zb["x"][1]) - max(za["x"][0], zb["x"][0])
            overlap_y = min(za["y"][1], zb["y"][1]) - max(za["y"][0], zb["y"][0])
            assert overlap_x <= 0 or overlap_y <= 0, "%s overlaps %s" % (a, b)


@check("mode ALL heats and waits for all four zones")
def _(cfg, definition):
    printer = fresh(cfg)
    printer.run("M190 S60")
    assert printer.hot() == sorted(ZONE_HEATERS), printer.targets()
    assert sorted(printer.waits) == sorted(ZONE_HEATERS), printer.waits


@check("START_PRINT in the rear right selects BED_HR, and M190 heats it alone")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    # RatOS hands _USER_START_PRINT the whole START_PRINT parameter line. This
    # is a real one, from the configurator's own slicer fixtures, with the
    # coordinates moved into the rear right quarter.
    printer.run("_USER_START_PRINT EXTRUDER_TEMP=245,248 EXTRUDER_OTHER_LAYER_TEMP=245,243"
                " BED_TEMP=85 CHAMBER_TEMP=0 INITIAL_TOOL=0 TOTAL_LAYER_COUNT=150"
                " X0=380 Y0=380 X1=520 Y1=520 TOTAL_TOOLSHIFTS=0 FIRST_X=400 FIRST_Y=400"
                " MIN_X=382 MAX_X=518 USED_TOOLS=0")
    assert printer.state("active") == "BED_HR", printer.state("active")
    printer.run("M190 S60")
    assert printer.hot() == ["BED_HR"], printer.targets()
    assert printer.waits == ["BED_HR"], printer.waits
    assert printer.targets()["heater_bed"] == 0
    # one degree of slack: TEMPERATURE_WAIT has no settle window of its own and
    # a PID heater can hold just under its setpoint forever.
    assert printer.wait_minimums == {"BED_HR": 59}, printer.wait_minimums


@check("a print straddling a zone border heats both zones")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("_USER_START_PRINT BED_TEMP=60 X0=250 Y0=100 X1=340 Y1=200")
    assert sorted(printer.state("active").split(",")) == ["BED_VR", "heater_bed"]


@check("the margin pulls in a neighbour the part only comes close to")
def _(cfg, definition):
    margin = Printer(cfg).state("margin")
    near, clear = margin - 1, margin + 1

    def select(x0, y0, x1, y1):
        printer = fresh(cfg, mode="auto")
        printer.run("_USER_START_PRINT BED_TEMP=60 X0=%s Y0=%s X1=%s Y1=%s"
                    % (x0, y0, x1, y1))
        return sorted(printer.state("active").split(","))

    # one case per edge of the overlap test, each one step inside the margin
    # and one step outside it, so dropping any single term fails here.
    cases = [
        ((100, 100, 300 - near, 200), ["BED_VR", "heater_bed"]),    # part reaches right
        ((100, 100, 300 - clear, 200), ["heater_bed"]),
        ((300 + near, 100, 500, 200), ["BED_VR", "heater_bed"]),    # part reaches left
        ((300 + clear, 100, 500, 200), ["BED_VR"]),
        ((100, 100, 200, 300 - near), ["BED_HL", "heater_bed"]),    # part reaches back
        ((100, 100, 200, 300 - clear), ["heater_bed"]),
        ((100, 300 + near, 200, 500), ["BED_HL", "heater_bed"]),    # part reaches front
        ((100, 300 + clear, 200, 500), ["BED_HL"]),
    ]
    for box, expected in cases:
        got = select(*box)
        assert got == expected, "print area %s selected %s, expected %s" % (
            box, got, expected)


@check("exclude_object outlines are used when the slicer sends no print area")
def _(cfg, definition):
    objects = [
        {"name": "left", "polygon": [[20, 400], [80, 400], [80, 500], [20, 500]]},
        {"name": "right", "polygon": [[400, 420], [460, 420], [460, 480], [400, 480]]},
    ]
    printer = fresh(cfg, mode="auto", exclude_object={"objects": objects})
    printer.run("_USER_START_PRINT BED_TEMP=60 X0=-1 Y0=-1 X1=-1 Y1=-1")
    assert sorted(printer.state("active").split(",")) == ["BED_HL", "BED_HR"]


@check("no print area at all falls back to the whole bed")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("_USER_START_PRINT BED_TEMP=60")
    assert sorted(printer.state("active").split(",")) == sorted(ZONE_HEATERS)
    assert any("whole bed" in r for r in printer.responses), printer.responses


@check("IDEX copy and mirror mode always heat the whole bed")
def _(cfg, definition):
    for mode in ("copy", "mirror"):
        printer = fresh(cfg, mode="auto", dual_carriage={"carriage_1": mode})
        printer.run("_USER_START_PRINT BED_TEMP=60 X0=380 Y0=380 X1=520 Y1=520")
        assert sorted(printer.state("active").split(",")) == sorted(ZONE_HEATERS), mode


@check("always_on keeps a zone in every selection")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.variables["_BED_ZONES"]["always_on"] = "heater_bed"
    printer.run("_USER_START_PRINT BED_TEMP=60 X0=380 Y0=380 X1=520 Y1=520")
    assert sorted(printer.state("active").split(",")) == ["BED_HR", "heater_bed"]


@check("switching the bed off drops the selection, so the next heat-up is whole-bed")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("_USER_START_PRINT BED_TEMP=60 X0=380 Y0=380 X1=520 Y1=520")
    printer.run("M190 S60")
    printer.run("M140 S0")
    assert printer.hot() == [], printer.targets()
    assert sorted(printer.state("active").split(",")) == sorted(ZONE_HEATERS)


@check("the poll follows a bed target set outside the macros")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("BED_ZONES_SELECT ZONES=BED_HL,BED_HR")
    printer.heaters["heater_bed"]["target"] = 70.0
    printer.run_delayed("_BED_ZONES_POLL")
    assert printer.hot() == ["BED_HL", "BED_HR"], printer.targets()
    assert printer.targets()["heater_bed"] == 0
    assert printer.state("target") == 70


@check("the poll switches everything off when a zone is switched off elsewhere")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("BED_ZONES_SELECT ZONES=BED_HL,BED_HR")
    printer.run("BED_ZONES_SET TEMP=80")
    printer.heaters["BED_HL"]["target"] = 0.0
    printer.run_delayed("_BED_ZONES_POLL")
    assert printer.hot() == [], printer.targets()


@check("a quiet poll writes no heater command and re-arms itself")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("BED_ZONES_SET TEMP=60")
    printer.reset_log()
    printer.run_delayed("_BED_ZONES_POLL")
    assert not any(c.startswith("SET_HEATER_TEMPERATURE") for c in printer.commands), \
        printer.commands
    assert printer.timers.get("_BED_ZONES_POLL") == printer.state("poll_interval")


@check("the poll leaves the zones alone while M190 is waiting")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("BED_ZONES_SELECT ZONES=BED_HL,BED_HR")
    printer.run("BED_ZONES_SET TEMP=80")
    printer.variables["_BED_ZONES"]["waiting"] = True
    printer.heaters["BED_HL"]["target"] = 0.0
    printer.run_delayed("_BED_ZONES_POLL")
    assert printer.heaters["BED_HR"]["target"] == 80, printer.targets()


@check("mode ALL restores the whole bed after an automatic selection")
def _(cfg, definition):
    printer = fresh(cfg, mode="auto")
    printer.run("_USER_START_PRINT BED_TEMP=60 X0=380 Y0=380 X1=520 Y1=520")
    printer.run("BED_ZONES_SET TEMP=60")
    printer.run("BED_ZONES_MODE MODE=ALL")
    assert printer.hot() == sorted(ZONE_HEATERS), printer.targets()
    assert printer.saved.get("bed_zones_mode") == "all"


@check("the mode survives a restart through save_variables")
def _(cfg, definition):
    printer = Printer(cfg, extra_status={
        "save_variables": {"variables": {"bed_zones_mode": "auto"}}})
    printer.run_delayed("_BED_ZONES_INIT")
    assert printer.state("mode") == "auto"


@check("an unknown zone name is refused")
def _(cfg, definition):
    printer = fresh(cfg)
    try:
        printer.run("BED_ZONES_SELECT ZONES=BED_XX")
    except MacroError:
        return
    raise AssertionError("BED_ZONES_SELECT accepted a zone that does not exist")


@check("BED_ZONES_TEST heats exactly the named zone")
def _(cfg, definition):
    printer = fresh(cfg)
    printer.run("BED_ZONES_TEST ZONE=BED_VR TEMP=45")
    assert printer.hot() == ["BED_VR"], printer.targets()
    assert printer.heaters["BED_VR"]["target"] == 45


@check("BED_ZONES_STATUS reports every zone")
def _(cfg, definition):
    printer = fresh(cfg)
    printer.run("BED_ZONES_STATUS")
    for name in ZONE_HEATERS:
        assert any(name in r for r in printer.responses), (name, printer.responses)


@check("a missing heater is reported, not silently skipped")
def _(cfg, definition):
    printer = Printer(cfg)
    del printer.heaters["BED_HR"]
    printer.run_delayed("_BED_ZONES_INIT")
    assert any("BED_HR" in r for r in printer.responses), printer.responses
    printer.run("BED_ZONES_SELECT ZONES=all")
    printer.run("BED_ZONES_SET TEMP=60")
    assert printer.hot() == ["BED_HL", "BED_VR", "heater_bed"], printer.targets()


@check("M140 and M190 shadow the originals rather than recursing")
def _(cfg, definition):
    renamed = Printer(cfg).renamed
    assert renamed.get("M140.1") == "M140", renamed
    assert renamed.get("M190.1") == "M190", renamed


# ---------------------------------------------------------------------------


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cfg", default=os.path.join(root, "machine", "bed-zones.cfg"))
    parser.add_argument("--definition", default=os.path.join(
        root, "configurator", "printer-600", "printer-definition.json"))
    args = parser.parse_args()

    cfg = parse_cfg(args.cfg)
    with open(args.definition, "r", encoding="utf-8") as handle:
        definition = json.load(handle)

    print("executing %s" % os.path.relpath(args.cfg, root))
    failures = 0
    for name, fn in CHECKS:
        try:
            fn(cfg, definition)
        except Exception as exc:  # noqa: BLE001 - the report is the point
            print("  FAIL  %s\n        %s: %s" % (name, type(exc).__name__, exc))
            failures += 1
        else:
            print("  ok    %s" % name)

    total = len(CHECKS)
    print()
    if failures:
        print("%d of %d checks failed." % (failures, total))
        return 1
    print("all %d checks passed." % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
