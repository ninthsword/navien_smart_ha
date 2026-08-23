"""Entities must not disappear when the set of air-quality kinds shrinks.

This came from a real NRT-20D. CO2, particulates and radon accumulated normally until
20 August; then the server began sending only temperature and humidity, and restarting Home
Assistant turned the other six sensors `unavailable` wholesale. Even when the values came
back, they did not revive until another restart.

Within a session `set_air_sensors` merges and protects them, but entities are created only
once at startup, so that protection did not survive a restart.
"""

from __future__ import annotations

import re
import sys

from harness import Report, make_airone, source

r = Report()


r.section("the kinds ever seen are remembered")

device = make_airone(filters=[])
r.ok(device.known_sensor_kinds == (), "nothing is known at first")
r.ok(device.entity_sensor_kinds == (), "there are no entities to create either")

full = [
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "86"},
    {"type": "co2", "value": "605"},
    {"type": "pm2Dot5", "value": "3"},
]
device.set_air_sensors(full)
r.ok(device.sensor_kinds == ("pm2Dot5", "co2", "temperature", "humidity"), "the kinds that carried values")
r.ok(device.known_sensor_kinds == device.sensor_kinds, "the kinds ever seen match them")

# The air monitor drops out and the server sends only temperature and humidity.
device.set_air_sensors([
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "86"},
])
r.ok(
    device.sensor_kinds == ("pm2Dot5", "co2", "temperature", "humidity"),
    "merging within a session keeps the earlier values",
)
r.ok("co2" in device.known_sensor_kinds, "nothing drops out of the kinds ever seen")


r.section("entities survive a restart")

restarted = make_airone(filters=[])
# With the stored kinds restored, this poll returned only temperature and humidity.
restarted.remember_sensor_kinds(["temperature", "humidity", "co2", "pm2Dot5"])
restarted.set_air_sensors([
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "86"},
])
r.ok(
    restarted.sensor_kinds == ("temperature", "humidity"),
    "only two kinds carried values",
)
r.ok(
    restarted.entity_sensor_kinds == ("pm2Dot5", "co2", "temperature", "humidity"),
    "four entities are created even so",
)
r.ok(
    "entity_sensor_kinds" in source("sensor.py"),
    "entity creation uses the kinds ever seen",
)


r.section("unknown kinds are not remembered")

restarted.remember_sensor_kinds(["co2", "noSuchKind", 7, None])
r.ok(
    "noSuchKind" not in restarted.known_sensor_kinds,
    "a name absent from the table is not stored",
)
r.ok(
    len(restarted.known_sensor_kinds) == 4,
    "with odd values mixed in, only known kinds survive",
)


r.section("a missing value does not freeze it as a string sensor")

sensor_source = source("sensor.py")
r.ok(
    "if raw else True" in sensor_source,
    "with no value yet, it is treated as numeric",
)
r.ok(
    "a device_class once the value returns" in sensor_source,
    "the reasoning is recorded",
)


r.section("both stored shapes are accepted")

coordinator_source = source("coordinator.py")
r.ok("air_kinds" in coordinator_source, "the air-quality kinds are stored alongside")
r.ok(
    'if "reported" in entry or "air_kinds" in entry' in coordinator_source,
    "new and old shapes are told apart by shape",
)
r.ok(
    "reported, kinds = entry, None" in coordinator_source,
    "the old shape is read wholesale as reported",
)
r.ok(
    "remember_sensor_kinds(kinds)" in coordinator_source,
    "the restored kinds are put back on the device",
)


r.section("diagnostics shows which kinds are missing")

diagnostics_source = source("diagnostics.py")
r.ok("air_sensor_kinds_known" in diagnostics_source, "the kinds ever seen are recorded")
r.ok("air_sensor_kinds_missing" in diagnostics_source, "the kinds missing this time are recorded")


r.section("the user is told when the data stops")

binary_source = source("binary_sensor.py")
r.ok("AironeAirDataMissing" in binary_source, "the air-data-missing sensor is created")
r.ok("공기질 자료 끊김" in binary_source, "it is given a name")
r.ok(
    "reports missing data" in binary_source,
    "it records that this is an observation, not a fault verdict",
)
r.ok(
    "AironeAirDataMissing(coordinator, airone)" in binary_source
    and "wants_air_sensors" in binary_source,
    "created only for devices that are asked about air quality",
)

r.ok(
    "_log_air_sensors_missing" in coordinator_source,
    "a loss during a session is logged as well",
)
r.ok(
    "AIRONE_SENSOR_KINDS," in coordinator_source,
    "the labels used in the log are imported",
)


r.section("a returning kind reaches disk")

# On a real device six sensors stayed unavailable even after the data returned. Kinds are
# only updated by the air-quality query while saving happened only on an MQTT report, so a
# newly seen kind never reached disk and a restart restored only the old ones.
body = coordinator_source.split("async def _async_update_air_sensors")[1]
body = body.split("\n    def ")[0]
r.ok("set_air_sensors" in body, "the air-quality query updates the kinds")
r.ok(
    "_async_remember_state()" in body,
    "the same place also persists them",
)
r.ok(
    body.index("set_air_sensors") < body.index("_async_remember_state()"),
    "the save comes after the update",
)
r.ok(
    "!= before" in body,
    "nothing is saved when nothing grew",
)

grown = make_airone(filters=[])
grown.remember_sensor_kinds(["temperature", "humidity"])
was = grown.known_sensor_kinds
grown.set_air_sensors([
    {"type": "temperature", "value": "25"},
    {"type": "humidity", "value": "59"},
    {"type": "co2", "value": "605"},
])
r.ok(grown.known_sensor_kinds != was, "a returning kind changes the set, so a save is triggered")

same = make_airone(filters=[])
same.set_air_sensors([{"type": "temperature", "value": "25"}])
was = same.known_sensor_kinds
same.set_air_sensors([{"type": "temperature", "value": "26"}])
r.ok(same.known_sensor_kinds == was, "a poll where only values changed triggers no save")


r.section("nothing is saved before the restore")

# Adding the save above immediately turned 17 room-controller values unknown. The first poll
# runs before the restore, and the snapshot written then held no `reported`, since it had not
# been read yet. The save replaces the document wholesale, so the `reported` on disk was
# destroyed and the restore that followed read back what had just been erased.
save = coordinator_source.split("def _async_remember_state")[1]
save = save.split("\n    async def ")[0]
r.ok("_state_restored" in save, "it checks the restore flag first")
r.ok(
    save.index("_state_restored") < save.index("snapshot: dict"),
    "it bails out before building the snapshot",
)
r.ok(
    "async_delay_save" in save and save.index("_state_restored") < save.index("async_delay_save"),
    "it bails out before writing",
)

restore = coordinator_source.split("async def async_restore_state")[1]
restore = restore.split("\n    # -- ")[0]
r.ok(
    restore.count("self._state_restored = True") == 3,
    "the flag is set even when the read fails, or nothing could ever be written",
)
r.ok(
    "_async_remember_state()" in restore,
    "one save follows the restore, persisting the kinds the first poll could not",
)
r.ok(
    restore.rindex("self._state_restored = True")
    < restore.index("self._async_remember_state()"),
    "the call comes after the flag is set",
)


r.section("polling does not discard the restored kinds")

# **This is what defeated the section above.** Device objects are rebuilt on every poll and
# the fields to carry over are listed by hand, and `known_sensor_kinds` was missing from that
# list. Five minutes later the restored kinds narrowed back down to whatever was arriving, and
# that narrowed set overwrote the stored copy, so the sensors disappeared again on restart.
carry = coordinator_source.split("def _parse_airone")[1].split("\n    def ")[0]
r.ok(
    "device.known_sensor_kinds = old.known_sensor_kinds" in carry,
    "the kinds ever seen are carried over",
)

FULL = ("pm2Dot5", "co2", "temperature", "humidity")


# **The list is not written out by hand.** Naming the fields here would let the test share
# the implementation's assumptions: if the test carries over a field the code does not, it
# passes with the bug intact. Written that way, this whole section did pass. So the
# assignments `_parse_airone` actually uses are extracted from the source and imitated.
CARRIED = re.findall(r"device\.(\w+) = old\.\1", carry)
assert CARRIED, "no carried-over fields were found in _parse_airone"


def _carry_over(old_device):
    """A new object carrying only the fields `_parse_airone` really carries over."""
    fresh = make_airone(filters=[])
    for name in CARRIED:
        setattr(fresh, name, getattr(old_device, name))
    return fresh


def _airs(kinds):
    return [{"type": k, "value": "1"} for k in kinds]


# Just after a restart: the first poll receives only temperature and humidity, and the
# restore then fills in four kinds.
poll1 = make_airone(filters=[])
poll1.set_air_sensors(_airs(["temperature", "humidity"]))
poll1.remember_sensor_kinds(FULL)
r.ok(len(poll1.known_sensor_kinds) == 4, "four kinds immediately after the restore")

# The poll five minutes later. The server still sends only temperature and humidity.
poll2 = _carry_over(poll1)
before = poll2.known_sensor_kinds
poll2.set_air_sensors(_airs(["temperature", "humidity"]))
r.ok(
    len(poll2.known_sensor_kinds) == 4,
    "four kinds survive the next poll",
)
r.ok(
    poll2.entity_sensor_kinds == FULL,
    "four entities are still created",
)
r.ok(
    poll2.known_sensor_kinds == before,
    "nothing changed, so no save runs and the stored set is not narrowed",
)

# When the values return the set grows, and a save has to run then.
poll3 = _carry_over(poll2)
before = poll3.known_sensor_kinds
poll3.set_air_sensors(_airs(["temperature", "humidity", "pm10"]))
r.ok(
    "pm10" in poll3.known_sensor_kinds and poll3.known_sensor_kinds != before,
    "a new kind grows the set and triggers a save",
)


sys.exit(r.finish())
