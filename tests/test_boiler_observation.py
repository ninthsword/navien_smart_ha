"""Verify boiler status and the NR-67D setpoint commands."""

from __future__ import annotations

import json
import sys
from datetime import date

from harness import Report, source
from navien_smarthome.boiler import (
    BoilerDevice,
    extract_boiler_status,
    observe_boiler_message,
    sanitize_boiler_value,
)
from navien_smarthome.const import (
    SERVICE_BOILER,
    SUPPORTED_SERVICE_CODES,
    TOPIC_PREFIX,
)
from navien_smarthome.diagnostics import TO_REDACT, _identifiers, _scrub

r = Report()


r.section("status sensors and limited control")

r.ok(TOPIC_PREFIX[SERVICE_BOILER] == "smarttok", "it subscribes to the same observation topic as the app")
r.ok(SERVICE_BOILER in SUPPORTED_SERVICE_CODES, "the boiler is classed as a supported device")
r.ok("async_boiler_request" in source("api.py"), "only the boiler envelope is relayed")
r.ok("boiler.build_start_payload" in source("coordinator.py"), "the initial status is requested once MQTT connects")
r.ok(
    "_schedule_boiler_readback(boiler)" in source("coordinator.py"),
    "a plain status query follows the initial request once",
)
r.ok(
    "전원·빠른온수·터보온수·설정온도 제어" in source("../../README.md"),
    "the README states what is supported",
)
r.ok("`대기`, `연소`" in source("../../README.md"), "the README lists the status sensor values")
r.ok("히팅 여부와 관계없이" in source("../../README.md"), "the README states the control condition")


r.section("an observed status envelope")

status = {
    "operationMode": 6,
    "operationBusy": 2,
    "errorCode": 0,
    "subErrorCode": 0,
    "insideTemperature": 60,
    "actualInsideTemperature": 303,
    "supplyTemperature": 67,
    "returnTemperature": 64,
    "ondolTemperatureSetting": 60,
    "hotWaterTemperature": 62,
    "hotWaterTemperatureSetting": 86,
    "insideHumidity": 585,
    "fastDHWUse": 1,
    "smartFastDHW": 1,
    "DHWBoost": 2,
}
envelope = {
    "topic": "private-response-topic",
    "payload": {
        "response": {
            "macAddress": "001122334455",
            "status": status,
        }
    },
}
stats = {}
parsed = extract_boiler_status(json.dumps(envelope).encode(), stats)
r.ok(parsed == ("001122334455", status), "the status envelope and the physical device id are extracted")
r.ok(stats == {"accepted": 1}, "received state is counted")

ignored_stats = {}
r.ok(
    extract_boiler_status(b'{"payload":{"response":{"feature":{}}}}', ignored_stats)
    is None,
    "a DID response is never used as state",
)
r.ok(ignored_stats == {"dropped_no_status": 1}, "non-status responses are counted")

raw_device = {
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
                "feature": {
                    "ondolTemperatureMin": 60,
                    "ondolTemperatureMax": 130,
                    "ondolUse": 2,
                    "hotWaterTemperatureMin": 60,
                    "hotWaterTemperatureMax": 120,
                    "hotWaterTemperatureSettingUse": 2,
                    "powerUse": 2,
                    "fastDHWUse": 2,
                    "smartFastDHWUse": 2,
                    "DHWBoostUse": 2,
                    "gasUsageUse": 2,
                },
            }
        },
    },
}
device = BoilerDevice.parse(raw_device)
r.ok(device is not None, "the boiler is built from REST metadata")
assert device is not None
r.ok(device.nickname == "보일러", "a string nickname becomes the device name")
dict_nick_device = BoilerDevice.parse(
    {
        **raw_device,
        "Properties": {
            **raw_device["Properties"],
            "nickName": {"mainItem": "보일러"},
        },
    }
)
r.ok(
    dict_nick_device is not None and dict_nick_device.nickname == "보일러",
    "a mainItem-shaped nickname becomes the device name",
)
device.apply_status(status)
r.ok(device.indoor_temperature == 30.3, "the precise indoor temperature is in 0.1C units")
r.ok(device.supply_temperature == 33.5, "the heating-water temperature is in 0.5C units")
r.ok(device.return_temperature == 32.0, "the return temperature is in 0.5C units")
r.ok(device.ondol_target_temperature == 30.0, "the underfloor setpoint is in 0.5C units")
r.ok(device.hot_water_temperature == 31.0, "the current hot-water value is in 0.5C units")
r.ok(device.hot_water_target_temperature == 43.0, "the hot-water setpoint is in 0.5C units")
r.ok(device.indoor_humidity == 58.5, "indoor humidity is in 0.1% units")
r.ok(device.operation_mode == 6, "the raw mode code is preserved")
r.ok(device.operation_mode_name == "온돌 난방", "operationMode=6 is the underfloor heating mode")
r.ok(device.operation_busy == 2 and not device.heating_is_idle, "the observed heating value 2 is preserved")
r.ok(device.operating_state == "연소", "powered on with operationBusy=2 means combustion")
device.apply_status({"operationBusy": 1})
r.ok(device.heating_is_idle, "the observed idle value 1 is preserved")
r.ok(device.operating_state == "대기", "powered on with operationBusy=1 means idle")
device.apply_status({"operationBusy": 3})
r.ok(device.operating_state is None, "an unrecognised operationBusy is not guessed to be combustion")
device.apply_status({"operationMode": 1})
r.ok(device.operating_state == "꺼짐", "operationMode=1 means powered off")
r.ok(device.operation_mode_name == "꺼짐", "the mode name for operationMode=1 is off as well")
device.apply_status({"operationMode": 99})
r.ok(device.operation_mode_name is None, "an unrecognised operating mode gets no guessed name")
device.apply_status({"operationMode": 6, "operationBusy": 2})
r.ok(device.status_age is not None, "when MQTT state arrived is kept for diagnostics")
r.ok(device.error_code == 0 and device.available, "a connected device that received state is available")
r.ok(device.switch_state("power") is True, "any operationMode other than off means powered on")
r.ok(device.switch_state("fast_dhw") is False, "fast hot water 1 means off")
r.ok(device.switch_state("smart_fast_dhw") is False, "smart operation 1 means off")
r.ok(device.switch_state("dhw_boost") is True, "turbo hot water 2 means on")

gas_meter = {
    "thisYearMonthTotalGasUsage": 142,
    "thisYearMonthTotalHeatGasUsage": 99,
    "thisYearMonthTotalHotWaterGasUsage": 43,
    "gasMeterThisMonth": [
        {
            "year": 2026,
            "month": 8,
            "day": 20,
            "gasMeter": 3,
            "heatGasMeter": 1,
            "hotWaterGasMeter": 2,
        }
    ],
}
gas_envelope = {
    "payload": {
        "response": {
            "macAddress": "001122334455",
            "gasMeter": gas_meter,
        }
    }
}
gas_update = extract_boiler_status(json.dumps(gas_envelope).encode())
r.ok(gas_update is not None, "a gas-usage response attaches to the boiler too")
assert gas_update is not None
device.apply_status(gas_update[1], now=700.0)
r.ok(device.gas_total_month == 14.2, "the monthly raw gas value is divided by 10 as the app does")
r.ok(device.gas_heating_month == 9.9, "monthly heating gas usage is decoded")
r.ok(device.gas_hot_water_month == 4.3, "monthly hot-water gas usage is decoded")
r.ok(device.gas_day(date(2026, 8, 20)) == (0.3, 0.1, 0.2), "all three of today's daily gas figures are decoded")
r.ok(device.gas_day(date(2026, 8, 19)) == (0.0, 0.0, 0.0), "a missing date within the same month reads 0")
r.ok(device.gas_day(date(2026, 7, 31)) is None, "a stale array from another month is not used as today")
r.ok(device.operation_mode == 6, "a gas response does not erase the existing running state")


r.section("the five-minute idle status check")

device.apply_status({"operationBusy": 1}, now=100.0)
r.ok(device.communication_age(now=220.0) == 120.0, "it computes the time since the last message")
r.ok(device.silence_refresh_delay(now=220.0) == 180.0, "it subtracts the elapsed time from five minutes")
device.note_communication(now=250.0)
r.ok(device.communication_age(now=260.0) == 10.0, "a request HA sent also refreshes the timestamp")
r.ok(device.silence_refresh_delay(now=600.0) == 1.0, "even past five minutes it does not repeat immediately")
r.ok(
    "_schedule_boiler_silence_check(device)" in source("coordinator.py")
    and "BOILER_SILENCE_REFRESH_SECONDS" in source("coordinator.py"),
    "every message reschedules the five-minute timer",
)
r.ok(
    "boiler.last_communication_at = old.last_communication_at" in source("coordinator.py"),
    "the five-minute REST refresh does not erase the last-communication time",
)
r.ok(
    "not target.connected or not self.mqtt_connected" in source("coordinator.py"),
    "no status request is sent while disconnected",
)


r.section("the NR-67D setpoint envelope")

r.ok(device.temperature_bounds("hot_water") == (30.0, 60.0), "the hot-water range decodes the server value at 0.5C")
r.ok(device.temperature_bounds("ondol") == (30.0, 65.0), "the heating-water range decodes the server value at 0.5C")

status_payload = device.build_status_payload("mqtt-client", now_ms=123456)
r.ok(status_payload["protocolVersion"] == 1, "the status request protocol version is 1")
r.ok(
    status_payload["requestTopic"] == "cmd/20/roomcon-001122334455/status",
    "the status request topic matches the app",
)
r.ok(
    status_payload["responseTopic"]
    == "cmd/20/private-topic-key/mobile-mqtt-client/res",
    "the response topic is bound to the app MQTT client",
)
r.ok(
    status_payload["request"]
    == {
        "macAddress": "001122334455",
        "registerAt": "123456",
        "additionalValue": "6272",
        "param": [],
        "paramStr": "",
        "command": 16777219,
        "deviceType": 20,
    },
    "the inner status-request envelope matches the app",
)

start_payload = device.build_start_payload("mqtt-client", now_ms=123456)
r.ok(start_payload["requestTopic"].endswith("/status/start"), "the initial status request topic")
r.ok(start_payload["responseTopic"].endswith("/res/start"), "the initial status response topic")
r.ok(start_payload["request"]["command"] == 16777217, "the initial status request command code")

gas_payload = device.build_gas_payload("mqtt-client", now_ms=123456)
r.ok(gas_payload["requestTopic"].endswith("/status/gas-meter-query"), "the gas query request topic")
r.ok(gas_payload["responseTopic"].endswith("/res/gas-meter"), "the gas query response topic")
r.ok(gas_payload["request"]["command"] == 16777224, "the gas query command code")

hot_water = device.build_temperature_payload(
    "hot_water", 43, "mqtt-client", now_ms=123456
)
r.ok(hot_water["request"]["mode"] == "hotwater-temperature", "the hot-water mode string")
r.ok(hot_water["request"]["command"] == 33554443, "the hot-water command code")
r.ok(hot_water["request"]["param"] == [86.0], "the NR-67D sends Celsius x 2 as a raw value")
r.ok(hot_water["request"]["roomUseSetting"] == "10000000", "the hot-water bitmask")

hot_water_half = device.build_temperature_payload(
    "hot_water", 43.5, "mqtt-client", now_ms=123456
)
r.ok(hot_water_half["request"]["param"] == [87.0], "a 0.5C step is also sent as a raw integer")

ondol = device.build_temperature_payload("ondol", 50, "mqtt-client", now_ms=123456)
r.ok(ondol["request"]["mode"] == "ondol-heat", "the heating-water mode string")
r.ok(ondol["request"]["command"] == 33554438, "the heating-water command code")
r.ok(ondol["request"]["param"] == [100.0], "heating water also sends Celsius x 2 as a raw value")
r.ok(ondol["request"]["roomUseSetting"] == "11111111", "the heating-water bitmask")

try:
    device.build_temperature_payload("hot_water", 43.25, "mqtt-client")
except ValueError:
    half_step_rejected = True
else:
    half_step_rejected = False
r.ok(half_step_rejected, "a 0.25C command, absent from the modelCode=20 app, is refused")

try:
    device.build_temperature_payload("ondol", 66, "mqtt-client")
except ValueError:
    outside_rejected = True
else:
    outside_rejected = False
r.ok(outside_rejected, "a command outside the server range is refused")
r.ok("_attr_native_step = 0.5" in source("number.py"), "the number slider is in 0.5C units too")
r.ok(
    "async_refresh_boiler_status(device)" not in source("coordinator.py")
    and "not current.heating_is_idle" not in source("coordinator.py"),
    "a temperature command is sent without pre-checking the heating state",
)


r.section("the NR-67D feature-switch envelopes")

power_on = device.build_power_payload(True, "mqtt-client", now_ms=123456)
r.ok(power_on["request"]["mode"] == "power-on", "the power-on mode string")
r.ok(power_on["request"]["command"] == 33554434, "the power-on command code")
r.ok(power_on["request"]["param"] == [], "a power command carries an empty value array")
r.ok(power_on["request"]["roomUseSetting"] == "11111111", "power targets every room controller")

power_off = device.build_power_payload(False, "mqtt-client", now_ms=123456)
r.ok(power_off["request"]["mode"] == "power-off", "the power-off mode string")
r.ok(power_off["request"]["command"] == 33554433, "the power-off command code")

for kind, mode, command in (
    ("fast_dhw", "fastDHW", 33554444),
    ("smart_fast_dhw", "smartFastDHW", 33554456),
    ("dhw_boost", "DHWBoost", 33554460),
):
    enabled = device.build_switch_payload(kind, True, "mqtt-client", now_ms=123456)
    disabled = device.build_switch_payload(kind, False, "mqtt-client", now_ms=123456)
    r.ok(enabled["request"]["mode"] == mode, f"{kind}: the app mode string")
    r.ok(enabled["request"]["command"] == command, f"{kind}: the command code")
    r.ok(enabled["request"]["param"] == [2], f"{kind}: on is value 2")
    r.ok(disabled["request"]["param"] == [1], f"{kind}: off is value 1")
    r.ok(enabled["request"]["roomUseSetting"] == "10000000", f"{kind}: the hot-water bitmask")

r.ok("async_boiler_power" in source("coordinator.py"), "the power control path lives in the coordinator")
r.ok("async_boiler_switch" in source("coordinator.py"), "the hot-water feature control path lives in the coordinator")
r.ok("BoilerMonthlyGasSensor" in source("sensor.py"), "the monthly gas sensor is created")
r.ok("BoilerDailyGasSensor" in source("sensor.py"), "the daily gas sensor is created")
r.ok("async_track_time_change" in source("sensor.py"), "the date basis rolls at midnight")
r.ok("BOILER_GAS_REFRESH_SECONDS" in source("coordinator.py"), "gas usage refreshes infrequently")


r.section("boiler diagnostics identifiers")

for key in (
    "clientID",
    "sessionID",
    "macAddress",
    "requestTopic",
    "responseTopic",
    "boilerControllerSerialNumber",
):
    r.ok(key in TO_REDACT, f"the {key} key is redacted")

sensitive = {
    "clientID": "client-secret-123",
    "macAddress": "001122334455",
    "requestTopic": "cmd/20/roomcon-001122334455/status/start",
}
scrubbed = repr(_scrub(sensitive, _identifiers([sensitive])))
r.ok("client-secret" not in scrubbed, "the clientID value appears nowhere in diagnostics")
r.ok("001122334455" not in scrubbed, "the MAC value does not survive inside a topic either")


r.section("only the JSON structure survives; identifiers are scrubbed")

raw = {
    "payload": {
        "deviceId": "DEVICE-SECRET-1234",
        "clientID": "CLIENT-SECRET-1234",
        "requestTopic": "cmd/20/roomcon-secret/status/start",
        "insideTemperature": "23.5",
        "mode": 2,
        "epoch": 1_787_210_786,
        "label": "우리집 보일러",
        "zones": [1, 2, 3],
    },
    "ABCDEF1234567890": {"value": 7},
}
observation = observe_boiler_message(
    json.dumps(raw, ensure_ascii=False).encode(),
    "12345/smarttok/DEVICE-SECRET-1234/status",
)
text = repr(observation)

r.ok(observation["encoding"] == "json", "a JSON envelope is recognised")
r.ok(observation["topic_suffix_depth"] == 2, "only the topic depth survives, never the text")
r.ok(
    observation["shape"]["payload"]["insideTemperature"] == 23.5,
    "short numbers survive",
)
r.ok(observation["shape"]["payload"]["mode"] == 2, "small integer states survive too")
r.ok(observation["shape"]["payload"]["epoch"] == {"kind": "number"}, "large numbers are redacted")
r.ok("DEVICE-SECRET" not in text, "the deviceId value does not survive")
r.ok("CLIENT-SECRET" not in text, "the clientID value does not survive")
r.ok("roomcon-secret" not in text, "the topic string does not survive")
r.ok("우리집" not in text, "even an ordinary string leaves no original text")
r.ok("ABCDEF1234567890" not in text, "a dict key shaped like an identifier is redacted too")


r.section("binary payloads and the size limit")

binary = observe_boiler_message(b"\x00\xffDEVICE-SECRET", "1/smarttok/secret")
r.ok(binary["encoding"] == "binary_or_text", "anything not JSON keeps only its kind")
r.ok("DEVICE-SECRET" not in repr(binary), "raw binary is never retained")

oversize = observe_boiler_message(b"[" + b"0," * 40_000 + b"0]", "1/smarttok/x")
r.ok(oversize["encoding"] == "oversize", "a large JSON payload is not parsed")
r.ok("shape" not in oversize, "a large JSON payload is not re-expanded in memory")

many = sanitize_boiler_value(list(range(40)))
r.ok(len(many) == 33, "a list keeps 32 items plus a truncation marker")
r.ok(many[-1] == {"kind": "truncated_items", "count": 8}, "the number truncated is reported")


r.section("the reasoning is recorded in the code")

boiler_source = source("boiler.py")
r.ok(
    "modelCode=20" in boiler_source and "operationMode=1" in boiler_source,
    "the model and state mapping evidence is recorded",
)
r.ok(
    "The official NR-67D manual likewise separates" in boiler_source
    and "flame indicator" in boiler_source,
    "it records the manual's split between selected mode and actual operation",
)
r.ok("Raw binary, even a leading" in boiler_source, "it records why binary is never retained")


sys.exit(r.finish())
