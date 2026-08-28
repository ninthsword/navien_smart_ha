"""Pin the fixes that came out of the full review so they cannot regress.

Each section states **what was wrong** first. Recording only the corrected shape leaves the
next person with no way to know why that line cannot be deleted.
"""

from __future__ import annotations

import ast
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
    r.ok(
        "ConfigEntryAuthFailed" in source(module),
        f"{module} can demand re-authentication, so the entry point is needed",
    )


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


r.section("expired AWS credential auth uses the safe retry path")

# AWS credentials are fetched during every MQTT connect. If the REST token used by that
# refresh has expired, the same 404/407 re-login path as other authenticated requests must be
# used; otherwise MQTT retries forever with an already-invalid access token.
import asyncio  # noqa: E402

from navien_smarthome.api import (  # noqa: E402
    AwsCredentials,
    NavienSmartApi,
    NavienSmartApiError,
    NavienSmartSession,
)


def check_aws_refresh_reauth(code: int) -> bool:
    api = NavienSmartApi(None, "user", "password")  # type: ignore[arg-type]
    old = NavienSmartSession("old-token", None, "user", 1, 2, [{"homeSeq": 1}], None)
    fresh = NavienSmartSession("fresh-token", None, "user", 1, 2, [{"homeSeq": 1}], None)
    api._session = old
    calls: list[str] = []

    async def request(*args: object, **kwargs: object) -> dict[str, object]:
        token = str(kwargs["token"])
        calls.append(token)
        if len(calls) == 1:
            raise NavienSmartApiError(code, "expired")
        return {
            "data": {
                "authInfo": {
                    "accessKeyId": "access",
                    "secretKey": "secret",
                    "sessionToken": "session",
                }
            }
        }

    async def login() -> NavienSmartSession:
        api._session = fresh
        return fresh

    api._async_request = request  # type: ignore[method-assign]
    api.async_login = login  # type: ignore[method-assign]
    credentials = asyncio.run(api.async_refresh_aws_credentials())
    return (
        calls == ["old-token", "fresh-token"]
        and credentials == AwsCredentials("access", "secret", "session")
        and fresh.aws == credentials
    )


for code in (404, 407):
    r.ok(
        check_aws_refresh_reauth(code),
        f"expired AWS credentials code {code} triggers re-login and retry",
    )


def aws_refresh_api() -> tuple[NavienSmartApi, NavienSmartSession]:
    api = NavienSmartApi(None, "user", "password")  # type: ignore[arg-type]
    session = NavienSmartSession(
        "old-token",
        None,
        "user",
        1,
        2,
        [{"homeSeq": 1}],
        AwsCredentials("old-access", "old-secret", "old-session"),
    )
    api._session = session
    return api, session


def valid_aws_payload() -> dict[str, object]:
    return {
        "data": {
            "authInfo": {
                "accessKeyId": "access",
                "secretKey": "secret",
                "sessionToken": "session",
            }
        }
    }


async def forbidden_login() -> NavienSmartSession:
    raise AssertionError("login must not run")


api_once, session_once = aws_refresh_api()
once_calls: list[str] = []


async def successful_request(*args: object, **kwargs: object) -> dict[str, object]:
    once_calls.append(str(kwargs["token"]))
    return valid_aws_payload()


api_once._async_request = successful_request  # type: ignore[method-assign]
api_once.async_login = forbidden_login  # type: ignore[method-assign]
once_credentials = asyncio.run(api_once.async_refresh_aws_credentials())
r.ok(
    once_calls == ["old-token"]
    and once_credentials == AwsCredentials("access", "secret", "session")
    and session_once.aws == once_credentials,
    "a successful AWS refresh makes one request and does not log in",
)

api_bad, _session_bad = aws_refresh_api()
bad_calls: list[str] = []


async def bad_request(*args: object, **kwargs: object) -> dict[str, object]:
    bad_calls.append(str(kwargs["token"]))
    raise NavienSmartApiError(400, "bad request")


api_bad._async_request = bad_request  # type: ignore[method-assign]
api_bad.async_login = forbidden_login  # type: ignore[method-assign]
try:
    asyncio.run(api_bad.async_refresh_aws_credentials())
except NavienSmartApiError as err:
    r.ok(
        err.code == 400 and bad_calls == ["old-token"],
        "a non-auth API error propagates without re-login",
    )
else:
    r.ok(False, "a non-auth API error propagates without re-login")

api_partial, session_partial = aws_refresh_api()
partial_before = session_partial.aws


async def partial_request(*args: object, **kwargs: object) -> dict[str, object]:
    return {"data": {"authInfo": {"accessKeyId": "partial"}}}


api_partial._async_request = partial_request  # type: ignore[method-assign]
api_partial.async_login = forbidden_login  # type: ignore[method-assign]
partial_after = asyncio.run(api_partial.async_refresh_aws_credentials())
r.ok(
    partial_after is partial_before and session_partial.aws is partial_before,
    "partial authInfo does not discard existing AWS credentials",
)

for label, invalid_value in (
    ("null", None),
    ("number", 123),
    ("list", []),
    ("empty string", ""),
):
    api_invalid, session_invalid = aws_refresh_api()
    invalid_before = session_invalid.aws

    async def invalid_request(
        *args: object, _invalid: object = invalid_value, **kwargs: object
    ) -> dict[str, object]:
        return {
            "data": {
                "authInfo": {
                    "accessKeyId": "access",
                    "secretKey": _invalid,
                    "sessionToken": "session",
                }
            }
        }

    api_invalid._async_request = invalid_request  # type: ignore[method-assign]
    api_invalid.async_login = forbidden_login  # type: ignore[method-assign]
    invalid_after = asyncio.run(api_invalid.async_refresh_aws_credentials())
    r.ok(
        invalid_after is invalid_before and session_invalid.aws is invalid_before,
        f"{label} AWS credential fields do not replace valid credentials",
    )

api_race, race_old_session = aws_refresh_api()
race_fresh_credentials = AwsCredentials("fresh-access", "fresh-secret", "fresh-session")
race_fresh_session = NavienSmartSession(
    "fresh-token", None, "user", 1, 2, [{"homeSeq": 1}], race_fresh_credentials
)


async def racing_request(*args: object, **kwargs: object) -> dict[str, object]:
    api_race._session = race_fresh_session
    return valid_aws_payload()


api_race._async_request = racing_request  # type: ignore[method-assign]
race_result = asyncio.run(api_race.async_refresh_aws_credentials())
r.ok(
    race_result is race_fresh_credentials
    and race_fresh_session.aws is race_fresh_credentials
    and race_old_session.aws != AwsCredentials("access", "secret", "session"),
    "an old in-flight AWS response cannot overwrite a newer login session",
)

api_homes, old_homes_session = aws_refresh_api()
old_homes_session.homes = [None, {}, {"homeSeq": 1}]  # type: ignore[list-item]
fresh_homes_session = NavienSmartSession(
    "fresh-token",
    None,
    "user",
    1,
    2,
    [None, {}, {"homeSeq": 2}, {"homeSeq": 1}],  # type: ignore[list-item]
    None,
)
home_calls: list[str] = []


async def homes_request(*args: object, **kwargs: object) -> dict[str, object]:
    home_calls.append(str(kwargs["token"]))
    if len(home_calls) == 1:
        raise NavienSmartApiError(404, "expired")
    return {"code": 200}


async def homes_login() -> NavienSmartSession:
    api_homes._session = fresh_homes_session
    return fresh_homes_session


api_homes._async_request = homes_request  # type: ignore[method-assign]
api_homes.async_login = homes_login  # type: ignore[method-assign]
homes_result = asyncio.run(api_homes._async_authed_request("GET", "/devices"))
r.ok(
    homes_result == {"code": 200}
    and home_calls == ["old-token", "fresh-token"]
    and fresh_homes_session.homes[0] == {"homeSeq": 1},
    "reauth ignores malformed home entries and preserves the selected home",
)

api_login = NavienSmartApi(None, "user", "password")  # type: ignore[arg-type]


async def homes_form_login() -> dict[str, object]:
    return {"accessToken": "token", "loginId": "user", "userSeq": 1}


async def homes_sign_in(*args: object) -> dict[str, object]:
    return {
        "home": [
            None,
            {},
            {"nickname": "missing"},
            {"homeSeq": []},
            {"homeSeq": True},
            {"homeSeq": 7},
            {"homeSeq": "8"},
        ],
        "userInfo": {"userSeq": 2},
    }


api_login._async_form_login = homes_form_login  # type: ignore[method-assign]
api_login._async_secured_sign_in = homes_sign_in  # type: ignore[method-assign]
filtered_session = asyncio.run(api_login.async_login())
r.ok(
    filtered_session.homes == [{"homeSeq": 7}, {"homeSeq": 8}],
    "login excludes malformed homeSeq values and normalizes decimal strings",
)


r.section("issue-directed logs contain no household identifiers")

# Diagnostics redacts these values before upload. A user copying an issue-directed warning
# from the log must get the same protection.
def issue_log_expressions(module_source: str, function: str) -> set[str]:
    tree = ast.parse(module_source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    )
    expressions: set[str] = set()
    for call in (node for node in ast.walk(target) if isinstance(node, ast.Call)):
        logger = call.func
        if not (
            isinstance(logger, ast.Attribute)
            and logger.attr in {"info", "warning", "error"}
            and isinstance(logger.value, ast.Name)
            and logger.value.id == "_LOGGER"
        ):
            continue
        values = [*call.args, *(keyword.value for keyword in call.keywords)]
        for value in values:
            expressions.update(
                ast.unparse(node)
                for node in ast.walk(value)
                if isinstance(node, (ast.Name, ast.Attribute))
            )
    return expressions


for function in (
    "_log_airone_found",
    "_log_air_sensors_missing",
    "_log_four_season",
    "_log_no_air_sensors",
    "_log_unknown_season",
    "_schedule_airone_silence_check",
):
    expressions = issue_log_expressions(coord, function)
    for risky in (
        "device.nickname",
        "target.nickname",
        "device.device_id",
        "target.device_id",
        "physical_device_id",
        "topic",
    ):
        r.ok(risky not in expressions, f"{function} warning does not expose {risky}")

airone_warning = issue_log_expressions(source("mqtt.py"), "extract_airone_reported")
r.ok("topic" not in airone_warning, "issue-directed Airone warnings do not expose a topic")

from navien_smarthome.coordinator import _key_map  # noqa: E402

identifier = "0011223344556677"
rendered_keys = _key_map({"Properties": {identifier: {"normalField": 1}}})
r.ok(identifier not in rendered_keys, "identifier-shaped response keys are redacted")
r.ok("<key:0>" in rendered_keys, "redacted response keys keep a stable structural marker")
r.ok("normalField" in rendered_keys, "ordinary response field names remain useful")

mixed_identifier = "SN12345678AB"
rendered_mixed = _key_map({"Properties": {mixed_identifier: {"normalField": 1}}})
r.ok(mixed_identifier not in rendered_mixed, "mixed base36-like response keys are redacted")

separated_identifier = "_".join(("SN1234", "5678ABCD"))
rendered_separated = _key_map(
    {"Properties": {separated_identifier: {"normalField": 1}}}
)
r.ok(
    separated_identifier not in rendered_separated,
    "separated base36-like response keys are redacted",
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
