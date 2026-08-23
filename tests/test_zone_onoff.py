"""Turning a single zone off and on (issue #16).

A device has two axes.

    operationMode     device power, one per device
    heater.<zone>     { enable, plus temperature.set or level.set }

**Turning off is a value, not an off command.** Dropping one step below the setting range
turns that zone off — the same structure as one step below level 1 being standby on a carbon
mat.

    stepped     (1-8, step 1)      1 - 1   = 0
    temperature (28-50, step 0.5)  28 - 0.5 = 27.5

The app sends the temperature and `enable` **together** (`enable = value >= rangeMin`) and
reads **only `enable`** when interpreting state. It does not trust the temperature a
powered-off zone reports and overwrites the display with "off". So `enable` is what decides
off here as well.

**Both sides cannot be turned off at once** — the device refuses (confirmed on real
hardware). The app does not power the device down on the user's behalf either; it just
explains. What the user asked for was one zone off, not the device off.
"""

from __future__ import annotations

import sys

from harness import Report, make_mat, source

r = Report()

# reference values from real devices
TEMP = dict(unit="0.5C", range_min=28, range_max=50)   # EME-520 / EMF520
LEVEL = dict(unit="1.0L", range_min=1, range_max=8)    # EME-500 (carbon)

MODELS = source("models.py")
CLIMATE = source("climate.py")
SELECT = source("select.py")


def refuse(device, zones) -> str | None:
    try:
        device.build_zone_off(zones)
    except ValueError as err:
        return str(err)
    return None


r.section("the off value is one step below the minimum")

temp = make_mat(**TEMP, capacity=2, zones={"left": 33.0, "right": 30.0})
r.ok(temp.heat_control.off_value == 27.5, f"temperature mat 28 - 0.5 = {temp.heat_control.off_value}")

level = make_mat(**LEVEL, capacity=2, zones={"left": 3, "right": 2})
r.ok(level.heat_control.off_value == 0, f"stepped mat 1 - 1 = {level.heat_control.off_value}")

# With an unknown axis, no value is invented.
water = make_mat(unit="1.0C", range_min=28, range_max=45, capacity=2,
                 zones={"left": 33.0, "right": 31.0})
r.ok(not water.heat_control.is_known, "1.0C is an axis that has not been confirmed")
r.ok(water.heat_control.off_value is None, "an unknown axis produces no off value")


r.section("turning one side off sends a lowered value")

sent = temp.build_zone_off(["right"])
r.ok(sent["heater"]["right"]["temperature"]["set"] == 27.5, "the right side goes to 27.5")
r.ok(sent["heater"]["right"]["enable"] is False, "enable is lowered alongside it")
r.ok(sent["heater"]["left"]["temperature"]["set"] == 33.0, "the left side stays at 33")
r.ok("operationMode" not in sent, "turning one side off leaves the power alone")


r.section("off is decided by enable, so display and command agree")

# A powered-off zone can report a temperature inside the normal range. That is why the app
# uses `enable`.
odd = make_mat(**TEMP, capacity=2, zones={"left": 30.0, "right": 30.0},
               enables={"left": False})
r.ok(odd.zone_setting("left") == 30.0, "the temperature is reported inside the normal range (30)")
r.ok(odd.zone_enabled("left") is False, "yet enable says off")
r.ok(odd.zone_is_off("left") is True, "enable is trusted, so it reads as off")

zone_is_off = MODELS.split("def zone_is_off")[1].split("\n    def ")[0]
r.ok("zone_enabled" in zone_is_off, "zone_is_off consults enable")
r.ok("zone_enabled" in CLIMATE, "hvac_mode consults enable too, so the two agree")


r.section("turning on raises a powered-off zone to the minimum")

on = odd.build_zone_on(["left"])
r.ok(on["operationMode"] == 1, "the device powers on")
r.ok(on["heater"]["left"]["temperature"]["set"] == 28, "the left side, which was off, goes to 28")
r.ok(on["heater"]["left"]["enable"] is True, "enable is switched on too")
r.ok(on["heater"]["right"]["temperature"]["set"] == 30.0, "the right side, already on, keeps its value")

# **A zone the user asked to turn on always goes into the command.** When the device sent no
# temperature for it, that zone used to drop out entirely: power came on and the zone stayed
# as it was.
missing = make_mat(**TEMP, capacity=2, zones={"right": 30.0})
missing.apply_reported({"heater": {"left": {"enable": False}}})
r.ok(missing.zone_setting("left") is None, "the left temperature cannot be read")
r.ok(missing.zone_is_off("left") is True, "enable still shows it is off")
sent = missing.build_zone_on(["left"])
r.ok("left" in sent["heater"], "an instruction to turn on always includes that zone")
r.ok(sent["heater"]["left"]["temperature"]["set"] == 28, "the minimum value is sent with it")

# With no state ever received, no value is invented — an explanation goes out instead.
# Turning off has a fixed value to send and can be built without state; turning on cannot,
# because how far to raise it has to come from the device state.
blank = make_mat(**TEMP, capacity=2, zones={})
try:
    blank.build_zone_on(["left"])
    guide = ""
except ValueError as err:
    guide = str(err)
r.ok("전원" in guide and "스위치" in guide, "with no state, the message points at the power switch")
r.ok("heater" in blank.build_zone_off(["left"]), "turning off can be sent without any state")


r.section("the last remaining zone cannot be turned off; it refuses and explains")

half = make_mat(**TEMP, capacity=2, zones={"left": 27.5, "right": 30.0})
r.ok(half.zone_is_off("left") is True, "the left side is already off")
msg = refuse(half, ["right"])
r.ok(msg is not None, "the last remaining zone is refused")
r.ok("매트 전원을 종료해" in (msg or ""), f"it says what to do instead: {msg}")
r.ok(refuse(half, ["left", "right"]) is not None, "turning both off at once is refused as well")

single = make_mat(**TEMP, capacity=1, zones={"single": 30.0})
r.ok(refuse(single, ["single"]) is not None, "the same holds for a single-zone mat")

# **Power is never switched off on the user's behalf.** The app does not do it either.
build_zone_off = MODELS.split("def build_zone_off")[1].split("\n    def ")[0]
r.ok("operationMode" not in build_zone_off, "turning off never produces a power command")

# An unknown state does not block: send it and let the device decide.
unknown = make_mat(**TEMP, capacity=2, zones={"right": 30.0})
r.ok(unknown.zone_is_off("left") is None, "the left state is unknown")
r.ok(refuse(unknown, ["right"]) is None, "an unknown state does not block")


r.section("on a stepped mat both readings give the same answer")

# `level 0` and `enable false` move together (confirmed on a real device), so switching the
# test to `enable` changes nothing for owners of a stepped mat.
for value, want_off in ((0, True), (1, False), (3, False)):
    mat = make_mat(**LEVEL, capacity=2, zones={"left": value, "right": 2})
    by_enable = mat.zone_is_off("left")
    by_value = value <= mat.heat_control.off_value
    r.ok(by_enable is want_off and by_value is want_off,
         f"level {value}: by enable {by_enable}, by value {by_value}")

mat = make_mat(**LEVEL, capacity=2, zones={"left": 0, "right": 2})
r.ok(mat.build_zone_on(["left"])["heater"]["left"]["level"]["set"] == 1, "standby becomes step 1")
r.ok(mat.build_zone_on(["right"])["heater"]["right"]["level"]["set"] == 2, "the side already on keeps its value")
r.ok(refuse(mat, ["right"]) is not None, "with the left in standby, turning the right off is refused")


r.section("the entities use different axes")

r.ok("not control.is_celsius" in CLIMATE, "climate is created only for temperature mats")
r.ok("not control.is_level" in SELECT, "select is created only for stepped mats")
r.ok("build_zone_off" in SELECT, "standby on a stepped mat uses the same path")


sys.exit(r.finish())
