#!/usr/bin/env python3
"""Run the heater_power sensor against fakes and check what it reports.

The module turns a heater's PWM duty cycle into watts so the interface can show
it. Two things make it worth testing rather than eyeballing:

* It runs on the printer as a registered klippy extension. A config error in a
  ``[temperature_sensor]`` section is a printer that does not boot, so the ways
  it is deliberately NOT strict -- an unknown heater, a min/max it ignores --
  are load-bearing behaviour, not politeness.
* The arithmetic is the whole feature. duty x rated, summed, is easy to get
  subtly wrong (a missing heater counted as full power, a single rating not
  spread over the list) and impossible to notice on screen, because a plausible
  wrong number looks exactly like a plausible right one.

Klipper is not importable here, so the printer, reactor and config are fakes
that implement only what the module actually calls.

Usage:
    tests/test_heater_power.py [path-to-heater_power.py]
"""

import importlib.util
import os
import sys

DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "configurator", "klippy", "heater_power.py",
)


class ConfigError(Exception):
    pass


class FakeReactor:
    NEVER = float("inf")

    def __init__(self):
        self.timers = []
        self.updates = []

    def register_timer(self, callback):
        self.timers.append(callback)
        return callback

    def update_timer(self, timer, waketime):
        self.updates.append((timer, waketime))

    def monotonic(self):
        return 100.0


class FakeHeater:
    def __init__(self, power):
        self.power = power

    def get_status(self, eventtime):
        return {"temperature": 60.0, "target": 60.0, "power": self.power}


class FakeHeaters:
    def __init__(self, heaters):
        self.heaters = heaters

    def lookup_heater(self, name):
        if " " in name:
            name = name.split(" ", 1)[1]
        if name not in self.heaters:
            raise Exception("Unknown heater '%s'" % name)
        return self.heaters[name]


class FakeGcode:
    def __init__(self):
        self.messages = []

    def respond_info(self, msg, log=True):
        self.messages.append(msg)


class FakeMcu:
    def estimated_print_time(self, eventtime):
        return eventtime - 0.5


class FakePrinter:
    def __init__(self, heaters):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode()
        self.objects = {"heaters": FakeHeaters(heaters), "mcu": FakeMcu()}
        self.event_handlers = {}
        self.added = {}
        self.shutdowns = []

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name):
        if name == "gcode":
            return self.gcode
        return self.objects[name]

    def load_object(self, config, name):
        return self.objects[name]

    def add_object(self, name, obj):
        self.added[name] = obj

    def register_event_handler(self, event, callback):
        self.event_handlers[event] = callback

    def invoke_shutdown(self, msg):
        self.shutdowns.append(msg)


class FakeConfig:
    error = ConfigError

    def __init__(self, printer, name, options):
        self.printer = printer
        self.name = name
        self.options = options

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def getlist(self, option, default=None, sep=",", count=None):
        raw = self.options.get(option)
        if raw is None:
            return default
        return [part.strip() for part in raw.split(sep) if part.strip()]

    def getfloatlist(self, option, default=None, sep=",", count=None):
        parts = self.getlist(option, None, sep)
        if parts is None:
            return default
        return [float(part) for part in parts]


def load_module(path):
    spec = importlib.util.spec_from_file_location("heater_power", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(module, printer, heaters, watts, name="W_test"):
    config = FakeConfig(printer, "temperature_sensor " + name,
                        {"heaters": heaters, "rated_watts": watts})
    return module.HeaterPowerSensor(config)


def run(sensor, printer):
    """Bring the sensor to the state it is in on a running printer."""
    reported = []
    sensor.setup_minmax(-273.15, 99999999.9)
    sensor.setup_callback(lambda read_time, value: reported.append((read_time, value)))
    printer.event_handlers["klippy:ready"]()
    next_time = printer.reactor.timers[0](printer.reactor.monotonic())
    return reported, next_time


def check(path):
    module = load_module(path)
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    # --- the arithmetic -------------------------------------------------
    printer = FakePrinter({"heater_bed": FakeHeater(0.5), "BED_VR": FakeHeater(1.0),
                           "BED_HL": FakeHeater(0.0), "BED_HR": FakeHeater(0.25),
                           "chamber_heater": FakeHeater(0.8)})
    sensor = build(module, printer, "heater_bed", "600")
    reported, _ = run(sensor, printer)
    expect(reported and abs(reported[-1][1] - 300.0) < 1e-6,
           "a 600W heater at 50%% duty should report 300W, got %s" % (reported,))

    # One rating is spread over every heater; it is not "600W in total".
    printer = FakePrinter({"a": FakeHeater(1.0), "b": FakeHeater(0.5)})
    sensor = build(module, printer, "a, b", "600")
    reported, _ = run(sensor, printer)
    expect(abs(reported[-1][1] - 900.0) < 1e-6,
           "a single rated_watts must apply to each heater (600 + 300), got %s"
           % reported[-1][1])

    # Per-heater ratings, the shape the real total uses.
    printer = FakePrinter({"heater_bed": FakeHeater(1.0), "BED_VR": FakeHeater(0.5),
                           "BED_HL": FakeHeater(0.0), "BED_HR": FakeHeater(0.25),
                           "chamber_heater": FakeHeater(1.0)})
    sensor = build(module, printer, "heater_bed, BED_VR, BED_HL, BED_HR, chamber_heater",
                   "600, 600, 600, 600, 1500")
    reported, next_time = run(sensor, printer)
    expected = 600 + 300 + 0 + 150 + 1500
    expect(abs(reported[-1][1] - expected) < 1e-6,
           "the total is %s, expected %s" % (reported[-1][1], expected))
    expect(abs(sensor.get_status(0.0)["watts"] - expected) < 1e-6,
           "get_status disagrees with what the sensor reported")

    # The timer has to re-arm at a real cadence, or the display freezes at the
    # first reading. "Some time in the future" is not enough of a check:
    # reactor.NEVER is in the future too, and means never.
    expected_wake = printer.reactor.monotonic() + module.REPORT_TIME
    expect(abs(next_time - expected_wake) < 1e-6,
           "the update timer re-arms at %s, expected %s (one REPORT_TIME on)"
           % (next_time, expected_wake))
    expect(printer.reactor.updates, "the timer is never started at ready")

    # --- not strict, on purpose -----------------------------------------
    printer = FakePrinter({"heater_bed": FakeHeater(1.0)})
    sensor = build(module, printer, "heater_bed, BED_VR", "600, 600")
    reported, _ = run(sensor, printer)
    expect(abs(reported[-1][1] - 600.0) < 1e-6,
           "a missing heater must be left out of the total, not counted as "
           "full power: got %s" % reported[-1][1])
    expect(any("BED_VR" in m for m in printer.gcode.messages),
           "a missing heater is dropped without telling anyone: %s"
           % printer.gcode.messages)
    expect(sensor.get_status(0.0)["missing"] == ["BED_VR"],
           "the status does not name the heater that is missing")

    # A watt reading must never be able to shut the printer down.
    printer = FakePrinter({"heater_bed": FakeHeater(1.0)})
    sensor = build(module, printer, "heater_bed", "600")
    sensor.setup_minmax(0.0, 10.0)          # far below the 600 it will report
    sensor.setup_callback(lambda read_time, value: None)
    printer.event_handlers["klippy:ready"]()
    printer.reactor.timers[0](printer.reactor.monotonic())
    expect(not printer.shutdowns,
           "a reading outside min/max shut the printer down: %s" % printer.shutdowns)

    # --- config errors, which are the operator's own typing -------------
    for heaters, watts, why in (
        ("a, b, c", "600, 600", "fewer ratings than heaters"),
        ("a", "600, 600", "more ratings than heaters"),
        ("a", "0", "a zero rating"),
        ("a", "-600", "a negative rating"),
        ("", "600", "no heaters at all"),
    ):
        printer = FakePrinter({"a": FakeHeater(1.0)})
        try:
            build(module, printer, heaters, watts)
        except ConfigError:
            pass
        else:
            failures.append("%s was accepted" % why)

    # --- registration ---------------------------------------------------
    printer = FakePrinter({})
    factories = {}
    printer.objects["heaters"].add_sensor_factory = lambda name, factory: factories.__setitem__(name, factory)
    module.load_config(FakeConfig(printer, "heater_power", {}))
    expect(factories.get("heater_power") is module.HeaterPowerSensor,
           "the module does not register the 'heater_power' sensor type: %s"
           % list(factories))

    return failures


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT
    if not os.path.isfile(path):
        sys.stderr.write("FAIL: %s does not exist\n" % path)
        return 1
    try:
        failures = check(path)
    except Exception as exc:  # a crash here is a failure, not a traceback
        sys.stderr.write("FAIL: %s: %s\n" % (type(exc).__name__, exc))
        return 1
    if failures:
        for message in failures:
            sys.stderr.write("FAIL: %s\n" % message)
        return 1
    print("ok: heater_power reports duty x rated watts, drops unknown heaters "
          "and cannot shut the printer down")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
