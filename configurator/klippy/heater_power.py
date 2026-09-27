# Report heater power in watts as a temperature sensor.
#
# Klipper knows each heater's PWM duty cycle. It has no notion of electrical
# power, and the web interface has no field for watts -- but it renders every
# [temperature_sensor] live and graphs it. So this registers a sensor type that
# reports duty x rated watts, which then shows up beside the temperatures.
#
# The number is DERIVED, not measured: nothing here senses current or mains
# voltage. It is rated power times the fraction of time the heater is switched
# on, which is as close as a printer without a power meter gets. A heater whose
# real element has drifted, or a mains that sags under load, moves the true
# figure without moving this one.
#
# The interface labels every sensor in degrees. There is no way to tell it
# otherwise from here, so put the unit in the name:
#
#   [heater_power]                     # loads this module, registers the type
#
#   [temperature_sensor Bed_zone_1_W]
#   sensor_type: heater_power
#   heaters: heater_bed
#   rated_watts: 600
#
#   [temperature_sensor Heaters_total_W]
#   sensor_type: heater_power
#   heaters: heater_bed, BED_VR, BED_HL, BED_HR, chamber_heater
#   rated_watts: 600, 600, 600, 600, 1500
#
# One rated_watts value is applied to every heater listed; otherwise the two
# lists must be the same length.
#
# Two deliberate refusals to be strict, because this is a display and a display
# must never be why a printer will not boot:
#
#   * A heater named here that the printer does not have is dropped with a
#     warning on the console, not a config error.
#   * min_temp / max_temp are accepted and never enforced. A watt reading
#     cannot shut the printer down, however large it gets.

import logging

REPORT_TIME = 1.0


class HeaterPowerSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]

        self.heater_names = config.getlist("heaters")
        if not self.heater_names:
            raise config.error(
                "heater_power %s: 'heaters' lists no heater" % (self.name,)
            )
        rated = list(config.getfloatlist("rated_watts"))
        if len(rated) == 1:
            rated = rated * len(self.heater_names)
        if len(rated) != len(self.heater_names):
            raise config.error(
                "heater_power %s: %d heater(s) but %d rated_watts value(s). "
                "Give one value per heater, or a single value for all of them."
                % (self.name, len(self.heater_names), len(rated))
            )
        for watts in rated:
            if watts <= 0.0:
                raise config.error(
                    "heater_power %s: rated_watts must be positive" % (self.name,)
                )
        self.rated_watts = rated

        self.last_value = 0.0
        self.min_temp = 0.0
        self.max_temp = 0.0
        self.temperature_callback = None
        self.tracked = []
        self.missing = []

        # Registered as its own object so a macro can read the exact figure
        # rather than the rounded one the sensor reports.
        self.printer.add_object("heater_power " + self.name, self)
        self.update_timer = self.reactor.register_timer(self._update_event)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    # --- the sensor contract heaters.py expects -------------------------
    def setup_minmax(self, min_temp, max_temp):
        # Accepted so the section parses like any other sensor, then ignored.
        self.min_temp = min_temp
        self.max_temp = max_temp

    def setup_callback(self, temperature_callback):
        self.temperature_callback = temperature_callback

    def get_report_time_delta(self):
        return REPORT_TIME

    # --- wiring ---------------------------------------------------------
    def _handle_ready(self):
        pheaters = self.printer.lookup_object("heaters")
        for name, watts in zip(self.heater_names, self.rated_watts):
            try:
                heater = pheaters.lookup_heater(name)
            except Exception:
                self.missing.append(name)
                continue
            self.tracked.append((name, heater, watts))
        if self.missing:
            msg = (
                "heater_power %s: this printer has no heater %s -- left out of "
                "the total. Check the names against the config."
                % (self.name, ", ".join(self.missing))
            )
            logging.warning(msg)
            self.printer.lookup_object("gcode").respond_info(msg)
        self.reactor.update_timer(
            self.update_timer, self.reactor.monotonic() + 1.0
        )

    def _update_event(self, eventtime):
        watts = 0.0
        for _name, heater, rated in self.tracked:
            # 'power' is the last PWM value the heater was given, 0..1.
            duty = heater.get_status(eventtime).get("power") or 0.0
            watts += duty * rated
        self.last_value = watts
        if self.temperature_callback is not None:
            mcu = self.printer.lookup_object("mcu")
            self.temperature_callback(
                mcu.estimated_print_time(eventtime), watts
            )
        return eventtime + REPORT_TIME

    # --- status ---------------------------------------------------------
    def get_temp(self, eventtime):
        return self.last_value, 0.0

    def get_status(self, eventtime):
        return {
            "watts": round(self.last_value, 1),
            "heaters": [name for name, _heater, _watts in self.tracked],
            "missing": list(self.missing),
        }


def load_config(config):
    pheaters = config.get_printer().load_object(config, "heaters")
    pheaters.add_sensor_factory("heater_power", HeaterPowerSensor)
