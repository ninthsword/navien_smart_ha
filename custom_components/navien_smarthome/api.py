"""Navien Smart REST client.

Authentication happens in two stages.

1. A form POST to `member.naviensmartcontrol.com/member/login` — hold the cookies, follow
   the redirect, and scrape the token out of `var message = {...}` in the HTML.
2. `POST /users/secured-sign-in` — returns the home list and **temporary AWS IoT
   credentials**.

`accountSeq` is the `userSeq` from the first response, and differs from the
`userInfo.userSeq` in the second. They are easy to confuse, so the names are kept apart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import (
    AIRONE_LEGACY_TOPIC_FMT,
    AIRONE_TOPIC_FMT,
    API_URL,
    CODE_NOT_AUTHORIZED,
    CODE_SUCCESS,
    CODE_TOKEN_EXPIRED,
    LEGACY_CONTROLLER_TO_REQUEST,
    LEGACY_RUNNING_TO_REQUEST,
    LOGIN_URL,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_FAIL_POPUP = 'id="loginFailPopup" style="display:none;"'
_MISMATCH = "입력한 정보가 일치하지 않습니다."
_ATTEMPT_RE = re.compile(r"현재 (\d)회")
_MESSAGE_MARKER = "var message = "


def extract_airs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the list of air-quality items out of an `/air-sensor` response.

    The shape confirmed by a real-device report:
        `data.sensorList[]` → `{ zoneId, updateTime, airMonitor{}, airs[] }`

    The first guess was `data.airs`, and it **read nothing at all** — the reason air-quality
    sensors never appeared even with an air monitor attached. There can be several zones, so
    the whole list is walked, and the earlier guess stays as a fallback.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    airs: list[dict[str, Any]] = []
    for entry in data.get("sensorList") or []:
        if isinstance(entry, dict) and isinstance(entry.get("airs"), list):
            airs.extend(item for item in entry["airs"] if isinstance(item, dict))
    if airs:
        return airs

    if isinstance(data.get("airs"), list):
        return [item for item in data["airs"] if isinstance(item, dict)]
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("airs"), list):
            return [item for item in value["airs"] if isinstance(item, dict)]
    return []


class NavienSmartError(Exception):
    """Base exception for this integration."""


class NavienSmartAuthError(NavienSmartError):
    """The credentials are wrong, or the session became invalid."""


def _legacy_request(desired: dict[str, Any]) -> dict[str, Any]:
    """Translate a newer `desired` into the older `request` form.

    Callers know nothing about generations and build a single `roomController`. The envelope
    difference is absorbed here, just before sending, so generation branching never spreads
    into the models.
    """
    controller = desired.get("roomController")
    if not isinstance(controller, dict):
        return {}
    request: dict[str, Any] = {}
    running = controller.get("running")
    if running is not None:
        # The older generation inverts the running value; without flipping it, power goes out
        # backwards.
        request["power"] = LEGACY_RUNNING_TO_REQUEST.get(running, running)
    for source, target in LEGACY_CONTROLLER_TO_REQUEST.items():
        if source in controller:
            request[target] = controller[source]
    return request


class NavienSmartApiError(NavienSmartError):
    """The server returned a code other than success."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class AwsCredentials:
    """Temporary credentials for connecting to AWS IoT."""

    access_key_id: str
    secret_key: str
    session_token: str

    @classmethod
    def from_auth_info(cls, info: dict[str, Any]) -> AwsCredentials | None:
        try:
            return cls(info["accessKeyId"], info["secretKey"], info["sessionToken"])
        except KeyError:
            return None


@dataclass(slots=True)
class NavienSmartSession:
    """The result of logging in. `home_seq` may be overwritten with the user's choice."""

    access_token: str
    refresh_token: str | None
    user_id: str
    account_seq: int
    user_seq: int
    homes: list[dict[str, Any]]
    aws: AwsCredentials | None


class NavienSmartApi:
    """Owns the REST calls. MQTT belongs to `mqtt.py`."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
    ) -> None:
        self._http = session
        self._username = username
        self._password = password
        self._session: NavienSmartSession | None = None
        self._lock = asyncio.Lock()

    @property
    def session(self) -> NavienSmartSession | None:
        return self._session

    # -- authentication ----------------------------------------------------

    async def async_login(self) -> NavienSmartSession:
        """Run both stages and swap in the new session.

        **If a login that started earlier has finished, use it.** The server allows one
        session per account, so requests that failed at the same time would invalidate each
        other by each logging in. If the session already changed by the time the lock is
        acquired, that one is good enough.
        """
        seen = self._session
        async with self._lock:
            if self._session is not None and self._session is not seen:
                # Another request fetched a fresh one while this was waiting.
                return self._session
            login = await self._async_form_login()
            data = await self._async_secured_sign_in(
                login["accessToken"], login["loginId"], login["userSeq"]
            )

            homes = data.get("home") or []
            if not homes:
                raise NavienSmartAuthError("계정에 등록된 home 이 없습니다.")

            self._session = NavienSmartSession(
                access_token=login["accessToken"],
                refresh_token=login.get("refreshToken"),
                user_id=login["loginId"],
                account_seq=login["userSeq"],
                user_seq=data["userInfo"]["userSeq"],
                homes=homes,
                aws=AwsCredentials.from_auth_info(data.get("authInfo") or {}),
            )
            _LOGGER.debug(
                "로그인 완료: userSeq=%s home %s개",
                self._session.user_seq,
                len(homes),
            )
            return self._session

    async def _async_form_login(self) -> dict[str, Any]:
        """Form login. It needs a cookie session, so it takes a dedicated ClientSession."""
        try:
            async with self._http.post(
                f"{LOGIN_URL}/member/login",
                data={"username": self._username, "password": self._password},
                headers={
                    "User-Agent": USER_AGENT,
                    "Origin": LOGIN_URL,
                    "Referer": f"{LOGIN_URL}/member/login",
                },
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                html = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NavienSmartError(f"로그인 요청 실패: {err}") from err

        if _FAIL_POPUP in html:
            raise self._auth_error_from_html(html)

        if "passwordChg" in html:
            # The integration never issues requests that change account state (`/pwchgLate`).
            raise NavienSmartAuthError(
                "서버가 비밀번호 변경을 요구합니다. 앱이나 웹에서 먼저 처리해 주세요."
            )

        token_json = self._extract_message_json(html)
        if token_json is None:
            raise NavienSmartAuthError("로그인 응답에서 토큰을 찾지 못했습니다.")
        return token_json

    @staticmethod
    def _auth_error_from_html(html: str) -> NavienSmartAuthError:
        if _MISMATCH not in html:
            return NavienSmartAuthError("아이디가 올바르지 않습니다.")
        match = _ATTEMPT_RE.search(html)
        if match:
            return NavienSmartAuthError(
                f"비밀번호가 올바르지 않습니다. 5회 실패하면 재설정이 필요합니다 "
                f"(현재 {match.group(1)}회)."
            )
        return NavienSmartAuthError(
            "비밀번호가 올바르지 않습니다. 재설정이 필요할 수 있습니다."
        )

    @staticmethod
    def _extract_message_json(html: str) -> dict[str, Any] | None:
        for line in html.splitlines():
            if _MESSAGE_MARKER not in line:
                continue
            start = line.find("{")
            end = line.rfind("}")
            if start == -1 or end <= start:
                continue
            try:
                data = json.loads(line[start : end + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    async def _async_secured_sign_in(
        self, access_token: str, user_id: str, account_seq: int
    ) -> dict[str, Any]:
        payload = await self._async_request(
            "POST",
            "/users/secured-sign-in",
            token=access_token,
            json_body={"userId": user_id, "accountSeq": account_seq},
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise NavienSmartAuthError("secured-sign-in 응답에 data 가 없습니다.")
        return data

    async def async_refresh_aws_credentials(self) -> AwsCredentials | None:
        """Fetch fresh AWS credentials.

        `/auth/token/refresh` returns only an accessToken and no AWS credentials, so calling
        `secured-sign-in` again is the only path — confirmed against the live service.
        """
        session = self._require_session()
        data = await self._async_secured_sign_in(
            session.access_token, session.user_id, session.account_seq
        )
        session.aws = AwsCredentials.from_auth_info(data.get("authInfo") or {})
        return session.aws

    # -- requests ----------------------------------------------------------

    def _require_session(self) -> NavienSmartSession:
        if self._session is None:
            raise NavienSmartError("먼저 async_login() 을 호출해야 합니다.")
        return self._session

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        raw_body: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": token, "User-Agent": USER_AGENT}
        data: bytes | None = None
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
            data = raw_body.encode()
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode()

        try:
            async with self._http.request(
                method,
                f"{API_URL}{path}",
                params=params,
                headers=headers,
                data=data,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            # **Leaving `TimeoutError` out records nothing at all.** It is not a subclass of
            # `aiohttp.ClientError` (it descends from `OSError`), so it escaped this handler
            # entirely and neither the failure count nor a log line survived.
            raise NavienSmartError(f"{path} 요청 실패: {err}") from err

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise NavienSmartError(f"{path} 응답이 JSON 이 아닙니다.") from err

        # **Valid JSON is not necessarily an object.** `null`, arrays and numbers parse too.
        # Calling `.get` on those raises `AttributeError`, which is not one of our exceptions,
        # so it lands in neither the failure count nor the log and silently stops refreshing.
        if not isinstance(payload, dict):
            raise NavienSmartError(
                f"{path} 응답이 객체가 아닙니다 ({type(payload).__name__})."
            )

        code = payload.get("code")
        if code == CODE_SUCCESS:
            return payload
        raise NavienSmartApiError(code, payload.get("msg") or f"{path} 실패 (code={code})")

    async def _async_authed_request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """On an expired token or a stolen session, log in once more and retry.

        With one session per account, opening the app produces a `404`. That is common enough
        to recover from quietly.
        """
        session = self._require_session()
        try:
            return await self._async_request(method, path, token=session.access_token, **kwargs)
        except NavienSmartApiError as err:
            if err.code not in (CODE_TOKEN_EXPIRED, CODE_NOT_AUTHORIZED):
                raise
            _LOGGER.debug("세션 무효(code=%s) — 재로그인 후 재시도", err.code)
            home_seq = session.homes[0].get("homeSeq") if session.homes else None
            refreshed = await self.async_login()
            # Keep the home the user chose.
            if home_seq is not None:
                refreshed.homes.sort(key=lambda h: h.get("homeSeq") != home_seq)
            return await self._async_request(
                method, path, token=refreshed.access_token, **kwargs
            )

    # -- devices -----------------------------------------------------------

    async def async_get_devices(self, home_seq: int) -> list[dict[str, Any]]:
        session = self._require_session()
        payload = await self._async_authed_request(
            "GET",
            "/devices",
            params={"homeSeq": home_seq, "userSeq": session.user_seq},
        )
        return (payload.get("data") or {}).get("devices") or []

    async def async_control(
        self,
        home_seq: int,
        device: dict[str, Any],
        desired: dict[str, Any],
    ) -> None:
        """Relay a `desired` to the shadow.

        `event.modelCode` goes on every command. `beep` does not: the app attaches it only to
        models from 2024 onwards, and commands were confirmed to work without it on a real
        device.
        """
        session = self._require_session()
        device_seq = device["deviceSeq"]
        topic = f"$aws/things/{device['deviceId']}/shadow/name/status/update"

        body_obj = {
            "serviceCode": device["serviceCode"],
            "topic": "\x00TOPIC\x00",
            "payload": {
                "state": {
                    "desired": {
                        "event": {"modelCode": int(device["modelCode"])},
                        **desired,
                    }
                }
            },
        }
        # The app escapes '/' in the topic as '\/'. The server may be fussy, so match it.
        raw = json.dumps(body_obj, ensure_ascii=False).replace(
            '"\\u0000TOPIC\\u0000"', json.dumps(topic).replace("/", "\\/")
        )

        _LOGGER.debug("제어 전송 deviceSeq=%s desired=%s", device_seq, desired)
        await self._async_authed_request(
            "POST",
            f"/devices/{device_seq}/control",
            params={"homeSeq": home_seq, "userSeq": session.user_seq},
            raw_body=raw,
        )

    async def async_request_shadow(
        self, home_seq: int, device: dict[str, Any]
    ) -> None:
        """Ask the shadow for the **last state it has stored**.

        An AWS shadow holds the document the device last reported. Posting an empty body here
        makes the server return that document on `.../status/get/accepted`.

        **This is a read and changes nothing.** Unlike the older initial request, which used
        `desired`, it leaves no trace in the shadow.

        **A powered-off device still answers**, because the server answers, not the device.
        So the setpoints arrive immediately after a mat connects, even before the device has
        sent anything. Without it `climate` has no temperature to send and is blocked with
        "no zone value to send".

        **The app never publishes this topic.** It does carry code that handles
        `status/get/accepted`, and two real devices (one online, one offline) confirmed the
        server accepts it — the response came back with `code=200`.
        """
        session = self._require_session()
        device_seq = device["deviceSeq"]
        topic = f"$aws/things/{device['deviceId']}/shadow/name/status/get"

        body_obj = {
            "serviceCode": device["serviceCode"],
            "topic": "\x00TOPIC\x00",
            # **An empty body is the contract.** Putting a value in makes it something other
            # than a read.
            "payload": {},
        }
        raw = json.dumps(body_obj, ensure_ascii=False).replace(
            '"\\u0000TOPIC\\u0000"', json.dumps(topic).replace("/", "\\/")
        )

        _LOGGER.debug("섀도우 조회 deviceSeq=%s", device_seq)
        await self._async_authed_request(
            "POST",
            f"/devices/{device_seq}/control",
            params={"homeSeq": home_seq, "userSeq": session.user_seq},
            raw_body=raw,
        )

    # -- boiler ------------------------------------------------------------

    async def async_boiler_request(
        self,
        home_seq: int,
        device_seq: int,
        service_code: int,
        payload: dict[str, Any],
    ) -> None:
        """Relay the smarttok boiler envelope the app builds to the server."""
        session = self._require_session()
        body_obj = {"serviceCode": service_code, "payload": payload}
        raw = json.dumps(body_obj, ensure_ascii=False)
        # Match what the app sends, but keep identifying values out of the log.
        for key in ("requestTopic", "responseTopic"):
            value = payload.get(key)
            if isinstance(value, str):
                quoted = json.dumps(value)
                raw = raw.replace(quoted, quoted.replace("/", "\\/"))

        _LOGGER.debug("보일러 요청 전송 deviceSeq=%s", device_seq)
        await self._async_authed_request(
            "POST",
            f"/devices/{device_seq}/control",
            params={"homeSeq": home_seq, "userSeq": session.user_seq},
            raw_body=raw,
        )

    # -- Airone ------------------------------------------------------------

    async def async_airone_request(
        self,
        home_seq: int,
        device_seq: int,
        service_code: int,
        model_code: str,
        physical_device_id: str,
        command: str,
        client_id: str,
        desired: dict[str, Any] | None = None,
        legacy: bool = False,
    ) -> None:
        """Relay an Airone command.

        **The envelope differs from a mat.** A mat puts a single `topic` at the top level and
        `payload.state.desired` inside; Airone puts both the request and response topics in
        the envelope and pairs them with a `sessionId` (`AironePubComm`).

        A `desired` of `None` means a state query — `state` is then omitted entirely.
        """
        session = self._require_session()
        topic_fmt = AIRONE_LEGACY_TOPIC_FMT if legacy else AIRONE_TOPIC_FMT
        topic = topic_fmt.format(
            model_code=model_code, device_id=physical_device_id, command=command
        )
        payload: dict[str, Any] = {
            "clientId": client_id,
            # The app puts the epoch in milliseconds here as a string. The server uses it to
            # pair up the response.
            "sessionId": str(int(time.time() * 1000)),
            "requestTopic": topic,
            "responseTopic": f"{topic}/res",
        }
        if desired is not None:
            if legacy:
                payload["request"] = _legacy_request(desired)
            else:
                payload["state"] = {"desired": desired}

        body_obj = {"serviceCode": service_code, "payload": payload}
        # Escape '/' in the topic for the same reason as a mat.
        # **Do not run the substitution over the whole body**: `desired` is built by the
        # caller, so a '/' appearing there later would be silently corrupted. Only the two
        # topics are rewritten.
        raw = json.dumps(body_obj, ensure_ascii=False)
        for value in (payload["responseTopic"], topic):
            quoted = json.dumps(value)
            raw = raw.replace(quoted, quoted.replace("/", "\\/"))

        _LOGGER.debug(
            "에어원 전송 deviceSeq=%s command=%s desired=%s", device_seq, command, desired
        )
        await self._async_authed_request(
            "POST",
            f"/devices/{device_seq}/control",
            params={"homeSeq": home_seq, "userSeq": session.user_seq},
            raw_body=raw,
        )

    async def async_get_air_sensor(
        self, home_seq: int, device_seq: int
    ) -> list[dict[str, Any]]:
        """Read the air-quality values.

        A status message carries only the sensor **kinds**, not their values — the values
        exist only on this endpoint.
        """
        session = self._require_session()
        payload = await self._async_authed_request(
            "GET",
            f"/devices/{device_seq}/air-sensor",
            params={"homeSeq": home_seq, "userSeq": session.user_seq},
        )
        return extract_airs(payload)
