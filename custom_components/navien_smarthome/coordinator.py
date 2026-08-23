"""Combine the device list (REST) with live state (MQTT).

The long polling interval is not laziness: devices were confirmed on real hardware to push
their own state, so polling only handles **the initial sync after a reconnect and noticing
changes to the device list**.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AwsCredentials,
    NavienSmartApi,
    NavienSmartAuthError,
    NavienSmartError,
)
from homeassistant.helpers.storage import Store

from .airone import AironeDevice
from .boiler import (
    BOILER_GAS_METER_UPDATE,
    BOILER_GAS_REFRESH_SECONDS,
    BOILER_OBSERVATION_KEEP,
    BOILER_READBACK_DELAY_SECONDS,
    BOILER_SILENCE_REFRESH_SECONDS,
    BoilerDevice,
)
from .gas_statistics import async_import_gas_statistics
from .const import (
    AIRONE_AIR_ERROR_LOG_EVERY,
    AIRONE_CMD_CHANGE_MODE,
    AIRONE_CMD_POWER,
    AIRONE_CMD_STATUS,
    AIRONE_READBACK_DELAY_SECONDS,
    AIRONE_SENSOR_KINDS,
    AIRONE_SILENCE_CHECK_SECONDS,
    AIRONE_TOPIC_FMT,
    AIRONE_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    MIN_REFRESH_COOLDOWN_SECONDS,
    OUT_OF_SCOPE_REASONS,
    REPORT_WANTED_NOTES,
    REPORT_WANTED_SERVICE_CODES,
    SERVICE_AIRONE,
    SERVICE_BOILER,
    SERVICE_NAMES,
    SUPPORTED_SERVICE_CODES,
    TOPIC_PREFIX,
    UPDATE_INTERVAL_SECONDS,
)
from .models import NavienDevice
from .mqtt import NavienSmartMqtt

_LOGGER = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _key_map(raw: dict[str, Any], depth: int = 5) -> str:
    """Render only the key structure of a response. **No values** — those would leave personal data in the log."""

    def walk(value: Any, level: int) -> Any:
        if not isinstance(value, dict) or level <= 0:
            return "..." if isinstance(value, dict) else type(value).__name__
        return {key: walk(inner, level - 1) for key, inner in value.items()}

    return str(walk(raw.get("Properties"), depth))


class NavienSmartCoordinator(DataUpdateCoordinator[dict[str, NavienDevice]]):
    """`data` maps `deviceId` to `NavienDevice`."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: NavienSmartApi,
        home_seq: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
            config_entry=entry,
            # **The floor on how often anything outside can wake us.**
            #
            # HA defaults to 10 seconds (`REQUEST_REFRESH_DEFAULT_COOLDOWN`). Left there, a
            # single automation calling `homeassistant.update_entity` every 10 seconds would
            # hit Navien's servers 8,640 times a day from one account — more than an
            # unofficial client can carry, or should.
            #
            # `immediate=True` stays at its default: the first request is served at once and
            # only the flood behind it is coalesced, so "refresh now" keeps working.
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=MIN_REFRESH_COOLDOWN_SECONDS,
                immediate=True,
            ),
        )
        self.api = api
        self.home_seq = home_seq
        # The raw payload is kept even for unsupported devices, because the diagnostics export
        # needs it — it is the only evidence an Airone or boiler owner can attach to a report.
        self.raw_devices: list[dict[str, Any]] = []
        self.unsupported: list[dict[str, Any]] = []
        # Airone has a different state scheme from a mat and is never mixed into the same
        # dict. Leaving the verified mat path undisturbed comes first.
        self.airone: dict[str, AironeDevice] = {}
        # Boilers are controlled only through the confirmed modelCode=20 commands. Received
        # MQTT structures are de-identified and kept briefly, and confirmed state also reaches
        # the sensors.
        self.boilers: dict[str, BoilerDevice] = {}
        self.boiler_observations: list[dict[str, Any]] = []
        self._boiler_silence_unsubs: dict[str, Callable[[], None]] = {}
        self._boiler_gas_unsubs: dict[str, Callable[[], None]] = {}
        # **Even one-shot timers are held.** Waking after an unload or reload would have an
        # integration that no longer exists send a request to the server. The exception is
        # caught, but a pointless request still reaches an unofficial server, and it is
        # inconsistent with every other timer, which is managed.
        self._oneshot_unsubs: set[Callable[[], None]] = set()
        self.boiler_silence_requests = 0
        self.boiler_silence_failures = 0
        self.boiler_gas_requests = 0
        self.boiler_gas_failures = 0
        self.boiler_gas_statistics_failures = 0
        # **Overlapping statistics writes for one device corrupt the running total.** The
        # sequence is read the previous total, add, write; if the second read happens before
        # the first write, both start from the same value and both write the wrong one. That
        # can really happen when the initial request overlaps the hourly refresh, or when the
        # server returns the same response twice.
        self._gas_statistics_locks: dict[str, asyncio.Lock] = {}
        # The older generation never answers a `remote/status` request. The last state
        # received is stored and restored at startup — without it, power, mode and fan speed
        # all read as empty until the first command.
        self._store: Store = Store(hass, 1, f"{DOMAIN}.airone_state")
        # Devices whose state was restored. Diagnostics has to be able to tell whether a value
        # on screen came from a restore — power can read as on while the device is off.
        self.restored_devices: set[str] = set()
        # Nothing is written before what was stored has been read (`_async_remember_state`).
        self._state_restored = False
        # Counters that let diagnostics alone tell "no state arrives" from "it arrives and
        # cannot be attached". Nothing personal — counts and key names only.
        self.drop_counts: dict[str, int] = {
            "mate_no_device": 0,
            "airone_no_device": 0,
            "boiler_no_device": 0,
        }
        self._mqtt: NavienSmartMqtt | None = None
        self._skipped_logged: set[str] = set()
        # Records **whether polling is running at all.**
        #
        # In issue #1 the air-quality values sat unchanged for ten hours while both the
        # failure count and the empty-response count were 0. "The room was quiet" and "we never
        # read it at all" looked identical, because nothing recorded the polling side.
        self.poll_stamp: float | None = None
        self.poll_failures = 0
        # **Counting only `NavienSmartError` hides everything else.** In issue #1 polling ran
        # once after installation and stopped, with `poll_failures` at 0. Anything that is not
        # one of our exceptions — a `TypeError` from an unexpected response shape, say — left
        # no trace in the counter or in diagnostics. So every kind is counted and the last one
        # is kept. **Nothing personal: the exception name and message only.**
        self.poll_last_error: str | None = None
        # **Separates "called and failed" from "never called".**
        #
        # In issue #1 polling ran once after installation and stopped. The failure count was
        # 0, and although the reporter grepped their whole log for `navien`, the
        # `Error fetching ... data` line HA always emits on a failed refresh was **absent.**
        #
        # That leaves two possibilities: we died on an exception we do not catch, or **HA
        # never scheduled the next run.** The last success time cannot separate them; counting
        # the attempts themselves settles it in one line.
        #
        #   attempts rising  -> we are being called, and we are the ones failing
        #   attempts frozen   -> HA is not calling us; the problem is in scheduling
        self.poll_attempts = 0

    # -- collection --------------------------------------------------------

    async def _async_update_data(self) -> dict[str, NavienDevice]:
        self.poll_attempts += 1
        try:
            return await self._async_collect()
        except (NavienSmartAuthError, ConfigEntryAuthFailed, UpdateFailed):
            raise
        except Exception as err:
            # **Reaching here means something we did not anticipate.** Re-raising lets HA
            # catch it again, but leaves nothing at all in our diagnostics.
            self.poll_failures += 1
            self.poll_last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception("갱신 중 예상 못 한 오류가 났습니다")
            raise UpdateFailed(self.poll_last_error) from err

    async def _async_collect(self) -> dict[str, NavienDevice]:
        try:
            raw_devices = await self.api.async_get_devices(self.home_seq)
        except NavienSmartAuthError as err:
            self.poll_failures += 1
            self.poll_last_error = f"인증 실패: {err}"
            raise ConfigEntryAuthFailed(str(err)) from err
        except NavienSmartError as err:
            self.poll_failures += 1
            self.poll_last_error = f"기기목록 조회 실패: {err}"
            raise UpdateFailed(str(err)) from err

        previous = self.data or {}
        devices: dict[str, NavienDevice] = {}
        self.raw_devices = raw_devices
        self.unsupported = []

        previous_airone = self.airone
        airone: dict[str, AironeDevice] = {}
        previous_boilers = self.boilers
        boilers: dict[str, BoilerDevice] = {}

        for raw in raw_devices:
            # **An entry may not be a dict.** Calling `.get` on one then raises
            # `AttributeError`, which is not our exception, and the refresh stops silently.
            if not isinstance(raw, dict):
                _LOGGER.warning(
                    "기기 목록에 예상 못 한 항목이 있어 건너뜁니다 (%s)",
                    type(raw).__name__,
                )
                continue
            # Mats were confirmed to send an integer. Airone is not assumed to do the same: a
            # string would make the comparison fail silently and the device vanish entirely.
            service_code = _as_int(raw.get("serviceCode"))

            if service_code == SERVICE_BOILER:
                boiler = BoilerDevice.parse(raw)
                if boiler is None:
                    self._log_skip(
                        raw,
                        "응답에 deviceId·deviceSeq 가 없어 보일러를 만들지 못했습니다",
                    )
                    self.unsupported.append(raw)
                    continue
                if (old := previous_boilers.get(boiler.device_id)) is not None:
                    # Do not lose MQTT state when the REST response carries only `feature`.
                    if not boiler.status:
                        boiler.status = old.status
                    if boiler.physical_device_id is None:
                        boiler.physical_device_id = old.physical_device_id
                    boiler.status_received_at = old.status_received_at
                    boiler.last_communication_at = old.last_communication_at
                    boiler.gas_meter = old.gas_meter
                    boiler.gas_received_at = old.gas_received_at
                boilers[boiler.device_id] = boiler
                continue

            if service_code not in SUPPORTED_SERVICE_CODES:
                self.unsupported.append(raw)
                self._log_unsupported(raw)
                continue

            if service_code == SERVICE_AIRONE:
                parsed = self._parse_airone(raw, previous_airone)
                if parsed is not None:
                    airone[parsed.device_id] = parsed
                continue

            device = NavienDevice.parse(raw)
            if device is None:
                self._log_skip(raw, "응답에서 기기를 해석하지 못했습니다")
                continue

            control = device.heat_control
            if control is None:
                self._log_skip(
                    raw, "functions.heatControl 이 없어 난방 제어를 만들지 않습니다"
                )
            elif not control.is_known:
                # No command is guessed for an item whose value scheme is unknown.
                self._log_skip(
                    raw,
                    f"heatControl.unit '{control.unit}' 은 확인된 값이 아닙니다. "
                    "난방 제어 엔티티를 만들지 않고 건너뜁니다",
                )

            # Do not lose live state already received.
            #
            # **The records carry over too.** Device objects are rebuilt on every poll, and
            # anything left off the carry-over list silently resets to zero — which is exactly
            # how the diagnostics records were being wiped every interval (v0.9.5).
            if (old := previous.get(device.device_id)) is not None:
                device.reported = old.reported
                device.command_log = old.command_log
                device.state_log = old.state_log

            if device.is_four_season:
                self._log_four_season(device)
            if device.has_unknown_season:
                self._log_unknown_season(device)

            devices[device.device_id] = device

        self.airone = airone
        self.boilers = boilers
        self._tune_interval()
        await self._async_update_air_sensors()
        self.poll_stamp = time.monotonic()
        self.poll_failures = 0
        self.poll_last_error = None
        return devices

    def _tune_interval(self) -> None:
        """Shorten the polling interval when an Airone is present.

        Mat state arrives over MQTT, but **air quality can only be read over REST.** At the
        mat interval (15 minutes) the particulate readings are useless.
        """
        wanted = timedelta(
            seconds=(
                AIRONE_UPDATE_INTERVAL_SECONDS
                if self.airone
                else UPDATE_INTERVAL_SECONDS
            )
        )
        if self.update_interval != wanted:
            _LOGGER.debug("폴링 주기를 %s 로 바꿉니다", wanted)
            self.update_interval = wanted
        # **The floor follows along.** If outside callers can wake us faster than our own
        # polling interval, it is not a floor at all. `Debouncer.cooldown` is read when the
        # next timer is scheduled, so changing it here is safe.
        self._debounced_refresh.cooldown = wanted.total_seconds()

    def _parse_airone(
        self, raw: dict[str, Any], previous: dict[str, AironeDevice]
    ) -> AironeDevice | None:
        """Parse one Airone unit, logging the reason when it cannot be built."""
        device = AironeDevice.parse(raw)
        if device is None:
            self._log_skip(
                raw,
                "응답에 deviceId·deviceSeq 가 없어 기기를 만들지 못했습니다 "
                f"(Properties 구조: {_key_map(raw)})",
            )
            self.unsupported.append(raw)
            return None

        if _as_int(device.model_code) is None:
            # Do not lump this in as "older generation". Failing to read the value and being
            # an older device are different things.
            self._log_skip(
                raw,
                f"modelCode '{device.model_code}' 를 숫자로 읽지 못해 세대를 "
                "가릴 수 없습니다. 이 로그를 제보해 주세요",
            )
            self.unsupported.append(raw)
            return None

        if not device.modes:
            # **The device is still built.** Power, running state and errors come from the
            # status response and work without metadata; only the selection entities are
            # missing.
            self._log_skip(
                raw,
                "능력 메타데이터를 찾지 못해 운전 모드·풍량·목표 습도는 만들지 "
                "않습니다. 전원과 상태 엔티티는 만듭니다 "
                f"(찾은 곳: Properties.data.did.reported / 실제 구조: {_key_map(raw)}). "
                "이 로그를 제보해 주시면 바로 넓힐 수 있습니다",
            )

        # Do not lose live state or air-quality values already received.
        #
        # **Records and counters carry over too.** Missing this made the air-quality detection
        # added in v0.9.3 useless: a new object every poll meant the failure count could never
        # reach 3, and the 15-minute warning **could never fire at all.**
        if (old := previous.get(device.device_id)) is not None:
            device.reported = old.reported
            device.air_sensors = old.air_sensors
            device.sensor_kinds = old.sensor_kinds
            # **Restored kinds survive only through this line.** What is read from storage
            # lands in the object that existed at that moment, while polling builds a new one
            # every five minutes. Without this, the next poll narrows the kinds back down to
            # whatever is arriving right now, and that narrowed set overwrites the stored copy
            # so the sensors disappear again on restart — the restore would be pointless five
            # minutes after it ran.
            device.known_sensor_kinds = old.known_sensor_kinds
            device.last_humidity = old.last_humidity
            device.command_log = old.command_log
            device.humidity_log = old.humidity_log
            device.air_sensor_stamp = old.air_sensor_stamp
            device.air_sensor_empty = old.air_sensor_empty
            device.air_sensor_errors = old.air_sensor_errors
            device.air_sensor_unchanged = old.air_sensor_unchanged

        self._log_airone_found(device)
        return device

    def _log_airone_found(self, device: AironeDevice) -> None:
        """Announce that an Airone was found, once.

        **v0.9.3 emitted this as a `WARNING`**, saying it was unverified on real hardware and
        might not work or might show odd values. Two things were wrong with that.

        1. **It was not true.** State, control and target humidity had been confirmed by
           reports.
        2. **`WARNING` is for problems.** The HA log screen shows warnings and above by
           default, so a reporter took this line for an error and attached it to an issue — it
           manufactured worry where nothing was wrong.

        The list of models keeps growing, which is why no list of verified models goes in the
        source. This records what was found and invites a report if anything looks wrong.
        """
        key = f"{device.device_seq}:airone_found"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        _LOGGER.info(
            "환기청정을 찾았습니다 (%s, modelCode=%s). 운전 모드 %d가지를 서버 "
            "정보에서 찾았습니다. 값이 앱과 다르거나 조작이 안 되면 이슈로 "
            "알려 주세요.",
            device.nickname,
            device.model_code,
            len(device.selectable_modes),
        )

    async def _async_update_air_sensors(self) -> None:
        """Read the air-quality values.

        A status message carries only the sensor kinds, not their values — those exist only on
        `/air-sensor`.
        """
        for device in self.airone.values():
            if not device.available:
                continue
            if not device.wants_air_sensors:
                # **The device declared it has no sensors.** Asking returns nothing but empty
                # responses. Skipping removes one pointless request every five minutes, and
                # with it one place polling can fail. Fitting an air monitor later shows up in
                # the device list and asking resumes.
                self._log_no_air_sensors(device)
                continue
            try:
                airs = await self.api.async_get_air_sensor(
                    self.home_seq, device.device_seq
                )
            except NavienSmartError as err:
                device.air_sensor_errors += 1
                # **Never passed over silently.** Since an empty response stopped clearing
                # values, a query that keeps failing leaves the old numbers on screen. All the
                # user sees is that HA disagrees with the app, with no way to know why.
                if device.air_sensor_errors % AIRONE_AIR_ERROR_LOG_EVERY == 0:
                    _LOGGER.warning(
                        "%s 공기질을 %d회 연속 못 읽었습니다. 화면에 남아 있는 값은 "
                        "그 전에 받은 것입니다 (%s)",
                        device.nickname,
                        device.air_sensor_errors,
                        err,
                    )
                else:
                    _LOGGER.debug("%s 공기질 조회 실패: %s", device.nickname, err)
                continue
            device.air_sensor_errors = 0
            before = device.known_sensor_kinds
            unknown = device.set_air_sensors(airs)
            # Persist immediately when the known kinds grow. Kinds are only updated by this
            # query while saving only happened on an MQTT report, so a newly seen kind never
            # reached disk. A restart then restored only the old kinds and the returned item
            # stayed without an entity — which is what happened on a real device.
            if device.known_sensor_kinds != before:
                self._async_remember_state()
            # Report an item that used to arrive and no longer does. No error code is sent and
            # the query still succeeds, so without this line a disconnected air monitor is
            # undetectable.
            missing = [
                kind for kind in device.known_sensor_kinds
                if kind not in device.sensor_kinds
            ]
            if missing:
                self._log_air_sensors_missing(device, missing)
            if unknown:
                self._log_skip(
                    device.raw,
                    "확인되지 않은 공기질 항목은 만들지 않습니다: "
                    + ", ".join(sorted(set(unknown))),
                )

    def _log_air_sensors_missing(
        self, device: AironeDevice, missing: list[str]
    ) -> None:
        """Report missing air-quality items **once per distinct set of missing kinds.**

        When the link between the air monitor and the room controller drops, the server sends
        only temperature and humidity. There is no error code and the query succeeds, so
        without a log line it cannot be noticed. A changed set is reported again, so further
        losses and returns stay visible.
        """
        key = f"{device.device_id}:air-missing:{','.join(missing)}"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        _LOGGER.warning(
            "%s 에서 전에 받던 공기질 항목이 오지 않습니다: %s. "
            "에어모니터와 룸콘 사이 통신을 확인해 주세요 — 값이 돌아오면 "
            "센서도 함께 돌아옵니다",
            device.nickname,
            ", ".join(AIRONE_SENSOR_KINDS[k][0] for k in missing),
        )

    def _log_no_air_sensors(self, device: AironeDevice) -> None:
        """Announce **once** that air quality will not be queried.

        Skipping silently leaves nothing to answer "why are there no air-quality entities"
        with. If the judgement was wrong, this line comes back in a report.
        """
        key = f"{device.device_id}:no-air-sensors"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        _LOGGER.info(
            "%s 는 공기질 센서를 갖고 있지 않다고 알려왔습니다 "
            "(룸콘 센서 목록 비어 있음, 에어모니터 없음). 공기질을 조회하지 "
            "않습니다. 앱에는 공기질이 보이는데 HA 에 없다면 제보해 주세요",
            device.nickname,
        )

    def _log_unsupported(self, raw: dict[str, Any]) -> None:
        """Explain why an unsupported device was skipped.

        Dropping it silently makes the user think the integration is broken. The reporting
        route is offered only for devices worth a report; out-of-scope devices are not given
        false hope.
        """
        service_code = raw.get("serviceCode")
        key = f"{raw.get('deviceSeq')}:unsupported"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        name = SERVICE_NAMES.get(service_code, f"serviceCode {service_code}")

        if service_code in REPORT_WANTED_SERVICE_CODES:
            _LOGGER.warning(
                "%s 를 찾았습니다 (modelName=%s). 아직 지원하지 않습니다 — %s. "
                "지원을 원하시면 설정 → 기기 및 서비스 → 나비엔 스마트 → "
                "⋮ 메뉴의 '통계정보 다운로드' 를 이슈에 붙여 주세요. "
                "기기ID·IP·MAC·별칭은 자동으로 가려집니다.",
                name,
                raw.get("modelName"),
                REPORT_WANTED_NOTES.get(service_code, "실기기 정보가 필요합니다"),
            )
            return

        _LOGGER.info(
            "%s (modelName=%s) 는 건너뜁니다 — %s.",
            name,
            raw.get("modelName"),
            OUT_OF_SCOPE_REASONS.get(service_code, "이 통합의 범위가 아닙니다"),
        )

    def _log_four_season(self, device: NavienDevice) -> None:
        """Announce a four-season device once.

        Heating works as it is. The cooling value scheme is unconfirmed, so control is left
        disabled in that band only. A report carrying the values below is what opens cooling.
        """
        key = f"{device.device_seq}:four_season"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        cool = device.cool_control
        _LOGGER.info(
            "사계절 모델을 찾았습니다 (%s, modelCode=%s). 냉방(COOL) 범위는 "
            "%s~%s 입니다. 앱에서 COOL 로 바꾸시면 HA 도 그 범위로 따라갑니다 — "
            "**냉방에서는 좌우가 같은 온도로 동작하므로** 어느 쪽을 조작해도 "
            "양쪽에 같은 값이 갑니다.",
            device.nickname,
            device.model_code,
            cool.range_min if cool else "?",
            cool.range_max if cool else "?",
        )

    def _log_unknown_season(self, device: NavienDevice) -> None:
        """Announce once when `season` holds a value we do not recognise.

        The app constants are only WARM (0) and COOL (2), while the spec sheet also names a
        `Cool+`. **An unrecognised value falls back to heating and is reported** — safer than
        applying the cooling range by mistake.
        """
        key = f"{device.device_seq}:season:{device.season}"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        _LOGGER.warning(
            "%s 의 season 값 %s 를 해석하지 못해 난방으로 다룹니다 "
            "(아는 값: 0 난방 / 2 냉방). 냉방 중이신데 이 로그가 보이면 "
            "이 줄과 통계정보를 이슈에 붙여 주세요 — 바로 넓힐 수 있습니다.",
            device.nickname,
            device.season,
        )

    def _log_skip(self, raw: dict[str, Any], reason: str) -> None:
        """Record what was skipped in the installation log. Nothing is dropped silently."""
        key = f"{raw.get('deviceSeq')}:{reason}"
        if key in self._skipped_logged:
            return
        self._skipped_logged.add(key)
        _LOGGER.warning(
            "기기 건너뜀 (deviceSeq=%s, modelName=%s): %s",
            raw.get("deviceSeq"),
            raw.get("modelName"),
            reason,
        )

    # -- live state --------------------------------------------------------

    async def async_start_mqtt(self) -> None:
        prefixes = {
            prefix
            for device in (self.data or {}).values()
            if (prefix := TOPIC_PREFIX.get(device.service_code))
        }
        if self.airone and (prefix := TOPIC_PREFIX.get(SERVICE_AIRONE)):
            prefixes.add(prefix)
        # Boilers live in their own dict rather than the mat `data`, so the subscription is
        # driven from the raw list. Read-only; no command is sent.
        if any(
            _as_int(raw.get("serviceCode")) == SERVICE_BOILER
            for raw in self.raw_devices
            if isinstance(raw, dict)
        ) and (prefix := TOPIC_PREFIX.get(SERVICE_BOILER)):
            prefixes.add(prefix)
        if not prefixes:
            _LOGGER.debug("구독할 기기가 없어 MQTT 를 시작하지 않습니다")
            return

        session = self.api.session
        self._mqtt = NavienSmartMqtt(
            self.hass,
            home_seq=self.home_seq,
            user_seq=session.user_seq if session else self.home_seq,
            topic_prefixes=prefixes,
            credentials_provider=self._async_aws_credentials,
            on_reported=self._handle_reported,
            on_subscribed=self._async_request_initial_state,
            on_airone_reported=self._handle_airone_reported,
            on_boiler_reported=self._handle_boiler_reported,
            on_boiler_observation=self._handle_boiler_observation,
        )
        await self._mqtt.async_start()

    async def _async_request_initial_state(self) -> None:
        """Ask powered-on devices to push their state.

        Shadow events only arrive **when something changes**, so a bare subscription leaves
        the state empty for as long as nobody touches the device — the entities stay `unknown`.

        Sending `event.modelCode` alone, with no control fields, makes the device push its
        current state as `reported`. The app does the same.
        **Nothing is changed** — there is no value to change.

        A powered-off device is not asked: it never answers and the request just piles up in
        the shadow.

        **That is why the shadow is read first.** It asks for the last document stored on the
        server, so **even a powered-off device answers.** The setpoints appear immediately
        after connecting, and `climate` no longer gets blocked with "no zone value to send".
        """
        for device in (self.data or {}).values():
            # **Read what is stored first.** Online or offline makes no difference, because
            # the server answers rather than the device. Confirmed on two real devices.
            try:
                await self.api.async_request_shadow(self.home_seq, device.raw)
                _LOGGER.debug("%s 섀도우를 조회했습니다", device.nickname)
            except NavienSmartError as err:
                # A failure still leaves the request below; it simply behaves as v0.13.x did.
                _LOGGER.debug("%s 섀도우 조회 실패: %s", device.nickname, err)

            if not device.available:
                _LOGGER.debug("%s 는 오프라인이라 초기 상태를 요청하지 않습니다", device.nickname)
                continue
            try:
                await self.api.async_control(self.home_seq, device.raw, {})
                _LOGGER.debug("%s 에 초기 상태를 요청했습니다", device.nickname)
            except NavienSmartError as err:
                _LOGGER.warning("%s 초기 상태 요청 실패: %s", device.nickname, err)

        for airone in self.airone.values():
            if not airone.available:
                _LOGGER.debug("%s 는 오프라인이라 상태를 요청하지 않습니다", airone.nickname)
                continue
            try:
                await self._async_airone_request(airone, AIRONE_CMD_STATUS, None)
                _LOGGER.debug("%s 에 초기 상태를 요청했습니다", airone.nickname)
            except NavienSmartError as err:
                _LOGGER.warning("%s 초기 상태 요청 실패: %s", airone.nickname, err)
                continue
            self._schedule_airone_silence_check(airone)

        # A boiler also gets the same read request as the app's getDeviceStatus once subscribed.
        # Without it the setpoint numbers stay unavailable and the running-state sensor has no
        # value until the first spontaneous report after a restart. Some connections do not
        # answer status/start alone, so a plain status request follows it once.
        for boiler in self.boilers.values():
            if not boiler.connected:
                _LOGGER.debug("%s 는 오프라인이라 상태를 요청하지 않습니다", boiler.nickname)
                continue
            try:
                payload = boiler.build_start_payload(self._boiler_client_id())
                await self._async_send_boiler_payload(boiler, payload)
                self._schedule_boiler_readback(boiler)
                _LOGGER.debug("%s 에 초기 상태를 요청했습니다", boiler.nickname)
            except (HomeAssistantError, NavienSmartError) as err:
                _LOGGER.warning("%s 초기 상태 요청 실패: %s", boiler.nickname, err)
            if boiler.supports_feature("gasUsageUse"):
                try:
                    await self._async_request_boiler_gas(boiler)
                except (HomeAssistantError, NavienSmartError) as err:
                    self.boiler_gas_failures += 1
                    _LOGGER.debug("%s 초기 가스 사용량 조회 실패: %s", boiler.nickname, err)
                self._schedule_boiler_gas_refresh(boiler)

    @callback
    def _track_oneshot(self, unsub: Callable[[], None]) -> Callable[[], None]:
        """Hold a one-shot timer, and let it release itself once it fires."""
        self._oneshot_unsubs.add(unsub)
        return unsub

    async def async_stop_mqtt(self) -> None:
        for unsub in self._boiler_silence_unsubs.values():
            unsub()
        self._boiler_silence_unsubs.clear()
        for unsub in self._boiler_gas_unsubs.values():
            unsub()
        self._boiler_gas_unsubs.clear()
        for unsub in tuple(self._oneshot_unsubs):
            unsub()
        self._oneshot_unsubs.clear()
        if self._mqtt is not None:
            await self._mqtt.async_stop()
            self._mqtt = None

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt is not None and self._mqtt.connected

    @property
    def poll_age(self) -> float | None:
        """Seconds since the last poll that **ran through to success**."""
        if self.poll_stamp is None:
            return None
        return round(time.monotonic() - self.poll_stamp, 1)

    @property
    def mqtt_stats(self) -> dict[str, Any]:
        """Received and discarded counts, so diagnostics can settle it without any logging."""
        stats: dict[str, Any] = dict(self._mqtt.stats) if self._mqtt else {}
        stats.update(self.drop_counts)
        return stats

    async def _async_aws_credentials(self) -> AwsCredentials | None:
        """Fetch fresh credentials on every connect and reconnect.

        `/auth/token/refresh` returns no AWS credentials, so calling `secured-sign-in` again
        is the only path.
        """
        try:
            return await self.api.async_refresh_aws_credentials()
        except NavienSmartAuthError:
            session = await self.api.async_login()
            return session.aws

    @callback
    def _handle_reported(self, device_id: str, reported: dict[str, Any]) -> None:
        """Apply a `reported` that arrived over MQTT. Called on the HA event loop."""
        devices = self.data or {}
        device = devices.get(device_id)
        if device is None:
            # This may be a newly registered device; the next poll picks it up.
            self.drop_counts["mate_no_device"] += 1
            _LOGGER.debug("모르는 기기의 보고 무시: %s", device_id)
            return
        # **Never overwritten.** Four-season models send partial responses (`apply_reported`).
        device.apply_reported(reported)
        self._async_push_update(devices)

    @callback
    def _handle_airone_reported(self, device_id: str, reported: dict[str, Any]) -> None:
        """Apply Airone state. Called on the HA event loop.

        The `deviceId` in the device list can differ from `did.roomController.deviceId`, so
        both are searched.
        """
        device = self.airone.get(device_id)
        if device is None:
            device = next(
                (d for d in self.airone.values() if d.physical_device_id == device_id),
                None,
            )
        if device is None:
            self.drop_counts["airone_no_device"] += 1
            _LOGGER.debug("모르는 에어원의 보고 무시: %s", device_id)
            return
        # **Never overwritten.** Command responses arrive as partial payloads (see the
        # comment on `apply_reported`).
        device.apply_reported(reported)
        # A value the device actually pushed has arrived, so this is no longer a restore.
        self.restored_devices.discard(device.device_id)
        self._async_remember_state()
        self._async_push_update(self.data or {})

    @callback
    def _handle_boiler_observation(self, observation: dict[str, Any]) -> None:
        """Keep at most eight de-identified observation records."""
        self.boiler_observations.append(observation)
        del self.boiler_observations[:-BOILER_OBSERVATION_KEEP]

    @callback
    def _handle_boiler_reported(
        self, physical_id: str, status: dict[str, Any]
    ) -> None:
        """Attach MQTT boiler state to the matching REST device."""
        device = next(
            (
                boiler
                for boiler in self.boilers.values()
                if boiler.physical_device_id == physical_id
            ),
            None,
        )
        # With exactly one boiler, and a model whose REST response omits macAddress, the match
        # can be made safely.
        if device is None and len(self.boilers) == 1:
            device = next(iter(self.boilers.values()))
            device.physical_device_id = physical_id
        if device is None:
            self.drop_counts["boiler_no_device"] += 1
            return
        is_gas_update = BOILER_GAS_METER_UPDATE in status
        device.apply_status(status)
        self._schedule_boiler_silence_check(device)
        if is_gas_update:
            self._schedule_boiler_gas_refresh(device)
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_import_gas_statistics(device),
                f"navien gas statistics {device.device_seq}",
            )
        self.last_update_success = True
        self.async_update_listeners()

    @callback
    def _async_push_update(self, data: dict[str, NavienDevice]) -> None:
        """Notify entities of state received over MQTT. **The polling schedule is left alone.**

        This used to call `async_set_updated_data()`, which starved polling. In HA's own
        source that function does this:

            def async_set_updated_data(self, data):
                self._async_unsub_refresh()        # cancels the scheduled next poll
                self._debounced_refresh.async_cancel()
                ...
                if self._listeners:
                    self._schedule_refresh()       # and starts counting again from now

        **Every message pushes the next poll five minutes out.** An Airone reports roughly
        every 46 seconds, so the 300-second timer never fires. A mat reports six times in 19
        hours and was unaffected — which is why air quality froze only for Airone owners
        (#1, #12, #13).

        `async_update_listeners()` only notifies the entities and never touches the schedule,
        so that is all this uses.
        """
        self.data = data
        # Fresh values arrived over MQTT, so the entities stay alive. Everything matches
        # `async_set_updated_data` except that the schedule is not touched.
        self.last_update_success = True
        self.async_update_listeners()

    def _async_remember_state(self) -> None:
        """Persist the last state. A failure here never blocks anything.

        There are two stored shapes. It began as `{device: reported}` and now writes
        `{device: {"reported": ..., "air_kinds": [...]}}` so the air-quality kinds are kept
        alongside. **Both are accepted when reading** — telling them apart by shape is easier
        to undo than bumping a storage version and adding a migration.
        """
        if not self._state_restored:
            # **Nothing is written before the restore runs.** A snapshot holds only what is in
            # memory, and the first poll runs before the restore. Writing then destroys the
            # `reported` that has not been read yet, and the restore that follows reads back
            # what we just erased — on a real device that turned 17 room-controller values into
            # unknown.
            return
        snapshot: dict[str, Any] = {}
        for device_id, device in self.airone.items():
            entry: dict[str, Any] = {}
            if device.reported:
                entry["reported"] = device.reported
            if device.known_sensor_kinds:
                entry["air_kinds"] = list(device.known_sensor_kinds)
            if entry:
                snapshot[device_id] = entry
        if snapshot:
            self._store.async_delay_save(lambda: snapshot, 5)

    async def async_restore_state(self) -> None:
        """Restore the last stored state.

        **A restored value is provisional.** It is overwritten as soon as the device pushes
        something or the user acts. Still, the last known value beats showing nothing at all.
        """
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a storage problem must not block the integration
            _LOGGER.debug("에어원 상태 복원 실패: %s", err)
            self._state_restored = True
            return
        if not isinstance(stored, dict):
            self._state_restored = True
            return
        for device_id, entry in stored.items():
            device = self.airone.get(device_id)
            if device is None or not isinstance(entry, dict):
                continue
            # The old shape held `reported` directly; the new one nests it one level deeper.
            if "reported" in entry or "air_kinds" in entry:
                reported = entry.get("reported")
                kinds = entry.get("air_kinds")
            else:
                reported, kinds = entry, None
            if isinstance(kinds, list):
                device.remember_sensor_kinds(kinds)
            if isinstance(reported, dict) and reported and not device.reported:
                device.apply_reported(reported)
                self.restored_devices.add(device_id)
        if self.restored_devices:
            _LOGGER.debug(
                "에어원 %s대의 마지막 상태를 되살렸습니다. 기기가 새로 올리기 전까지는 "
                "잠정값입니다", len(self.restored_devices)
            )
        self._state_restored = True
        # Air-quality kinds learned during the first poll have not been persisted yet, because
        # of the rule above. Writing once here is what carries them across a restart.
        self._async_remember_state()

    # -- control -----------------------------------------------------------

    def _boiler_client_id(self) -> str:
        client_id = self._mqtt.client_id if self._mqtt is not None else ""
        if not client_id:
            raise HomeAssistantError(
                "보일러 MQTT 연결이 준비되지 않아 명령을 보낼 수 없습니다."
            )
        return client_id

    async def _async_send_boiler_payload(
        self, device: BoilerDevice, payload: dict[str, Any]
    ) -> None:
        try:
            await self.api.async_boiler_request(
                self.home_seq,
                device_seq=device.device_seq,
                service_code=SERVICE_BOILER,
                payload=payload,
            )
            device.note_communication()
            self._schedule_boiler_silence_check(device)
        except NavienSmartAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

    async def async_boiler_temperature(
        self, device: BoilerDevice, kind: str, target: float
    ) -> None:
        """Change one setpoint, regardless of whether the boiler is currently heating."""
        current = self.boilers.get(device.device_id) or device
        try:
            payload = current.build_temperature_payload(
                kind, target, self._boiler_client_id()
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_send_boiler_payload(current, payload)
        self._schedule_boiler_readback(current)

    async def async_boiler_power(
        self, device: BoilerDevice, turn_on: bool
    ) -> None:
        """Switch NR-67D power with the same command as the app, then verify the real state."""
        current = self.boilers.get(device.device_id) or device
        try:
            payload = current.build_power_payload(turn_on, self._boiler_client_id())
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_send_boiler_payload(current, payload)
        self._schedule_boiler_readback(current)

    async def async_boiler_switch(
        self, device: BoilerDevice, kind: str, turn_on: bool
    ) -> None:
        """Set fast hot water, smart operation or turbo hot water, then verify the real state."""
        current = self.boilers.get(device.device_id) or device
        try:
            payload = current.build_switch_payload(
                kind, turn_on, self._boiler_client_id()
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_send_boiler_payload(current, payload)
        self._schedule_boiler_readback(current)

    async def _async_request_boiler_gas(self, device: BoilerDevice) -> None:
        """The monthly total moves slowly, so the app's request is sent only once an hour."""
        try:
            payload = device.build_gas_payload(self._boiler_client_id())
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_send_boiler_payload(device, payload)
        self.boiler_gas_requests += 1

    async def _async_import_gas_statistics(self, device: BoilerDevice) -> None:
        """Write gas history into long-term statistics. A failure never blocks a state update.

        **One run at a time per device**, because overlapping runs corrupt the running total
        (see the comment on `_gas_statistics_locks`). Waiting is preferred over dropping: a
        dropped run could leave the last response unapplied, and since the work rewrites the
        same data, running once more is cheap.
        """
        lock = self._gas_statistics_locks.setdefault(device.device_id, asyncio.Lock())
        async with lock:
            try:
                await async_import_gas_statistics(self.hass, device)
            except Exception:
                self.boiler_gas_statistics_failures += 1
                _LOGGER.exception("가스 장기 통계를 반영하지 못했습니다")

    @callback
    def _schedule_boiler_gas_refresh(self, device: BoilerDevice) -> None:
        """Refresh the gas totals hourly — more conservative than the app's own screen."""
        device_id = device.device_id
        if old := self._boiler_gas_unsubs.pop(device_id, None):
            old()

        async def _refresh(_now: Any) -> None:
            self._boiler_gas_unsubs.pop(device_id, None)
            target = self.boilers.get(device_id)
            if target is None or not target.supports_feature("gasUsageUse"):
                return
            if target.connected and self.mqtt_connected:
                try:
                    await self._async_request_boiler_gas(target)
                except (HomeAssistantError, NavienSmartError) as err:
                    self.boiler_gas_failures += 1
                    _LOGGER.debug("%s 가스 사용량 조회 실패: %s", target.nickname, err)
            self._schedule_boiler_gas_refresh(target)

        self._boiler_gas_unsubs[device_id] = async_call_later(
            self.hass, BOILER_GAS_REFRESH_SECONDS, _refresh
        )

    @callback
    def _schedule_boiler_readback(self, device: BoilerDevice) -> None:
        """Re-read the device state after a command. Nothing is changed optimistically."""
        device_id = device.device_id

        holder: list[Callable[[], None]] = []

        async def _readback(_now: Any) -> None:
            if holder:
                self._oneshot_unsubs.discard(holder[0])
            target = self.boilers.get(device_id)
            if target is None:
                return
            try:
                payload = target.build_status_payload(self._boiler_client_id())
                await self._async_send_boiler_payload(target, payload)
            except (HomeAssistantError, NavienSmartError) as err:
                _LOGGER.debug("%s 보일러 상태 재확인 실패: %s", target.nickname, err)

        holder.append(
            self._track_oneshot(
                async_call_later(self.hass, BOILER_READBACK_DELAY_SECONDS, _readback)
            )
        )

    @callback
    def _schedule_boiler_silence_check(
        self, device: BoilerDevice, *, delay: float | None = None
    ) -> None:
        """Request status once when five minutes pass with no traffic either way.

        A state change arriving over MQTT pushes this timer five minutes out again, so nothing
        is polled while the boiler is reporting actively. Even with no request or response, a
        failure is not retried immediately but waits another five minutes, so an unofficial
        server is never pressed.
        """
        device_id = device.device_id
        if old := self._boiler_silence_unsubs.pop(device_id, None):
            old()
        wait = device.silence_refresh_delay() if delay is None else max(1.0, delay)

        async def _check(_now: Any) -> None:
            self._boiler_silence_unsubs.pop(device_id, None)
            target = self.boilers.get(device_id)
            if target is None:
                return
            age = target.communication_age()
            if age is not None and age < BOILER_SILENCE_REFRESH_SECONDS:
                self._schedule_boiler_silence_check(target)
                return
            if not target.connected or not self.mqtt_connected:
                self._schedule_boiler_silence_check(
                    target, delay=BOILER_SILENCE_REFRESH_SECONDS
                )
                return
            try:
                payload = target.build_status_payload(self._boiler_client_id())
                await self._async_send_boiler_payload(target, payload)
                self.boiler_silence_requests += 1
            except (HomeAssistantError, NavienSmartError) as err:
                self.boiler_silence_failures += 1
                _LOGGER.debug("%s 보일러 5분 무통신 상태 요청 실패: %s", target.nickname, err)
                self._schedule_boiler_silence_check(
                    target, delay=BOILER_SILENCE_REFRESH_SECONDS
                )

        self._boiler_silence_unsubs[device_id] = async_call_later(
            self.hass, wait, _check
        )

    async def _async_airone_request(
        self,
        device: AironeDevice,
        command: str,
        desired: dict[str, Any] | None,
    ) -> None:
        client_id = self._mqtt.client_id if self._mqtt is not None else ""
        # Record what was sent. Some problems can only be settled by seeing the order in
        # diagnostics.
        device.note_command(command, desired)
        await self.api.async_airone_request(
            self.home_seq,
            device_seq=device.device_seq,
            service_code=device.service_code,
            model_code=device.model_code,
            physical_device_id=device.physical_device_id,
            command=command,
            client_id=client_id,
            desired=desired,
            # Generation differences are absorbed in the transport layer alone; nothing above
            # it knows about generations.
            legacy=not device.is_v2_generation,
        )

    async def async_airone_power(self, device: AironeDevice, turn_on: bool) -> None:
        try:
            await self._async_airone_request(
                device, AIRONE_CMD_POWER, device.build_power_desired(turn_on)
            )
        except NavienSmartAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        self._schedule_airone_readback(device)

    async def async_airone_mode(
        self,
        device: AironeDevice,
        mode: int,
        option: int,
        air_volume: int | None = None,
        humidity: int | None = None,
    ) -> None:
        # **The mechanism that re-sent the target humidity later was removed in v0.9.1.**
        #
        # v0.9.0 added it on the theory that re-sending after entering the mode might work.
        # A real-device report exposed two things.
        #
        # 1. **The device never echoes the target humidity back as state.** All eight
        #    observations were empty, so whether it had been reverted could not be judged, the
        #    re-send was forever treated as unsuccessful, and one more went out on every mode
        #    change.
        # 2. **It overwrote the user.** The test compared `mode` and forgot `option`, so
        #    changing only the fan speed within dehumidify was undone eight seconds later. The
        #    report records the exact moment turbo was dragged back to the base speed.
        #
        # Nothing is fired repeatedly without evidence. The humidity rides along with the mode
        # change exactly once, and whether the device accepts it is judged from the diagnostics
        # records.
        desired = device.build_mode_desired(mode, option, air_volume, humidity)
        try:
            await self._async_airone_request(device, AIRONE_CMD_CHANGE_MODE, desired)
        except NavienSmartAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        self._schedule_airone_readback(device)

    @callback
    def _schedule_airone_silence_check(self, device: AironeDevice) -> None:
        """Report a status request that was never answered.

        **A silent failure is the hardest kind to catch.** When the request succeeds (HTTP
        200) and no response arrives, the entities stay unknown forever and the user thinks
        the integration is broken. Only a log line saying how far it got makes a report
        conclusive.
        """
        device_id = device.device_id
        holder: list[Callable[[], None]] = []

        async def _check(_now: Any) -> None:
            if holder:
                self._oneshot_unsubs.discard(holder[0])
            target = self.airone.get(device_id)
            if target is None or target.reported:
                return
            key = f"{target.device_seq}:silent"
            if key in self._skipped_logged:
                return
            self._skipped_logged.add(key)
            prefix = TOPIC_PREFIX.get(SERVICE_AIRONE)
            # **Logged without identifiers.** This warning asks the user to attach it to an
            # issue, and the topic carried the device's physical id (derived from its MAC) and
            # the homeSeq. The diagnostics export redacts those very values, so the two
            # disagreed. The **shape** of the topic is enough to narrow down the cause.
            _LOGGER.warning(
                "%s 에 상태를 요청했지만 %d초 안에 응답이 오지 않았습니다. "
                "요청은 정상 전송됐습니다 — 보낸 곳: %s, 듣는 곳: **REDACTED**/%s/#. "
                "엔티티가 「알 수 없음」으로 남습니다. 이 로그와 통계정보를 "
                "이슈에 붙여 주시면 원인을 좁힐 수 있습니다.",
                target.nickname,
                AIRONE_SILENCE_CHECK_SECONDS,
                AIRONE_TOPIC_FMT.format(
                    model_code=target.model_code,
                    device_id="**REDACTED**",
                    command=AIRONE_CMD_STATUS,
                ),
                prefix,
            )

        holder.append(
            self._track_oneshot(
                async_call_later(self.hass, AIRONE_SILENCE_CHECK_SECONDS, _check)
            )
        )

    @callback
    def _schedule_airone_readback(self, device: AironeDevice) -> None:
        """Ask for the state once more after sending a command.

        **Nothing is updated optimistically.** Changing the UI as soon as a command is
        accepted means the user watches it "work and then revert" whenever the device refuses
        — dressing a failure up as a success. Mats avoid it for the same reason.

        Instead the real state is re-read. The device normally pushes it by itself, and this
        single request catches up when it does not.
        """
        device_id = device.device_id
        holder: list[Callable[[], None]] = []

        async def _readback(_now: Any) -> None:
            if holder:
                self._oneshot_unsubs.discard(holder[0])
            target = self.airone.get(device_id)
            if target is None or not target.available:
                return
            try:
                await self._async_airone_request(target, AIRONE_CMD_STATUS, None)
            except NavienSmartError as err:
                _LOGGER.debug("%s 상태 재확인 실패: %s", target.nickname, err)

        holder.append(
            self._track_oneshot(
                async_call_later(self.hass, AIRONE_READBACK_DELAY_SECONDS, _readback)
            )
        )

    async def async_send(self, device: NavienDevice, desired: dict[str, Any]) -> None:
        """No optimistic update after sending a command.

        It waits for the device to push a `reported`. Using the event fired when the command
        lands in the shadow (an `/accepted` without `reported`) as state would put HA ahead of
        the device.
        """
        # Record what was sent. Cooling can only be settled by seeing whether a sent value
        # returns unchanged, and this record is that evidence.
        device.note_command(desired)
        try:
            await self.api.async_control(self.home_seq, device.raw, desired)
        except NavienSmartAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
