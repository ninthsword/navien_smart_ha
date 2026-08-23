"""Pin the fixes that came out of the full review so they cannot regress.

Each section states **what was wrong** first. Recording only the corrected shape leaves the
next person with no way to know why that line cannot be deleted.
"""

from __future__ import annotations

import sys

from harness import Report, source

r = Report()


r.section("the re-authentication entry point exists")

# Raising `ConfigEntryAuthFailed` makes HA open the flow with `SOURCE_REAUTH` and look for
# `async_step_reauth`. With only a confirmation step and no entry point, the flow never opens
# and a user who changed their password has to delete and reinstall the integration — losing
# every entity id and all long-term statistics.
flow = source("config_flow.py")
r.ok("async def async_step_reauth(" in flow, "the entry point is defined")
r.ok("async def async_step_reauth_confirm(" in flow, "the confirmation step exists too")
r.ok(
    flow.index("async def async_step_reauth(")
    < flow.index("async def async_step_reauth_confirm("),
    "the entry point comes before the confirmation step",
)

for module in ("__init__.py", "coordinator.py"):
    if "ConfigEntryAuthFailed" in source(module):
        r.ok(True, f"{module} can demand re-authentication, so the entry point is needed")


r.section("the preceding statistics total is never reset to 0")

# When the range the server sends moves forward and no row sits at that point (the server
# sends no row for a month with no usage at all), this used to return 0. That restarted the
# whole two-year series from zero and drew a huge negative usage at the boundary, because HA
# derives usage from differences in `sum`.
gas = source("gas_statistics.py")
r.ok("_async_last_sum_before" in gas, "there is a path that finds the last preceding total")
baseline = gas.split("async def _async_baseline")[1].split("\nasync def ")[0]
r.ok(
    "_async_last_sum_before" in baseline,
    "with no row at that point, it takes that path",
)
r.ok(
    baseline.rfind("return 0.0") < baseline.find("_async_last_sum_before"),
    "it searches backwards before returning 0",
)


r.section("gas statistics writes never overlap")

# The sequence is read the previous total, add, write. If the second read happens before the
# first write, both start from the same value and both write the wrong one. That can occur
# when the initial request overlaps the hourly refresh, or when the server returns the same
# response twice.
coord = source("coordinator.py")
r.ok("_gas_statistics_locks" in coord, "there is a per-device lock")
importer = coord.split("async def _async_import_gas_statistics")[1]
importer = importer.split("\n    @callback")[0]
r.ok("async with lock" in importer, "the write happens while holding the lock")
r.ok(
    importer.index("async with lock") < importer.index("async_import_gas_statistics("),
    "the call comes after the lock is taken",
)


r.section("the MQTT backoff does not reset on every CONNACK")

# When the link dropped immediately after connecting, the backoff sat on its first delay
# (5 seconds) forever. With one session per account that really happens whenever the user
# leaves the Navien app open — reconnecting every five seconds and re-sending the initial
# status request to every device each time.
mq = source("mqtt.py")
r.ok("_STABLE_CONNECTION_SECONDS" in mq, "it defines how long counts as a real connection")
run = mq.split("async def _async_run")[1].split("\n    async def ")[0]
r.ok("self._attempt = 0" in run, "there is a place that resets it")
r.ok(
    run.index("_STABLE_CONNECTION_SECONDS") < run.index("self._attempt = 0"),
    "the reset happens after checking how long it held",
)
r.ok(
    "await self._async_wait_connected()\n                self._attempt = 0" not in run,
    "no reset immediately after CONNACK",
)


r.section("one-shot timers are cancelled too")

# Waking after an unload or reload has an integration that no longer exists send a request to
# the server. The exception is caught, but a pointless request still reaches an unofficial
# server, and it was inconsistent with every other timer, which is managed.
r.ok("_oneshot_unsubs" in coord, "one-shot timers are held")
r.ok("_track_oneshot" in coord, "there is a path that holds them")
stop = coord.split("async def async_stop_mqtt")[1].split("\n    @property")[0]
r.ok("_oneshot_unsubs" in stop, "they are cancelled on shutdown")

# Every call to `async_call_later` has to keep its result somewhere.
# **The preceding line is inspected too**: when the call is wrapped across several lines, the
# line holding it has neither the assignment nor the wrapping name. A single-line check bit
# three pieces of perfectly good code.
lines = coord.splitlines()
loose = []
for index, line in enumerate(lines):
    if "async_call_later(" not in line or "import" in line:
        continue
    context = " ".join(lines[max(0, index - 2) : index + 1])
    if "=" in context or "_track_oneshot" in context:
        continue
    loose.append(line.strip())
r.ok(not loose, f"no async_call_later discards its result ({len(loose)} found)")


r.section("simultaneous failures still log in only once")

# The server allows one session per account, so requests that fail at the same time would
# invalidate each other by each logging in.
api = source("api.py")
login = api.split("async def async_login")[1].split("\n    async def ")[0]
r.ok("seen = self._session" in login, "it remembers the session from before entering")
r.ok(
    "self._session is not seen" in login,
    "if it changed while waiting, that one is used",
)
r.ok(
    login.index("async with self._lock") < login.index("self._session is not seen"),
    "the check happens inside the lock",
)


r.section("the setpoint range follows the current value")

# Reading it once at startup and freezing it leaves the slider showing the old range after
# the server changes it: the user moves it and the command is rejected.
num = source("number.py")
r.ok("def native_min_value" in num, "the minimum is exposed as a property")
r.ok("def native_max_value" in num, "the maximum is exposed as a property")
r.ok("_fallback_bounds" in num, "a value is kept for the moment the device disappears")


r.section("there are no unused imports")

# The same kind of thing happened twice — `_dig` in `coordinator.py` and
# `LEGACY_EXTRA_FIELDS` in `airone.py`. Neither did any harm, so both lingered.
for module, name in (
    ("coordinator.py", "_dig"),
    ("airone.py", "LEGACY_EXTRA_FIELDS"),
):
    r.ok(name not in source(module), f"{module} does not import {name}")


sys.exit(r.finish())
