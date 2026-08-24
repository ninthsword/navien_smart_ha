"""Verify the boiler status fields that used to be parsed and thrown away.

The values here come straight from a real NR-67D diagnostics dump. Of its 44 status fields,
25 were unused, and only those whose meaning is certain are opened. Leaving the uncertain
ones **unnamed, as raw values**, is a rule of this repository.
"""

from __future__ import annotations

import sys

from harness import Report, source
from navien_smarthome.boiler import BoilerDevice

r = Report()


def make_boiler() -> BoilerDevice:
    device = BoilerDevice.parse(
        {
            "deviceId": "0011223344556272",
            "deviceSeq": 14,
            "serviceCode": 100,
            "modelCode": "20",
            "modelName": "NR-67D",
            "mqttTopicKey": "private-topic-key",
            "connected": 1,
            "Properties": {
                "nickName": "보일러",
                "did": {
                    "response": {
                        "macAddress": "001122334455",
                        # This device plainly uses hot water, yet hotWaterUse reads 1. The
                        # *Use fields of feature must not be read on the same convention as
                        # status.
                        "feature": {"hotWaterUse": 1, "outsideTemperatureDisplayUse": 1},
                    }
                },
            },
        }
    )
    assert device is not None
    return device


# The status block exactly as it appears in a real diagnostics dump.
STATUS = {
    "outsideTemperature": 270,
    "DHWUse": 1,
    "DHWUseSustained": 1,
    "DHWInternalFlowRate": 32,
    "heatFlowRate": 22,
    "wifiRssi": 54,
    "faultStatus1": 0,
    "faultStatus2": 0,
    "heatingIntensityModeSetting": 3,
    "timeCycleReservationSettingHour": 1,
    "timeCycleReservationSettingMinute": 10,
    "dayCycleReservationSetting": "000000000000000000000000",
    "programReservationUse": 1,
    "fastDHWReservationUse": 1,
}

device = make_boiler()
device.apply_status(STATUS, now=100.0)


r.section("outside temperature")

r.ok(device.outside_temperature == 27.0, "read in 0.1C units")
# On this device the value arrives even with outsideTemperatureDisplayUse at 1. Gating on
# that flag would have produced None above instead of 27.0.
r.ok(
    device.supports_feature("outsideTemperatureDisplayUse") is False
    and device.outside_temperature == 27.0,
    "a value that arrives is read even with the feature flag off",
)
r.ok(
    "a regional weather observation" in source("boiler.py"),
    "it records that the boiler did not measure this",
)
r.ok(
    "집 마당 기온으로 쓰지" in source("../../README.md"),
    "the README warns this is not the temperature at the house",
)
device.apply_status({"outsideTemperature": None}, now=110.0)
r.ok(device.outside_temperature is None, "with no value, nothing is guessed")
device.apply_status({"outsideTemperature": 270}, now=120.0)


r.section("detecting hot-water use")

r.ok(device.hot_water_running is False, "DHWUse 1 means not in use")
r.ok(device.hot_water_sustained is False, "DHWUseSustained uses the same encoding")
device.apply_status({"DHWUse": 2, "DHWUseSustained": 2}, now=130.0)
r.ok(device.hot_water_running is True, "2 means in use")
r.ok(device.hot_water_sustained is True, "sustained use is on at 2 as well")
device.apply_status({"DHWUse": 7}, now=140.0)
r.ok(device.hot_water_running is None, "an unrecognised value is not guessed to be on")
device.apply_status({"DHWUse": 1}, now=150.0)


r.section("flow rates and signal")

r.ok(device.hot_water_flow_rate == 3.2, "hot-water flow is in 0.1 units")
r.ok(device.heating_flow_rate == 2.2, "heating flow is in 0.1 units too")
r.ok(device.wifi_rssi == 54, "the Wi-Fi signal passes through as a raw integer")
r.ok("The unit was never confirmed" in source("boiler.py"), "it records that the unit is unknown")
r.ok("L/min" in source("sensor.py"), "the flow sensors use litres per minute")


r.section("fault bits")

r.ok(device.fault_status == (0, 0), "both values are read together")
device.apply_status({"faultStatus1": 4}, now=160.0)
r.ok(device.fault_status == (4, 0), "with only one present, the other fills in as 0")
device.apply_status({"faultStatus1": 0}, now=170.0)
empty = make_boiler()
r.ok(empty.fault_status is None, "with neither present, no judgement is made")


r.section("heating intensity is read only")

r.ok(device.heating_intensity == 3, "the raw step value passes through")
intensity_source = source("sensor.py").split("class BoilerHeatingIntensitySensor")[1]
r.ok("with no label" in intensity_source, "it records why the steps are left unnamed")
r.ok(
    "async_set" not in intensity_source.split("class ")[0],
    "no control path is created",
)
r.ok(
    "heatingIntensity" not in source("switch.py")
    and "heatingIntensity" not in source("number.py")
    and "heatingIntensity" not in source("select.py"),
    "heating intensity gets no control entity",
)


r.section("schedules are read only")

r.ok(device.repeat_reservation_interval == (1, 10), "the repeat interval is read as hours and minutes")
r.ok(device.day_cycle_reservation == "0" * 24, "the raw 24-hour schedule is kept verbatim")
r.ok(device.reservation_enabled("programReservationUse") is False, "a disabled schedule is read")
device.apply_status({"programReservationUse": 2}, now=180.0)
r.ok(device.reservation_enabled("programReservationUse") is True, "an enabled schedule is read")
r.ok(
    device.reservation_enabled("missingKey") is None,
    "with no value, the schedule state is not guessed",
)
reservation_source = source("sensor.py").split("class BoilerReservationSensor")[1]
r.ok("Never write it" in reservation_source, "it records why schedules are never written")
r.ok("kept raw, uninterpreted" in source("boiler.py"), "it records that the table positions are left uninterpreted")


r.section("collecting command codes of unknown meaning")

fresh = make_boiler()
fresh.apply_status({"command": 33554438}, now=200.0)
fresh.apply_status({"command": 33554438}, now=210.0)
fresh.apply_status({"command": 33554434}, now=220.0)
r.ok(
    fresh.observed_commands == {33554438: 2, 33554434: 1},
    "every command code seen is collected with its count",
)
r.ok(
    "observed_commands" in source("diagnostics.py"),
    "kept in diagnostics so the codes can be learned by pressing buttons in the app",
)

boiler_source = source("boiler.py")
r.ok("0x2000004 = 33554436" in boiler_source, "the observed away command code is recorded")
r.ok("These are observations, not" in boiler_source, "it records how that was learned")
r.ok(
    "33554436" not in source("select.py") and "33554436" not in source("switch.py"),
    "a command the device never executes gets no control entity",
)
r.ok("gooutUse" in boiler_source, "it records that this must be read alongside the support flags")


r.section("error codes transcribed from the manual")

from navien_smarthome.boiler import (  # noqa: E402
    BOILER_ERROR_NAMES,
    BOILER_STATE_HEATING,
)

r.ok(BOILER_STATE_HEATING == "연소", "the running state uses the manual's own wording")
r.ok(BOILER_ERROR_NAMES[1] == "열교환기 과열", "E001 was transcribed")
r.ok(BOILER_ERROR_NAMES[110] == "배기폐쇄", "three-digit numbers were transcribed too")
r.ok(BOILER_ERROR_NAMES[792] == "환탕 라인 순환 이상", "the table was transcribed through its last entry")
r.ok(len(BOILER_ERROR_NAMES) == 31, "the count matches the manual's table")

coded = make_boiler()
coded.apply_status({"errorCode": 110}, now=300.0)
r.ok(coded.error_label == "E110", "it builds the same notation as the manual")
r.ok(coded.error_name == "배기폐쇄", "the fault description is attached")
coded.apply_status({"errorCode": 999}, now=310.0)
r.ok(coded.error_label == "E999", "an unknown number still gets a label")
r.ok(coded.error_name is None, "a number absent from the table gets no invented name")
coded.apply_status({"errorCode": 0}, now=320.0)
r.ok(coded.error_label is None and coded.error_name is None, "a healthy device reports no error")
r.ok(
    "alignment of two notations" in boiler_source,
    "it states how the numbers were matched",
)


r.section("the new entities really are created")

sensor_setup = source("sensor.py").split("async_add_entities(entities)")[0]
for name in (
    "outside_temperature",
    "BoilerFlowRateSensor",
    "BoilerWifiSignalSensor",
    "BoilerHeatingIntensitySensor",
    "BoilerReservationSensor",
):
    r.ok(name in sensor_setup, f"{name} is registered")

binary_setup = source("binary_sensor.py").split("async_add_entities(entities)")[0]
r.ok("BoilerHotWaterRunning" in binary_setup, "the hot-water-in-use sensor is registered")
r.ok("BoilerFaultProblem" in binary_setup, "the fault-status sensor is registered")


sys.exit(r.finish())
