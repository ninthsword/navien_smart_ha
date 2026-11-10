"""The Bluetooth mat through Home Assistant's own services, with transport.py replaced."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from importlib import import_module
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.navien_smarthome import PLATFORMS as CLOUD_PLATFORMS
from custom_components.navien_smarthome.ble_mat import (
    CONF_MAT_MODEL,
    connection,
    transport,
)
from custom_components.navien_smarthome.ble_mat.connection import (
    CONF_MAT_ID,
    LINK_CONNECTED,
    LINK_CONNECTING,
    LINK_DISABLED,
    LINK_NEEDS_REGISTRATION,
)
from custom_components.navien_smarthome.ble_mat.programs import CONF_CUSTOM_PROGRAMS
from custom_components.navien_smarthome.const import DOMAIN
from custom_components.navien_smarthome.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import entity_id_for_unique_id

ADDRESS = "00:11:22:33:44:55"
PREFIX = "001122334455"
MAT_ID = "a1a2a3a4a5a6"
HEATING = bytes.fromhex("b20001020302241e1c231e1cfefefe0060120433")


def checksum(raw: bytearray) -> bytes:
    raw[19] = sum(raw[1:19]) & 0xFF
    return bytes(raw)


def status(**fields: int) -> bytes:
    """Build a status frame from the heating example, naming only what differs."""
    raw = bytearray(HEATING)
    indexes = {
        "mode": 2,
        "flags": 3,
        "volume": 4,
        "heat": 5,
        "left": 10,
        "right": 11,
        "byte9": 9,
        "byte12": 12,
        "byte13": 13,
        "byte14": 14,
        "fast": 15,
        "ion": 16,
        "model": 17,
        "range": 18,
    }
    for name, value in fields.items():
        raw[indexes[name]] = value
    return checksum(raw)


def reply(code: int, issued: bytes = bytes.fromhex(MAT_ID)) -> bytes:
    raw = bytearray([0xFE]) * 20
    raw[0] = 0xB2
    raw[1] = 0xF1
    raw[2] = code
    raw[3:9] = issued
    return checksum(raw)


OFF_STATUS = status(mode=6, flags=0, heat=0, left=0, right=0, byte9=0xFE)
SLEEP = status(mode=3)


class FakeMat:
    """Replaces transport.py: records writes and answers like a mat."""

    def __init__(self) -> None:
        self.visible = True
        self.device_lookups = 0
        self.last_advertisement: float | Exception | None = 1.0
        self.advertisement_reads = 0
        self.connect_error: Exception | None = None
        self.write_error: list[Exception] = []
        self.missing_characteristics = False
        self.auth_replies: list[bytes | None] = [reply(2)]
        self.register_replies: list[bytes | None] = [reply(1), reply(2)]
        self.first_status: bytes | None = HEATING
        self.status_before_reply2: bytes | None = None
        self.during_write: bytes | None = None
        self.write_hangs = 0
        self.discovered: list[transport.DiscoveredMat] | Exception = []
        self.respond: Callable[[bytes], bytes | None] | None = None
        self.writes: list[bytes] = []
        self.attempts: list[bytes] = []
        self.clients: list[object] = []
        self.disconnected: list[object] = []
        self.cache_clears = 0
        self.connect_calls = 0
        self.on_frame: Callable[[bytes], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

    def controls(self) -> list[bytes]:
        return [frame for frame in self.writes if frame[1] == 0x80]

    def feed(self, frame: bytes) -> None:
        assert self.on_frame is not None
        self.on_frame(frame)

    def lose_link(self) -> None:
        assert self.on_disconnect is not None
        self.on_disconnect()

    def _answer(self, frame: bytes | None) -> None:
        if frame is not None:
            asyncio.get_running_loop().call_soon(self.feed, frame)

    # The functions of transport.py.
    def discovered_mats(self, hass: HomeAssistant) -> list[transport.DiscoveredMat]:
        if isinstance(self.discovered, Exception):
            raise self.discovered
        return self.discovered

    def ble_device(self, hass: HomeAssistant, address: str) -> Any:
        self.device_lookups += 1
        return object() if self.visible else None

    def last_advertisement_time(self, hass: HomeAssistant, address: str) -> float | None:
        self.advertisement_reads += 1
        if isinstance(self.last_advertisement, Exception):
            raise self.last_advertisement
        return self.last_advertisement

    async def connect(self, device: Any, on_disconnect: Callable[[], None]) -> Any:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        client = object()
        self.clients.append(client)
        self.on_disconnect = on_disconnect
        return client

    async def start_notify(
        self, client: Any, on_frame: Callable[[bytes], None]
    ) -> None:
        if self.missing_characteristics:
            raise transport.MissingCharacteristic
        self.on_frame = on_frame

    async def write(self, client: Any, frame: bytes) -> None:
        self.attempts.append(frame)
        if self.during_write is not None and frame[1] == 0x80:
            self.feed(self.during_write)
        if frame[1] == 0x80 and self.write_hangs:
            self.write_hangs -= 1
            await asyncio.Event().wait()
        if self.write_error:
            raise self.write_error.pop(0)
        self.writes.append(frame)
        if frame[1] == 0x01:
            if frame[2] == 0xFE:
                self._answer(self.register_replies.pop(0))
                return
            self._answer(self.status_before_reply2)
            queue = self.auth_replies
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            self._answer(answer)
            if answer == reply(2):
                self._answer(self.first_status)
            return
        if self.respond is not None:
            self._answer(self.respond(frame))

    async def clear_cache(self, client: Any) -> None:
        self.cache_clears += 1

    async def disconnect(self, client: Any) -> None:
        self.disconnected.append(client)


@pytest.fixture
def mat(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMat]:
    fake = FakeMat()
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(connection, "SEND_TIMEOUT", 0.02)
    monkeypatch.setattr(connection, "RETRY_DELAY", 0.01)
    monkeypatch.setattr(connection, "REPLY_TIMEOUT", 0.05)
    monkeypatch.setattr(connection, "FIRST_STATUS_TIMEOUT", 0.05)
    with patch.multiple(
        transport,
        async_discovered_mats=fake.discovered_mats,
        async_ble_device=fake.ble_device,
        async_last_advertisement_time=fake.last_advertisement_time,
        async_connect=fake.connect,
        async_start_notify=fake.start_notify,
        async_write=fake.write,
        async_clear_cache=fake.clear_cache,
        async_disconnect=fake.disconnect,
    ):
        yield fake


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def make_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="매트",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_MAT_ID: MAT_ID},
    )
    entry.add_to_hass(hass)
    return entry


async def setup_mat(
    hass: HomeAssistant, mat: FakeMat, wait_status: bool = True
) -> MockConfigEntry:
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if wait_status:
        await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    return entry


def climate_id(hass: HomeAssistant, side: str) -> str:
    return entity_id_for_unique_id(hass, "climate", f"{PREFIX}_{side}_thermostat")


def state_of(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def assert_status_entities_unavailable(hass: HomeAssistant) -> None:
    """Both thermostats, 운전 모드 and 오류 wait for a live status; the others do not."""
    for side in ("left", "right"):
        assert state_of(hass, climate_id(hass, side)).state == "unavailable"
    mode = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_mode")
    error = entity_id_for_unique_id(hass, "binary_sensor", f"{PREFIX}_error")
    assert state_of(hass, mode).state == "unavailable"
    assert state_of(hass, error).state == "unavailable"


async def set_temp(
    hass: HomeAssistant, side: str, value: float, hvac_mode: str | None = None
) -> None:
    data: dict[str, Any] = {"entity_id": climate_id(hass, side), "temperature": value}
    if hvac_mode is not None:
        data["hvac_mode"] = hvac_mode
    await hass.services.async_call("climate", "set_temperature", data, blocking=True)


async def set_mode(hass: HomeAssistant, side: str, mode: str) -> None:
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": climate_id(hass, side), "hvac_mode": mode},
        blocking=True,
    )


async def feed_and_settle(hass: HomeAssistant, mat: FakeMat, frame: bytes) -> None:
    mat.feed(frame)
    await hass.async_block_till_done()


def confirm_with(frame: bytes) -> Callable[[bytes], bytes]:
    return lambda _sent: frame


# Options and device information.


def serialize_form(result: Any) -> list[dict[str, Any]]:
    """Use the form serializer available to this Home Assistant version's flow view."""
    try:
        serialize = import_module("probatio").to_field_list
    except (ImportError, AttributeError):
        serialize = import_module("voluptuous_serialize").convert
    return serialize(result["data_schema"], custom_serializer=cv.custom_serializer)


def mat_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    """Return the entry's one device; identifiers are not unique from 2026.9."""
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    return devices[0]


async def mat_options_form(
    hass: HomeAssistant, entry: MockConfigEntry, step: str = "mat_model"
) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    assert result.get("step_id") == "mat_menu"
    assert result.get("menu_options") == ["mat_model", "custom_program"]
    return dict(await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    ))


@pytest.mark.parametrize("stored", [None, "EQM560"])
async def test_mat_model_form_prefills_and_serializes(
    hass: HomeAssistant, stored: str | None
) -> None:
    entry = make_entry(hass)
    if stored is not None:
        hass.config_entries.async_update_entry(entry, options={CONF_MAT_MODEL: stored})
    # Importing the module registers the handler that supports_options consults.
    import_module("custom_components.navien_smarthome.config_flow")
    assert entry.supports_options

    result = await mat_options_form(hass, entry)

    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "mat_model"
    schema: Any = result.get("data_schema")
    assert len(schema.schema) == 1
    field = next(iter(schema.schema))
    assert isinstance(field, vol.Optional)
    assert str(field) == CONF_MAT_MODEL
    assert field.description == {"suggested_value": stored}
    assert schema({}) == {}
    fields = serialize_form(result)
    assert len(fields) == 1
    assert fields[0]["name"] == CONF_MAT_MODEL
    assert not fields[0].get("required", False)
    assert "default" not in fields[0]
    selector = fields[0]["selector"]["select"]
    assert selector["mode"] == "dropdown"
    assert not selector.get("multiple", False)
    assert not selector.get("custom_value", False)
    assert selector["options"] == [
        "EQM565",
        "EQM555",
        "EQM553",
        "EQM562",
        "EQM561",
        "EQM560",
        "EQM552",
        "EQM550",
        "EQM551",
        "EQM571",
        "EQM570",
        "EQM530",
        "EQH40/20DN",
    ]
    for model in selector["options"]:
        assert schema({CONF_MAT_MODEL: model}) == {CONF_MAT_MODEL: model}


@pytest.mark.parametrize("invalid", ["", "SYNTHETIC_MODEL"])
async def test_mat_model_selector_refuses_invalid_values(
    hass: HomeAssistant, invalid: str
) -> None:
    entry = make_entry(hass)
    result = await mat_options_form(hass, entry)
    schema: Any = result.get("data_schema")

    with pytest.raises(vol.Invalid):
        schema({CONF_MAT_MODEL: invalid})
    assert entry.options == {}


@pytest.mark.parametrize("model", [None, "EQM560"])
async def test_mat_device_registry_information(
    hass: HomeAssistant, mat: FakeMat, model: str | None
) -> None:
    entry = make_entry(hass)
    if model is not None:
        hass.config_entries.async_update_entry(entry, options={CONF_MAT_MODEL: model})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()

    device = mat_device(hass, entry)
    assert device.name == "숙면매트"
    assert device.manufacturer == "경동나비엔"
    assert device.model == model
    assert device.serial_number is None
    assert device.identifiers == {(DOMAIN, ADDRESS)}
    assert entry.title == "매트"
    assert entry.unique_id == ADDRESS
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert {entity.device_id for entity in entities} == {device.id}
    assert {entity.unique_id for entity in entities} == {
        f"{PREFIX}_{suffix}"
        for suffix in (
            "left_thermostat",
            "right_thermostat",
            "mode",
            "reservation_time",
            "link",
            "water_level",
            "error_detail",
            "fast_heat_minutes",
            "error",
            "ion_care_running",
            "enabled",
            "power",
            "lock",
            "fast_heat",
            "volume",
            "ion_care_period",
            "ion_care_run",
            "left_match_temperature",
            "right_match_temperature",
            "off_reservation",
            "on_reservation",
            "left_sleep_hours",
            "right_sleep_hours",
            "sleep",
            "on_reservation_temperature",
            "custom_sleep",
            "custom_program",
        )
    }


@pytest.mark.parametrize(
    ("submitted", "model"),
    [({CONF_MAT_MODEL: "EQM565"}, "EQM565"), ({}, None)],
    ids=["saves", "omitted-clears"],
)
async def test_mat_model_save_reloads_and_updates_device(
    hass: HomeAssistant,
    mat: FakeMat,
    submitted: dict[str, Any],
    model: str | None,
) -> None:
    entry = make_entry(hass)
    programs = {"1": {"left": [{"hours": 2, "temperature": 30}], "right": []}}
    hass.config_entries.async_update_entry(
        entry, options={CONF_MAT_MODEL: "EQM560", CONF_CUSTOM_PROGRAMS: programs}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.status is not None)
    device = mat_device(hass, entry)
    assert device.model == "EQM560"
    entities_before = {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    previous_connection = entry.runtime_data

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await mat_options_form(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], submitted
        )
        await hass.async_block_till_done()
        await until(lambda: entry.runtime_data.status is not None)
        await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_CUSTOM_PROGRAMS: programs, **submitted}
    reload.assert_called_once_with(entry.entry_id)
    assert entry.runtime_data is not previous_connection
    updated = mat_device(hass, entry)
    assert updated.id == device.id
    assert updated.name == "숙면매트"
    assert updated.manufacturer == "경동나비엔"
    if model is None:
        assert updated.model is None
    else:
        assert updated.model == model
    assert updated.serial_number is None
    assert updated.identifiers == {(DOMAIN, ADDRESS)}
    assert entry.title == "매트"
    assert entry.unique_id == ADDRESS
    assert entry.data == {CONF_ADDRESS: ADDRESS, CONF_MAT_ID: MAT_ID}
    assert {
        entity.unique_id: entity.entity_id
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    } == entities_before


# Availability and state.


async def test_entities_exist_and_are_unavailable_before_first_status(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = None
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    for side in ("left", "right"):
        assert state_of(hass, climate_id(hass, side)).state == "unavailable"
    mode = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_mode")
    error = entity_id_for_unique_id(hass, "binary_sensor", f"{PREFIX}_error")
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    switch = entity_id_for_unique_id(hass, "switch", f"{PREFIX}_enabled")
    assert state_of(hass, mode).state == "unavailable"
    assert state_of(hass, error).state == "unavailable"
    assert state_of(hass, link).state == LINK_CONNECTED
    assert state_of(hass, switch).state == "on"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    assert_status_entities_unavailable(hass)


async def test_heating_off_and_other_states(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    left = state_of(hass, climate_id(hass, "left"))
    assert left.state == "heat"
    assert left.attributes["hvac_action"] == "heating"
    assert left.attributes["current_temperature"] == 30
    assert left.attributes["temperature"] == 30
    assert left.attributes["min_temp"] == 28
    assert left.attributes["max_temp"] == 45

    await feed_and_settle(hass, mat, OFF_STATUS)
    off = state_of(hass, climate_id(hass, "left"))
    assert off.state == "off"
    assert off.attributes["hvac_action"] == "off"
    assert off.attributes["temperature"] is None

    await feed_and_settle(hass, mat, status(heat=3))
    assert state_of(hass, climate_id(hass, "left")).state == "heat"
    assert state_of(hass, climate_id(hass, "right")).state == "off"

    await feed_and_settle(hass, mat, status(heat=4))
    assert state_of(hass, climate_id(hass, "left")).state == "off"

    mode = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_mode")
    error = entity_id_for_unique_id(hass, "binary_sensor", f"{PREFIX}_error")
    await feed_and_settle(hass, mat, status(mode=3))
    assert state_of(hass, mode).state == "수면"
    assert state_of(hass, error).state == "off"
    await feed_and_settle(hass, mat, status(mode=5))
    assert state_of(hass, error).state == "on"
    await feed_and_settle(hass, mat, status(mode=0x20))
    assert state_of(hass, mode).state == "unknown"


async def test_reconnect_needs_a_fresh_status(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    mat.first_status = None
    sent_before = len(mat.controls())
    mat.lose_link()
    await hass.async_block_till_done()
    assert_status_entities_unavailable(hass)
    await set_temp(hass, "left", 31)  # raises nothing and sends nothing
    assert len(mat.controls()) == sent_before

    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    assert_status_entities_unavailable(hass)
    await set_temp(hass, "left", 31)
    assert len(mat.controls()) == sent_before

    await feed_and_settle(hass, mat, status(left=33))
    assert state_of(hass, climate_id(hass, "left")).state == "heat"
    mat.respond = confirm_with(status(left=33, right=29))
    await set_temp(hass, "right", 29)
    assert mat.controls()[-1][10:12] == bytes([33, 29])


# Commands.


def power_id(hass: HomeAssistant) -> str:
    return entity_id_for_unique_id(hass, "switch", f"{PREFIX}_power")


async def set_power(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": power_id(hass)}, blocking=True
    )


def entity_status(hass: HomeAssistant, mat: FakeMat) -> Any:
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0].runtime_data.status.state


async def test_temperature_exact_frame_and_confirmation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(right=29))
    await set_temp(hass, "right", 29)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c231e1dfefefefefefefe33"
    ]


async def test_resend_then_confirmed(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    answers = iter([None, status(right=29)])
    mat.respond = lambda _frame: next(answers)
    await set_temp(hass, "right", 29)
    assert len(mat.controls()) == 2


async def test_unconfirmed_raises_after_one_resend(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 29)
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 2


async def test_power_off_exact_frame_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_power(hass, "turn_off")
    assert not isinstance(raised.value, ServiceValidationError)
    assert [frame.hex() for frame in mat.controls()] == [
        "b280060000fe241e1cfe0000000000fefefefed8"
    ] * 2


async def test_heat_resend_and_final_error(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    answers = iter([None, HEATING])
    mat.respond = lambda _frame: next(answers)
    await set_mode(hass, "left", "heat")
    assert len(mat.controls()) == 2
    assert mat.controls()[0][2] == 1 and mat.controls()[0][10:12] == bytes([30, 28])

    await feed_and_settle(hass, mat, OFF_STATUS)
    mat.respond = None
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "left", "heat")
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 4


async def test_heat_resends_when_heating_at_other_targets(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    answers = iter([status(left=31, right=29), HEATING])
    mat.respond = lambda _frame: next(answers)
    await set_mode(hass, "left", "heat")
    assert len(mat.controls()) == 2
    assert [frame[10:12] for frame in mat.controls()] == [bytes([30, 28])] * 2


async def test_heat_fails_when_only_other_targets_arrive(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    mat.respond = confirm_with(status(left=31, right=29))
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "left", "heat")
    assert not isinstance(raised.value, ServiceValidationError)
    assert [frame[10:12] for frame in mat.controls()] == [bytes([30, 28])] * 2


async def test_failed_send_counts_as_unconfirmed(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_error = [OSError("dying link")]
    mat.respond = confirm_with(status(right=29))
    await set_temp(hass, "right", 29)
    assert len(mat.controls()) == 1

    mat.respond = None
    mat.write_error = [OSError("dying link"), OSError("dying link")]
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 30)
    assert "dying" not in str(raised.value)


async def test_write_raises_resends_then_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_error = [OSError("dying link"), OSError("dying link")]
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 29)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert [frame.hex() for frame in mat.attempts if frame[1] == 0x80] == [
        "b28001020002241e1c231e1dfefefefefefefe33"
    ] * 2
    assert mat.controls() == []


async def test_write_hangs_resends_then_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_hangs = 2
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 29)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert [frame.hex() for frame in mat.attempts if frame[1] == 0x80] == [
        "b28001020002241e1c231e1dfefefefefefefe33"
    ] * 2
    assert mat.controls() == []


async def test_write_hangs_once_then_confirmed(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.write_hangs = 1
    mat.respond = confirm_with(status(right=29))
    await set_temp(hass, "right", 29)
    assert len([frame for frame in mat.attempts if frame[1] == 0x80]) == 2
    assert len(mat.controls()) == 1


async def test_same_target_without_later_status_is_unconfirmed(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "left", 30)  # the held status already shows 30 and 28
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 2


async def test_identical_status_after_the_send_confirms(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = confirm_with(HEATING)
    await set_temp(hass, "left", 30)
    assert len(mat.controls()) == 1


async def test_status_received_during_the_write_does_not_confirm(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.during_write = status(right=29)
    with pytest.raises(HomeAssistantError):
        await set_temp(hass, "right", 29)
    assert len(mat.controls()) == 2


async def test_link_lost_during_confirmation(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 5.0)
    await setup_mat(hass, mat)
    first = hass.async_create_task(set_temp(hass, "right", 29))
    await until(lambda: len(mat.controls()) == 1)
    waiting = hass.async_create_task(set_temp(hass, "left", 31))
    await asyncio.sleep(0.05)  # the second call is now behind the lock
    mat.lose_link()
    with pytest.raises(HomeAssistantError) as one:
        await first
    assert not isinstance(one.value, ServiceValidationError)
    with pytest.raises(HomeAssistantError) as two:
        await waiting
    assert not isinstance(two.value, ServiceValidationError)
    assert len(mat.controls()) == 1


async def test_callbacks_after_a_lost_link_are_ignored(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "REPLY_TIMEOUT", 30.0)
    monkeypatch.setattr(connection, "RETRY_DELAY", 30.0)
    mat.auth_replies = [None]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: len(mat.writes) == 1)
    stale = mat.on_frame
    assert stale is not None
    conn = entry.runtime_data

    mat.lose_link()
    stale(reply(2))
    stale(HEATING)
    assert conn.authenticated is False
    assert conn.status is None

    # The attempt ends at once rather than after REPLY_TIMEOUT.
    await until(lambda: mat.disconnected == mat.clients)
    assert conn.authenticated is False
    assert conn.status is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_status_after_a_lost_link_is_ignored(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "RETRY_DELAY", 30.0)
    entry = await setup_mat(hass, mat)
    stale = mat.on_frame
    assert stale is not None
    mat.lose_link()
    stale(status(left=33))
    assert entry.runtime_data.status is None
    assert entry.runtime_data.authenticated is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_two_temperatures_in_a_row_both_applied(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 33)
    await set_temp(hass, "right", 34)
    assert mat.controls()[-1][10:12] == bytes([33, 34])


async def test_rounding_half_up(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 32.5)
    assert mat.controls()[-1][10] == 33
    await set_temp(hass, "left", 31.4)
    assert mat.controls()[-1][10] == 31


@pytest.mark.parametrize("frame", [SLEEP, status(heat=0), status(heat=9)])
async def test_other_state_refused_from_both_sides(
    hass: HomeAssistant, mat: FakeMat, frame: bytes
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, frame)
    for side in ("left", "right"):
        with pytest.raises(ServiceValidationError):
            await set_temp(hass, side, 33)
        with pytest.raises(ServiceValidationError):
            await set_temp(hass, side, 33, "off")
        with pytest.raises(ServiceValidationError):
            await set_mode(hass, side, "off")
    assert mat.controls() == []


async def test_temperature_alone_refused_while_off(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await set_temp(hass, "left", 33)
    assert mat.controls() == []


async def test_temperature_with_hvac_mode_while_heating(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 33, "heat")
    assert mat.controls()[-1][2] == 1
    assert mat.controls()[-1][10] == 33

    mat.respond = confirm_with(status(heat=4, left=28))
    await set_temp(hass, "left", 35, "off")
    assert mat.controls()[-1].hex() == "b28001020004241e1c231c1cfefefefefefefe32"


async def test_temperature_with_hvac_mode_while_off(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    await set_temp(hass, "left", 33, "off")
    assert mat.controls() == []  # already off

    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_temp(hass, "left", 33, "heat")
    assert mat.controls()[-1][10:12] == bytes([33, 28])
    assert mat.controls()[-1][2] == 1


async def test_power_switch_frames_and_idempotence(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await set_mode(hass, "left", "heat")
    await set_power(hass, "turn_on")
    assert mat.controls() == []  # already heating

    mat.respond = confirm_with(OFF_STATUS)
    await set_power(hass, "turn_off")
    assert mat.controls()[-1].hex() == "b280060000fe241e1cfe0000000000fefefefed8"

    await hass.async_block_till_done()
    sent = len(mat.controls())
    await set_power(hass, "turn_off")
    await set_mode(hass, "left", "off")
    assert len(mat.controls()) == sent  # already off

    mat.respond = lambda frame: status(left=frame[10], right=frame[11])
    await set_power(hass, "turn_on")
    assert mat.controls()[-1].hex() == "b28001020002241e1c231e1cfefefefefefefe32"
    await hass.async_block_till_done()
    await set_power(hass, "turn_on")
    assert len(mat.controls()) == sent + 1


@pytest.mark.parametrize(
    "frame", [status(heat=3, right=0), status(heat=4, left=0)]
)
async def test_power_switch_off_from_one_side_on(
    hass: HomeAssistant, mat: FakeMat, frame: bytes
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, frame)
    mat.respond = confirm_with(OFF_STATUS)
    await set_power(hass, "turn_off")
    assert mat.controls()[-1][2] == 6


@pytest.mark.parametrize("heat", [3, 4])
async def test_power_switch_on_with_one_side_on_sends_nothing(
    hass: HomeAssistant, mat: FakeMat, heat: int
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=heat))
    await set_power(hass, "turn_on")
    assert mat.controls() == []


async def test_power_switch_state(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    power = power_id(hass)
    assert state_of(hass, power).state == "on"
    await feed_and_settle(hass, mat, status(heat=3))
    assert state_of(hass, power).state == "on"
    await feed_and_settle(hass, mat, SLEEP)
    assert state_of(hass, power).state == "on"
    await feed_and_settle(hass, mat, OFF_STATUS)
    assert state_of(hass, power).state == "off"
    mat.first_status = None
    mat.lose_link()
    await hass.async_block_till_done()
    assert state_of(hass, power).state == "unavailable"


@pytest.mark.parametrize("mode", [2, 3, 9])
async def test_power_switch_refusals(
    hass: HomeAssistant, mat: FakeMat, mode: int
) -> None:
    mat.first_status = OFF_STATUS
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(mode=mode))
    with pytest.raises(ServiceValidationError):
        await set_power(hass, "turn_on")
    assert mat.controls() == []

    mat.respond = confirm_with(OFF_STATUS)
    await set_power(hass, "turn_off")
    assert mat.controls() == [
        bytes.fromhex("b280060000fe241e1cfe0000000000fefefefed8")
    ]


# One side at a time.


@pytest.mark.parametrize(
    ("heat", "left_mode", "right_mode"),
    [(2, "heat", "heat"), (3, "heat", "off"), (4, "off", "heat")],
)
async def test_each_thermostat_follows_its_side(
    hass: HomeAssistant, mat: FakeMat, heat: int, left_mode: str, right_mode: str
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=heat))
    assert state_of(hass, climate_id(hass, "left")).state == left_mode
    assert state_of(hass, climate_id(hass, "right")).state == right_mode


@pytest.mark.parametrize("heat", [0, 9])
async def test_other_byte_5_keeps_the_other_state_display(
    hass: HomeAssistant, mat: FakeMat, heat: int
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=heat))
    assert entity_status(hass, mat) is connection.MatState.OTHER
    assert state_of(hass, climate_id(hass, "left")).state == "heat"
    assert state_of(hass, climate_id(hass, "right")).state == "heat"


@pytest.mark.parametrize(
    ("side", "reply_status", "expected"),
    [
        ("left", status(heat=4, left=28, right=34), "b28001020004241e1c231c22fefefefefefefe38"),
        ("right", status(heat=3, left=33, right=28), "b28001020003241e1c23211cfefefefefefefe36"),
    ],
)
async def test_side_off_exact_frame(
    hass: HomeAssistant, mat: FakeMat, side: str, reply_status: bytes, expected: str
) -> None:
    mat.first_status = status(left=33, right=34)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(reply_status)
    await set_mode(hass, side, "off")
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_side_off_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "left", "off")
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020004241e1c231c1cfefefefefefefe32"
    ] * 2


async def test_side_off_resend_confirmed_by_byte_5(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    answers = iter([status(heat=2), status(heat=4, left=28)])
    mat.respond = lambda _frame: next(answers)
    await set_mode(hass, "left", "off")
    assert len(mat.controls()) == 2


async def test_side_off_resend_repeats_the_frame_after_status_moves(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    # The reply to the first send moves the status to this side already off.
    mat.during_write = status(heat=4, left=28)
    with pytest.raises(HomeAssistantError):
        await set_mode(hass, "left", "off")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020004241e1c231c1cfefefefefefefe32"
    ] * 2


@pytest.mark.parametrize(
    ("start", "command"),
    [
        (HEATING, lambda hass: set_mode(hass, "left", "off")),
        (HEATING, lambda hass: set_temp(hass, "right", 33)),
        (status(heat=3), lambda hass: set_mode(hass, "right", "heat")),
        (status(heat=3), lambda hass: set_temp(hass, "right", 33)),
    ],
)
async def test_per_side_command_stops_when_the_mat_left_heating(
    hass: HomeAssistant,
    mat: FakeMat,
    monkeypatch: pytest.MonkeyPatch,
    start: bytes,
    command: Callable[[HomeAssistant], Any],
) -> None:
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0.2)
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, start)
    task = hass.async_create_task(command(hass))
    await until(lambda: len(mat.controls()) == 1)
    mat.feed(OFF_STATUS)
    with pytest.raises(HomeAssistantError) as raised:
        await task
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 1


@pytest.mark.parametrize(("heat", "side"), [(3, "left"), (4, "right")])
@pytest.mark.parametrize("through_temperature", [False, True])
async def test_last_side_off_powers_the_mat_off(
    hass: HomeAssistant, mat: FakeMat, heat: int, side: str, through_temperature: bool
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=heat))
    mat.respond = confirm_with(OFF_STATUS)
    if through_temperature:
        await set_temp(hass, side, 35, "off")
    else:
        await set_mode(hass, side, "off")
    assert [frame.hex() for frame in mat.controls()] == [
        "b280060000fe241e1cfe0000000000fefefefed8"
    ]


async def turn_last_side_off(
    hass: HomeAssistant, side: str, through_temperature: bool
) -> None:
    if through_temperature:
        await set_temp(hass, side, 35, "off")
    else:
        await set_mode(hass, side, "off")


@pytest.mark.parametrize(("heat", "side"), [(3, "left"), (4, "right")])
@pytest.mark.parametrize("through_temperature", [False, True])
async def test_last_side_off_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat, heat: int, side: str, through_temperature: bool
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=heat))
    answers = iter([None, OFF_STATUS])
    mat.respond = lambda _frame: next(answers)
    await turn_last_side_off(hass, side, through_temperature)
    assert [frame.hex() for frame in mat.controls()] == [
        "b280060000fe241e1cfe0000000000fefefefed8"
    ] * 2

    await feed_and_settle(hass, mat, status(heat=heat))
    mat.respond = None
    with pytest.raises(HomeAssistantError) as raised:
        await turn_last_side_off(hass, side, through_temperature)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 4


@pytest.mark.parametrize(("heat", "side"), [(3, "left"), (4, "right")])
@pytest.mark.parametrize("through_temperature", [False, True])
async def test_last_side_off_resend_stops_when_the_other_side_came_on(
    hass: HomeAssistant,
    mat: FakeMat,
    monkeypatch: pytest.MonkeyPatch,
    heat: int,
    side: str,
    through_temperature: bool,
) -> None:
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0.2)
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=heat))
    task = hass.async_create_task(turn_last_side_off(hass, side, through_temperature))
    await until(lambda: len(mat.controls()) == 1)
    mat.feed(HEATING)
    with pytest.raises(HomeAssistantError) as raised:
        await task
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 1


async def test_off_on_a_side_already_off_sends_nothing(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3))
    await set_mode(hass, "right", "off")
    await set_temp(hass, "right", 33, "off")
    assert mat.controls() == []


async def test_heat_on_an_off_side_uses_the_remembered_target(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=33, right=34)
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3, left=33, right=28))
    mat.respond = confirm_with(status(left=33, right=34))
    await set_mode(hass, "right", "heat")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c232122fefefefefefefe3b"
    ]
    await hass.async_block_till_done()
    await set_mode(hass, "right", "heat")
    assert len(mat.controls()) == 1  # already on


async def test_heat_on_an_off_side_after_a_one_side_first_status_uses_30(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(heat=3)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(right=30))
    await set_mode(hass, "right", "heat")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c231e1efefefefefefefe34"
    ]


async def test_heat_on_an_off_side_with_nothing_stored_uses_30(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(heat=3, left=20)
    entry = await setup_mat(hass, mat)
    assert entry.runtime_data.stored_status is None
    mat.respond = confirm_with(status(left=20, right=30))
    await set_mode(hass, "right", "heat")
    # The side already on keeps the byte the set_temperature path echoes.
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c23141efefefefefefefe2a"
    ]


async def test_temperature_on_an_off_side_after_a_one_side_first_status_is_sent(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(heat=3)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(right=33))
    await set_temp(hass, "right", 33)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c231e21fefefefefefefe37"
    ]


async def test_temperature_28_on_an_off_side_turns_it_on(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3))
    mat.respond = confirm_with(status(right=28))
    await set_temp(hass, "right", 28)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c231e1cfefefefefefefe32"
    ]


async def test_side_on_reply_at_a_wrong_target_fails(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3))
    mat.respond = confirm_with(status(right=34))
    with pytest.raises(HomeAssistantError) as raised:
        await set_temp(hass, "right", 33)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED


async def test_heat_on_an_off_side_reply_at_a_wrong_target_fails(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=33, right=34)
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3, left=33, right=28))
    mat.respond = confirm_with(status(left=33, right=30))
    with pytest.raises(HomeAssistantError) as raised:
        await set_mode(hass, "right", "heat")
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c232122fefefefefefefe3b"
    ] * 2
    assert len(mat.controls()) == 2


@pytest.mark.parametrize(
    ("first", "side", "value", "expected", "confirm"),
    [
        (
            status(heat=3, left=33, right=20),
            "left",
            35,
            "b28001020003241e1c23231cfefefefefefefe38",
            status(heat=3, left=35, right=0),
        ),
        (
            status(heat=4, left=5, right=33),
            "right",
            36,
            "b28001020004241e1c231c24fefefefefefefe3a",
            status(heat=4, left=0, right=36),
        ),
    ],
)
async def test_temperature_on_the_side_on_while_the_other_is_off(
    hass: HomeAssistant,
    mat: FakeMat,
    first: bytes,
    side: str,
    value: int,
    expected: str,
    confirm: bytes,
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, first)
    mat.respond = confirm_with(confirm)
    await set_temp(hass, side, value)
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_status_below_28_shows_no_target(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3, right=20))
    assert state_of(hass, climate_id(hass, "right")).attributes["temperature"] is None


# The remembered targets.


async def test_targets_remembered_across_side_off_cycle_and_reload(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    mat.first_status = status(left=33, right=34)
    entry = await setup_mat(hass, mat)
    mat.respond = confirm_with(status(heat=4, left=28, right=34))
    await set_mode(hass, "left", "off")
    await hass.async_block_till_done()
    await feed_and_settle(hass, mat, status(heat=4, left=28, right=36))
    merged = bytes.fromhex("b20001020304241e1c232124fefefe0060120440")
    assert entry.runtime_data.stored_status.raw == merged

    await feed_and_settle(hass, mat, OFF_STATUS)
    assert entry.runtime_data.stored_status.raw == merged
    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    assert hass_storage[key]["data"]["heating_status"] == merged.hex()
    assert entry.runtime_data.stored_status.raw == merged

    mat.respond = confirm_with(status(heat=4, left=0, right=36))
    await set_power(hass, "turn_on")
    assert mat.controls()[-1].hex() == "b28001020004241e1c231c24fefefefefefefe3a"


async def test_one_side_first_status_creates_a_store(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(heat=3, left=33, right=0)
    entry = await setup_mat(hass, mat)
    stored = entry.runtime_data.stored_status
    assert (stored.heat_type, stored.left_target, stored.right_target) == (3, 33, 30)
    await feed_and_settle(hass, mat, status(left=33, right=34))
    assert entry.runtime_data.stored_status.raw == status(left=33, right=34)


async def test_out_of_range_active_side_leaves_the_store_unchanged(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3, left=20))
    assert entry.runtime_data.stored_status.raw == HEATING


async def test_power_on_mixes_latest_and_stored(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(byte9=0x21, volume=0x20)
    await setup_mat(hass, mat)
    await feed_and_settle(
        hass, mat, status(mode=6, flags=0x04, volume=0x13, heat=0, left=0, right=0)
    )
    mat.respond = confirm_with(status(left=30, right=28, flags=0x06, byte9=0x21))
    await set_mode(hass, "left", "heat")
    # Byte 3's lock bit and byte 4 are the latest status's; byte 9 and the targets the stored one's.
    assert mat.controls()[-1].hex() == "b28001061002241e1c211e1cfefefefefefefe44"

    await feed_and_settle(
        hass, mat, status(mode=6, flags=0x04, volume=0x13, heat=0, left=0, right=0)
    )
    mat.respond = confirm_with(status(left=33, right=28, flags=0x06, byte9=0x21))
    await set_temp(hass, "left", 33, "heat")
    assert mat.controls()[-1].hex() == "b28001061002241e1c21211cfefefefefefefe47"


async def test_no_stored_status_powers_on_at_30(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = OFF_STATUS
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(left=30, right=30))
    await set_power(hass, "turn_on")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1cfe1e1efefefefefefefe0f"
    ]


@pytest.mark.parametrize(
    ("side", "value", "heat", "expected"),
    [
        ("left", None, 3, "b28001020003241e1cfe1e1cfefefefefefefe0e"),
        ("left", 33, 3, "b28001020003241e1cfe211cfefefefefefefe11"),
        ("right", None, 4, "b28001020004241e1cfe1c1efefefefefefefe0f"),
        ("right", 33, 4, "b28001020004241e1cfe1c21fefefefefefefe12"),
    ],
)
async def test_no_stored_status_thermostat_on_turns_on_only_that_side(
    hass: HomeAssistant,
    mat: FakeMat,
    side: str,
    value: int | None,
    heat: int,
    expected: str,
) -> None:
    mat.first_status = OFF_STATUS
    await setup_mat(hass, mat)
    target = 30 if value is None else value
    mat.respond = confirm_with(
        status(heat=heat, left=target, right=0)
        if side == "left"
        else status(heat=heat, left=0, right=target)
    )
    if value is None:
        await set_mode(hass, side, "heat")
    else:
        await set_temp(hass, side, value, "heat")
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_stored_status_survives_reload_and_is_removed(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    remembered = status(byte9=0x21, left=33, right=29)
    mat.first_status = remembered
    entry = await setup_mat(hass, mat)

    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data.status.raw == OFF_STATUS
    assert entry.runtime_data.stored_status.raw == remembered

    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    assert hass_storage[key]["data"]["heating_status"] == remembered.hex()

    mat.respond = confirm_with(remembered)
    await set_mode(hass, "left", "heat")
    frame = mat.controls()[-1]
    assert frame[9] == 0x21
    assert frame[10:12] == bytes([33, 29])

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


@pytest.mark.parametrize(
    "stored",
    [
        "zz",
        12345,
        HEATING.hex()[:-2],
        bytes(20).hex(),
        status(left=0).hex(),
        OFF_STATUS.hex(),
    ],
)
async def test_malformed_store_is_absent(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], stored: Any
) -> None:
    mat.first_status = OFF_STATUS
    entry = make_entry(hass)
    hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.ble_mat.{entry.entry_id}",
        "data": {"enabled": True, "heating_status": stored},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.stored_status is None
    mat.respond = confirm_with(status(heat=3, left=30, right=0))
    await set_mode(hass, "left", "heat")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020003241e1cfe1e1cfefefefefefefe0e"
    ]


async def test_store_holding_no_mapping_is_absent(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = make_entry(hass)
    hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.ble_mat.{entry.entry_id}",
        "data": ["not", "a", "mapping"],
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.enabled is True


async def test_store_is_written_only_for_what_a_power_on_uses(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    conn = entry.runtime_data
    with patch.object(conn._store, "async_delay_save") as save:
        raw = bytearray(HEATING)
        raw[7] = 31  # the left current temperature
        await feed_and_settle(hass, mat, checksum(raw))
        save.assert_not_called()
        assert conn.stored_status.raw == HEATING

        await feed_and_settle(hass, mat, status(left=33))
        save.assert_called_once()
        await feed_and_settle(hass, mat, status(left=33, byte9=0x21))
        assert save.call_count == 2


async def test_switch_and_its_persistence(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_mat(hass, mat)
    switch = entity_id_for_unique_id(hass, "switch", f"{PREFIX}_enabled")
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()
    assert mat.disconnected
    assert state_of(hass, switch).state == "off"
    assert state_of(hass, link).state == LINK_DISABLED
    assert_status_entities_unavailable(hass)
    await set_temp(hass, "left", 31)  # skipped by Home Assistant
    assert mat.controls() == []
    assert hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"]["data"]["enabled"] is False

    connects = mat.connect_calls
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await settle()
    assert mat.connect_calls == connects
    assert state_of(hass, switch).state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert state_of(hass, link).state == LINK_CONNECTED


# Recovery paths.


async def test_not_seen_then_found(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.visible = False
    entry = await setup_mat(hass, mat, wait_status=False)
    assert entry.state is ConfigEntryState.LOADED
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    assert state_of(hass, link).state == LINK_CONNECTING
    mat.visible = True
    await until(lambda: entry.runtime_data.status is not None)


async def test_connect_failure_then_retry(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.connect_error = OSError("no route")
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: mat.connect_calls >= 2)
    mat.connect_error = None
    await until(lambda: entry.runtime_data.status is not None)


async def test_no_reply_then_retry(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.auth_replies = [None, reply(2)]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.status is not None)
    assert len(mat.clients) == 2
    assert mat.disconnected[0] is mat.clients[0]


async def test_lost_link_reconnects(hass: HomeAssistant, mat: FakeMat) -> None:
    entry = await setup_mat(hass, mat)
    mat.lose_link()
    assert entry.runtime_data.status is None
    await until(lambda: entry.runtime_data.status is not None)
    assert len(mat.clients) == 2


async def test_silence_reconnects(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "SILENCE_TIMEOUT", 0.02)
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: len(mat.clients) >= 2)
    assert mat.disconnected[0] is mat.clients[0]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_missing_characteristics_clear_cache_once(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.missing_characteristics = True
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: mat.connect_calls >= 3)
    assert mat.cache_clears == 1
    mat.missing_characteristics = False
    await until(lambda: entry.runtime_data.status is not None)


async def test_unexpected_exception_retries(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.connect_error = RuntimeError("anything")
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: mat.connect_calls >= 2)
    mat.connect_error = None
    await until(lambda: entry.runtime_data.status is not None)


async def test_three_refusals_stop_and_switch_restarts(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.auth_replies = [reply(0)]
    entry = await setup_mat(hass, mat, wait_status=False)
    link = entity_id_for_unique_id(hass, "sensor", f"{PREFIX}_link")
    switch = entity_id_for_unique_id(hass, "switch", f"{PREFIX}_enabled")
    await until(lambda: entry.runtime_data.needs_registration)
    await hass.async_block_till_done()
    assert state_of(hass, link).state == LINK_NEEDS_REGISTRATION
    connects = mat.connect_calls
    assert connects == 3
    await asyncio.sleep(0.03)
    assert mat.connect_calls == connects

    mat.auth_replies = [reply(2)]
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await until(lambda: entry.runtime_data.status is not None)


async def test_only_consecutive_refusals_count(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.auth_replies = [reply(0), reply(0), None, reply(0), reply(0), reply(2)]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.status is not None)
    assert mat.connect_calls == 6
    assert entry.runtime_data.needs_registration is False


async def test_reload_restarts_after_refusals(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.auth_replies = [reply(0)]
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.needs_registration)
    mat.auth_replies = [reply(2)]
    await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)


async def test_unload_disconnects(hass: HomeAssistant, mat: FakeMat) -> None:
    entry = await setup_mat(hass, mat)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert mat.disconnected == mat.clients


async def test_no_control_frame_before_authentication(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = None
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.authenticated)
    assert mat.writes[0][1] == 0x01
    assert mat.controls() == []


# Platforms and diagnostics.


async def test_forwards_only_its_seven_platforms(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = make_entry(hass)
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", return_value=None
    ) as forward:
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert {str(p) for p in forward.call_args.args[1]} == {
        "binary_sensor",
        "button",
        "climate",
        "number",
        "select",
        "sensor",
        "switch",
    }
    await hass.config_entries.async_unload(entry.entry_id)


async def test_cloud_entry_forwards_no_button_platform(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    assert "button" not in {str(platform) for platform in CLOUD_PLATFORMS}
    assert "button" not in {entity.domain for entity in hass.states.async_all()}


async def test_cloud_entry_still_loads_every_platform(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    domains = {entity.domain for entity in hass.states.async_all()}
    assert {"climate", "sensor", "switch", "binary_sensor", "water_heater"} <= domains


async def test_diagnostics_hold_no_address_or_id(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    result = await async_get_config_entry_diagnostics(hass, entry)
    text = str(result).lower()
    assert ADDRESS.lower() not in text
    assert PREFIX not in text
    assert MAT_ID not in text


# Status decoding and the reported range.


def unique(hass: HomeAssistant, domain: str, suffix: str) -> str:
    return entity_id_for_unique_id(hass, domain, f"{PREFIX}_{suffix}")


async def turn(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": entity_id}, blocking=True
    )


async def choose(hass: HomeAssistant, entity_id: str, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": option},
        blocking=True,
    )


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )


@pytest.mark.parametrize(
    ("code", "low", "high"),
    [
        (0, 25, 43),
        (1, 25, 45),
        (2, 25, 48),
        (3, 25, 50),
        (4, 28, 45),
        (5, 28, 45),
        (9, 28, 45),
    ],
)
async def test_range_for_each_byte_18_code(
    hass: HomeAssistant, mat: FakeMat, code: int, low: int, high: int
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(range=code))
    attributes = state_of(hass, climate_id(hass, "left")).attributes
    assert (attributes["min_temp"], attributes["max_temp"]) == (low, high)


async def test_code_3_mat_accepts_50_and_code_4_refuses_46(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(range=3))
    mat.respond = confirm_with(status(range=3, left=50))
    await set_temp(hass, "left", 50)
    assert mat.controls()[-1].hex() == "b28001020002241e1c23321cfefefefefefefe46"
    assert state_of(hass, climate_id(hass, "left")).attributes["temperature"] == 50

    sent = len(mat.controls())
    await feed_and_settle(hass, mat, status(range=4))
    with pytest.raises(ServiceValidationError):
        await set_temp(hass, "left", 46)
    assert len(mat.controls()) == sent


async def test_turned_off_side_byte_follows_the_range_minimum(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=33, right=34, range=3)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(heat=4, left=25, right=34, range=3))
    await set_mode(hass, "left", "off")
    assert mat.controls()[-1].hex() == "b28001020004241e1c231922fefefefefefefe35"


async def test_stored_default_range_then_code_3_one_side_target_50_survives_reload(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3, left=50, right=0, range=3))
    merged = entry.runtime_data.stored_status
    assert (merged.raw[18], merged.left_target, merged.right_target) == (3, 50, 28)

    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    assert hass_storage[key]["data"]["heating_status"] == merged.raw.hex()
    assert entry.runtime_data.stored_status.raw == merged.raw

    mat.respond = confirm_with(status(heat=3, left=50, right=0, range=3))
    await set_power(hass, "turn_on")
    assert mat.controls()[-1].hex() == "b28001020003241e1c233219fefefefefefefe44"


async def test_remembered_target_invalid_in_a_later_range_reloads_as_30(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=50, right=48, range=3)
    entry = await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(heat=3, left=33, right=0, range=4))
    stored = entry.runtime_data.stored_status
    assert (stored.raw[18], stored.left_target, stored.right_target) == (4, 33, 30)

    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data.stored_status.raw == stored.raw


async def test_store_saves_a_change_of_byte_5_or_byte_18(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    conn = entry.runtime_data
    with patch.object(conn._store, "async_delay_save") as save:
        await feed_and_settle(hass, mat, status(heat=3))
        save.assert_called_once()
        assert conn.stored_status.heat_type == 3
        await feed_and_settle(hass, mat, status(heat=3, range=3))
        assert save.call_count == 2
        assert conn.stored_status.raw[18] == 3


async def test_mode_7_status_names_discharge(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(mode=7))
    assert state_of(hass, unique(hass, "sensor", "mode")).state == "물빼기"


async def test_status_decoding_of_the_new_values(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(volume=0x71, fast=45, ion=0x51)
    await setup_mat(hass, mat)
    assert state_of(hass, unique(hass, "select", "volume")).state == "3단계"
    assert state_of(hass, unique(hass, "sensor", "water_level")).state == "낮음"
    assert state_of(hass, unique(hass, "sensor", "fast_heat_minutes")).state == "30"
    assert state_of(hass, unique(hass, "switch", "fast_heat")).state == "on"
    assert state_of(hass, unique(hass, "binary_sensor", "ion_care_running")).state == "on"
    assert state_of(hass, unique(hass, "select", "ion_care_period")).state == "5시간"

    await feed_and_settle(hass, mat, status(volume=0x07, fast=0, ion=0x60))
    assert state_of(hass, unique(hass, "select", "volume")).state == "음소거"
    assert state_of(hass, unique(hass, "sensor", "water_level")).state == "unknown"
    assert state_of(hass, unique(hass, "switch", "fast_heat")).state == "off"
    assert state_of(hass, unique(hass, "binary_sensor", "ion_care_running")).state == "off"


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (status(model=0xFE), "unavailable"),
        (status(model=0x10), "unavailable"),
        (status(model=0x13), "off"),
        (status(model=0x12), "off"),
        # A mode that carries no byte 17 reports the default model code.
        (status(mode=8, model=0x12), "unavailable"),
    ],
)
async def test_model_code_gates_fast_heat_and_ion_care(
    hass: HomeAssistant, mat: FakeMat, frame: bytes, expected: str
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, frame)
    assert state_of(hass, unique(hass, "switch", "fast_heat")).state == expected
    available = expected != "unavailable"
    for domain, suffix in (
        ("sensor", "fast_heat_minutes"),
        ("button", "ion_care_run"),
        ("binary_sensor", "ion_care_running"),
        ("select", "ion_care_period"),
    ):
        state = state_of(hass, unique(hass, domain, suffix)).state
        assert (state != "unavailable") is available


async def test_unavailable_model_gated_controls_send_nothing(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(model=0x10)
    await setup_mat(hass, mat)
    await turn(hass, "turn_on", unique(hass, "switch", "fast_heat"))
    await press(hass, unique(hass, "button", "ion_care_run"))
    assert mat.controls() == []


async def test_new_entities_wait_for_a_status(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = None
    entry = await setup_mat(hass, mat, wait_status=False)
    await until(lambda: entry.runtime_data.authenticated)
    await hass.async_block_till_done()
    for domain, suffix in (
        ("switch", "lock"),
        ("switch", "fast_heat"),
        ("select", "volume"),
        ("select", "ion_care_period"),
        ("sensor", "water_level"),
        ("sensor", "error_detail"),
        ("sensor", "fast_heat_minutes"),
        ("binary_sensor", "ion_care_running"),
        ("button", "ion_care_run"),
        ("button", "left_match_temperature"),
        ("button", "right_match_temperature"),
        ("select", "off_reservation"),
        ("select", "on_reservation"),
        ("select", "left_sleep_hours"),
        ("select", "right_sleep_hours"),
        ("switch", "sleep"),
        ("number", "on_reservation_temperature"),
    ):
        assert state_of(hass, unique(hass, domain, suffix)).state == "unavailable"


@pytest.mark.parametrize(
    ("byte9", "byte10", "expected"),
    [
        (0, 2, "2: 물 부족 에러(Er 02): 물이 부족한 경우입니다. 물 보충 안내 방법에 따라 물을 보충해주세요."),
        (3, 2, "302: 물 부족 에러(Er 02): 물이 부족한 경우입니다. 물 보충 안내 방법에 따라 물을 보충해주세요."),
        (0, 5, "5: 물탱크 공급 온도 센서 에러(Er 05): 공급 온도에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (2, 5, "205: 물탱크 공급 온도 센서 에러(Er 05): 공급 온도에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (0, 11, "11: 수위 감지 에러(Er 11): 정상 수위 감지가 아닐 경우입니다. 1588-1144로 연락해주세요."),
        (3, 11, "311: 수위 감지 에러(Er 11): 정상 수위 감지가 아닐 경우입니다. 1588-1144로 연락해주세요."),
        (0, 15, "15: 이온 케어 에러(Er 15): 이온 케어 장치에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (0, 16, "16: 물탱크 과열 에러(Er 16): 물탱크의 온도가 과열되었을 경우입니다. 1588-1144로 연락해주세요."),
        (
            0,
            17,
            "17: 난방 이상 에러(Er 17): 난방과 관련된 부품(온도퓨즈, 전류퓨즈)에 이상이 생긴 경우입니다. 1588-1144로 연락해주세요.",
        ),
        (0, 18, "18: 환수 온도 센서 에러(Er 18): 환수 온도 센서에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (0, 19, "19: 환수 온도 센서 에러(Er 18): 환수 온도 센서에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (2, 18, "218: 환수 온도 센서 에러(Er 18): 환수 온도 센서에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (2, 19, "219: 환수 온도 센서 에러(Er 18): 환수 온도 센서에 이상이 발생된 경우입니다. 1588-1144로 연락해주세요."),
        (0, 28, "28: 누수 에러(Er 28): 매트 내부 호스 파손에 의한 누수가 감지되었을 경우입니다. 1588-1144로 연락해주세요."),
        (2, 28, "228: 누수 에러(Er 28): 매트 내부 호스 파손에 의한 누수가 감지되었을 경우입니다. 1588-1144로 연락해주세요."),
        (1, 1, "101: 에러가 발생했습니다. 보일러를 점검해 주세요."),
    ],
)
async def test_error_detail_for_a_code(
    hass: HomeAssistant, mat: FakeMat, byte9: int, byte10: int, expected: str
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, status(mode=5, byte9=byte9, left=byte10))
    assert state_of(hass, unique(hass, "sensor", "error_detail")).state == expected


async def test_error_detail_empty_without_an_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    detail = unique(hass, "sensor", "error_detail")
    assert state_of(hass, detail).state == "unknown"
    await feed_and_settle(hass, mat, status(mode=5, byte9=0, left=0))
    assert state_of(hass, detail).state == "unknown"


# Lock, volume, fast heat, ion care and the same-temperature buttons.


async def test_lock_heating_frame_confirmation_and_no_op(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    lock = unique(hass, "switch", "lock")
    mat.respond = confirm_with(status(flags=0x06))
    await turn(hass, "turn_on", lock)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001060002241e1c231e1cfefefefefefefe36"
    ]
    await hass.async_block_till_done()
    assert state_of(hass, lock).state == "on"
    await turn(hass, "turn_on", lock)
    assert len(mat.controls()) == 1

    mat.respond = confirm_with(HEATING)
    await turn(hass, "turn_off", lock)
    assert mat.controls()[-1].hex() == "b28001020002241e1c231e1cfefefefefefefe32"


OFF_RESERVED = status(
    mode=6, flags=0, heat=0, left=1, right=5, byte9=0xFE, byte12=7
)


async def test_lock_off_frame_echoes_the_reservation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = OFF_RESERVED
    await setup_mat(hass, mat)
    mat.respond = confirm_with(
        status(mode=6, flags=0x04, heat=0, left=1, right=5, byte9=0xFE, byte12=7)
    )
    await turn(hass, "turn_on", unique(hass, "switch", "lock"))
    assert [frame.hex() for frame in mat.controls()] == [
        "b280060400fe241e1cfe0105070507fefefefef5"
    ]


@pytest.mark.parametrize("start", [HEATING, OFF_RESERVED])
async def test_lock_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat, start: bytes
) -> None:
    mat.first_status = start
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await turn(hass, "turn_on", unique(hass, "switch", "lock"))
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 2
    assert mat.controls()[0] == mat.controls()[1]


@pytest.mark.parametrize("frame", [status(mode=2, flags=0), SLEEP])
async def test_lock_refused_in_any_other_state(
    hass: HomeAssistant, mat: FakeMat, frame: bytes
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, frame)
    with pytest.raises(ServiceValidationError):
        await turn(hass, "turn_on", unique(hass, "switch", "lock"))
    assert mat.controls() == []


async def test_volume_frame_confirmation_no_op_and_refusal(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    volume = unique(hass, "select", "volume")
    await choose(hass, volume, "음소거")
    assert mat.controls() == []

    mat.respond = confirm_with(status(volume=0x23))
    await choose(hass, volume, "2단계")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001022002fe1e1cfe1e1cfefefefefefefe07"
    ]

    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await choose(hass, volume, "1단계")
    assert len(mat.controls()) == 1


async def test_volume_resend_and_final_error(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await choose(hass, unique(hass, "select", "volume"), "3단계")
    assert not isinstance(raised.value, ServiceValidationError)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001023002fe1e1cfe1e1cfefefefefefefe17"
    ] * 2


async def test_fast_heat_frames_no_op_and_refusal(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    fast = unique(hass, "switch", "fast_heat")
    await turn(hass, "turn_off", fast)
    assert mat.controls() == []

    mat.respond = confirm_with(status(fast=10))
    await turn(hass, "turn_on", fast)
    assert [frame.hex() for frame in mat.controls()] == [
        "b28007020002241e1c231e1c01fefefefefefe3b"
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "sensor", "fast_heat_minutes")).state == "10"
    await turn(hass, "turn_on", fast)
    assert len(mat.controls()) == 1

    mat.respond = confirm_with(HEATING)
    await turn(hass, "turn_off", fast)
    assert mat.controls()[-1].hex() == "b28007020002241e1c231e1c00fefefefefefe3a"

    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await turn(hass, "turn_on", fast)


async def test_fast_heat_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await turn(hass, "turn_on", unique(hass, "switch", "fast_heat"))
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 2


@pytest.mark.parametrize(
    ("first", "expected"),
    [
        (HEATING, "b28008020002241e1c231e1c01fefefe60fefe9e"),
        (status(ion=0x00), "b28008020002241e1c231e1c01fefefefefefe3c"),
    ],
)
async def test_ion_care_run_frame(
    hass: HomeAssistant, mat: FakeMat, first: bytes, expected: str
) -> None:
    mat.first_status = first
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(ion=0x61))
    await press(hass, unique(hass, "button", "ion_care_run"))
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_ion_care_run_no_op_and_refusal(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(ion=0x61)
    await setup_mat(hass, mat)
    await press(hass, unique(hass, "button", "ion_care_run"))
    assert mat.controls() == []
    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await press(hass, unique(hass, "button", "ion_care_run"))
    assert mat.controls() == []


async def test_ion_care_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, unique(hass, "button", "ion_care_run"))
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 2


@pytest.mark.parametrize(
    ("first", "confirm", "expected"),
    [
        (
            HEATING,
            status(ion=0x30),
            "b28008020002241e1c231e1c01fefefe30fefe6e",
        ),
        (
            status(ion=0x61),
            status(ion=0x31),
            "b28008020002241e1c231e1c00fefefe30fefe6d",
        ),
    ],
)
async def test_ion_care_period_frame_idle_and_running(
    hass: HomeAssistant, mat: FakeMat, first: bytes, confirm: bytes, expected: str
) -> None:
    mat.first_status = first
    await setup_mat(hass, mat)
    mat.respond = confirm_with(confirm)
    await choose(hass, unique(hass, "select", "ion_care_period"), "3시간")
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_ion_care_period_no_op_refusal_and_final_error(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    period = unique(hass, "select", "ion_care_period")
    await choose(hass, period, "6시간")
    assert mat.controls() == []

    with pytest.raises(HomeAssistantError) as raised:
        await choose(hass, period, "3시간")
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 2

    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await choose(hass, period, "3시간")
    assert len(mat.controls()) == 2


async def test_ion_care_period_keeps_byte_12_when_running_arrives(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0.1)
    await setup_mat(hass, mat)
    task = hass.async_create_task(
        choose(hass, unique(hass, "select", "ion_care_period"), "3시간")
    )
    await until(lambda: len(mat.controls()) == 1)
    mat.feed(status(ion=0x61))
    await until(lambda: len(mat.controls()) == 2)
    mat.feed(status(ion=0x31))
    await task
    assert [frame.hex() for frame in mat.controls()] == [
        "b28008020002241e1c231e1c01fefefe30fefe6e"
    ] * 2


@pytest.mark.parametrize(
    ("side", "first", "confirm", "expected"),
    [
        (
            "left",
            status(left=33, right=30),
            status(left=33, right=33),
            "b28001020002241e1c232121fefefefefefefe3a",
        ),
        (
            "right",
            status(left=33, right=30),
            status(left=30, right=30),
            "b28001020002241e1c231e1efefefefefefefe34",
        ),
        (
            "left",
            status(left=31, right=31),
            status(left=31, right=31),
            "b28001020002241e1c231f1ffefefefefefefe36",
        ),
        (
            "left",
            status(heat=3, left=33, right=28),
            status(left=33, right=33),
            "b28001020002241e1c232121fefefefefefefe3a",
        ),
    ],
)
async def test_match_temperature_frame(
    hass: HomeAssistant,
    mat: FakeMat,
    side: str,
    first: bytes,
    confirm: bytes,
    expected: str,
) -> None:
    mat.first_status = first
    await setup_mat(hass, mat)
    mat.respond = confirm_with(confirm)
    await press(hass, unique(hass, "button", f"{side}_match_temperature"))
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_match_temperature_unconfirmed_and_refusals(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=33, right=30)
    await setup_mat(hass, mat)
    button = unique(hass, "button", "left_match_temperature")
    mat.respond = confirm_with(status(left=33, right=30))
    with pytest.raises(HomeAssistantError) as raised:
        await press(hass, button)
    assert not isinstance(raised.value, ServiceValidationError)
    assert len(mat.controls()) == 2

    mat.respond = None
    await feed_and_settle(hass, mat, status(heat=4, left=28, right=30))
    with pytest.raises(ServiceValidationError):
        await press(hass, button)
    await feed_and_settle(hass, mat, OFF_STATUS)
    with pytest.raises(ServiceValidationError):
        await press(hass, button)
    await feed_and_settle(hass, mat, SLEEP)
    with pytest.raises(ServiceValidationError):
        await press(hass, button)
    assert len(mat.controls()) == 2


MOVED_OFF = status(mode=6, flags=0, heat=0, left=0, right=0, byte9=0xFE, ion=0x00)


@pytest.mark.parametrize(
    ("start", "command", "moved"),
    [
        (HEATING, lambda hass: turn(hass, "turn_on", unique(hass, "switch", "lock")), MOVED_OFF),
        (OFF_RESERVED, lambda hass: turn(hass, "turn_on", unique(hass, "switch", "lock")), HEATING),
        (HEATING, lambda hass: choose(hass, unique(hass, "select", "volume"), "2단계"), MOVED_OFF),
        (HEATING, lambda hass: turn(hass, "turn_on", unique(hass, "switch", "fast_heat")), MOVED_OFF),
        (HEATING, lambda hass: press(hass, unique(hass, "button", "ion_care_run")), MOVED_OFF),
        (HEATING, lambda hass: choose(hass, unique(hass, "select", "ion_care_period"), "3시간"), MOVED_OFF),
        (
            status(left=33, right=30),
            lambda hass: press(hass, unique(hass, "button", "left_match_temperature")),
            MOVED_OFF,
        ),
        (
            status(left=33, right=30),
            lambda hass: press(hass, unique(hass, "button", "left_match_temperature")),
            status(heat=4, left=28, right=30),
        ),
    ],
)
async def test_new_command_stops_on_a_resend_after_the_state_changed(
    hass: HomeAssistant,
    mat: FakeMat,
    monkeypatch: pytest.MonkeyPatch,
    start: bytes,
    command: Callable[[HomeAssistant], Any],
    moved: bytes,
) -> None:
    monkeypatch.setattr(connection, "CONFIRM_TIMEOUT", 0.2)
    mat.first_status = start
    await setup_mat(hass, mat)
    task = hass.async_create_task(command(hass))
    await until(lambda: len(mat.controls()) == 1)
    mat.feed(moved)
    with pytest.raises(HomeAssistantError) as raised:
        await task
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 1


# Powering on returns to the last state.


LEFT_LAST = status(heat=3, left=33, right=28)
RIGHT_LAST = status(heat=4, left=28, right=34)
HALF_CONFIRM = {
    3: status(heat=3, left=33, right=0),
    4: status(heat=4, left=0, right=34),
}


async def one_side_last(
    hass: HomeAssistant, mat: FakeMat, last: bytes
) -> MockConfigEntry:
    """Both sides seen at 33 and 34, then only one side left on, then off."""
    mat.first_status = status(left=33, right=34)
    entry = await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, last)
    await feed_and_settle(hass, mat, OFF_STATUS)
    return entry


@pytest.mark.parametrize(
    ("last", "heat", "expected"),
    [
        (LEFT_LAST, 3, "b28001020003241e1c23211cfefefefefefefe36"),
        (RIGHT_LAST, 4, "b28001020004241e1c231c22fefefefefefefe38"),
    ],
)
async def test_power_on_after_one_side_exact_frame_resend_and_final_error(
    hass: HomeAssistant, mat: FakeMat, last: bytes, heat: int, expected: str
) -> None:
    await one_side_last(hass, mat, last)
    answers = iter([None, HALF_CONFIRM[heat]])
    mat.respond = lambda _frame: next(answers)
    await set_power(hass, "turn_on")
    assert [frame.hex() for frame in mat.controls()] == [expected] * 2

    await feed_and_settle(hass, mat, OFF_STATUS)
    mat.respond = None
    with pytest.raises(HomeAssistantError) as raised:
        await set_power(hass, "turn_on")
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 4


@pytest.mark.parametrize("last", [LEFT_LAST, RIGHT_LAST])
async def test_power_on_one_side_fails_on_a_wrong_reply(
    hass: HomeAssistant, mat: FakeMat, last: bytes
) -> None:
    await one_side_last(hass, mat, last)
    mat.respond = confirm_with(status(heat=2, left=33, right=34))
    with pytest.raises(HomeAssistantError) as raised:
        await set_power(hass, "turn_on")
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert len(mat.controls()) == 2


async def test_power_on_after_both_sides_is_unchanged(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=33, right=34)
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, OFF_STATUS)
    mat.respond = confirm_with(status(left=33, right=34))
    await set_power(hass, "turn_on")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c232122fefefefefefefe3b"
    ]


async def test_heat_type_survives_a_reload(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await one_side_last(hass, mat, RIGHT_LAST)
    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data.stored_status.heat_type == 4
    mat.respond = confirm_with(HALF_CONFIRM[4])
    await set_power(hass, "turn_on")
    assert mat.controls()[-1].hex() == "b28001020004241e1c231c22fefefefefefefe38"


async def test_both_sides_frame_stored_before_this_change_powers_on_both(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    mat.first_status = OFF_STATUS
    entry = make_entry(hass)
    hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.ble_mat.{entry.entry_id}",
        "data": {"enabled": True, "heating_status": HEATING.hex()},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    mat.respond = confirm_with(HEATING)
    await set_power(hass, "turn_on")
    assert [frame.hex() for frame in mat.controls()] == [
        "b28001020002241e1c231e1cfefefefefefefe32"
    ]


@pytest.mark.parametrize(
    ("side", "value", "confirm", "expected"),
    [
        # Its side was on in the last state: that state, the target replaced when given.
        ("left", None, HALF_CONFIRM[3], "b28001020003241e1c23211cfefefefefefefe36"),
        (
            "left",
            35,
            status(heat=3, left=35, right=0),
            "b28001020003241e1c23231cfefefefefefefe38",
        ),
        # Its side was off in the last state: only that side, the remembered target or the given.
        ("right", None, HALF_CONFIRM[4], "b28001020004241e1c231c22fefefefefefefe38"),
        (
            "right",
            31,
            status(heat=4, left=0, right=31),
            "b28001020004241e1c231c1ffefefefefefefe35",
        ),
    ],
)
async def test_thermostat_on_while_off_follows_the_last_state(
    hass: HomeAssistant,
    mat: FakeMat,
    side: str,
    value: int | None,
    confirm: bytes,
    expected: str,
) -> None:
    await one_side_last(hass, mat, LEFT_LAST)
    mat.respond = confirm_with(confirm)
    if value is None:
        await set_mode(hass, side, "heat")
    else:
        await set_temp(hass, side, value, "heat")
    assert [frame.hex() for frame in mat.controls()] == [expected]


async def test_other_sides_target_survives_the_whole_cycle(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(left=33, right=34)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(RIGHT_LAST)
    await set_mode(hass, "left", "off")
    await hass.async_block_till_done()
    mat.respond = confirm_with(OFF_STATUS)
    await set_mode(hass, "right", "off")
    await hass.async_block_till_done()
    mat.respond = confirm_with(RIGHT_LAST)
    await set_power(hass, "turn_on")
    await hass.async_block_till_done()
    mat.respond = confirm_with(status(left=33, right=34))
    await set_mode(hass, "left", "heat")
    assert mat.controls()[-1].hex() == "b28001020002241e1c232122fefefefefefefe3b"


async def test_one_side_first_seen_goes_through_off_and_on_across_a_reload(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(heat=3, left=33, right=0)
    entry = await setup_mat(hass, mat)
    mat.respond = confirm_with(OFF_STATUS)
    await set_mode(hass, "left", "off")
    await hass.async_block_till_done()

    mat.first_status = OFF_STATUS
    await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    mat.respond = confirm_with(HALF_CONFIRM[3])
    await set_power(hass, "turn_on")
    assert mat.controls()[-1].hex() == "b28001020003241e1c23211cfefefefefefefe36"


# The Bluetooth config flow.


async def test_reauth_aborts_for_a_bluetooth_entry(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = make_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "bluetooth_reauth_unsupported"


async def _flow_to_bluetooth(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result.get("type") is FlowResultType.MENU
    return dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "bluetooth_mat"}
        )
    )


async def _flow_to_pair(hass: HomeAssistant, mat: FakeMat) -> dict[str, Any]:
    mat.discovered = [transport.DiscoveredMat(ADDRESS, "매트")]
    result = await _flow_to_bluetooth(hass)
    assert result.get("step_id") == "bluetooth_mat"
    result = dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    )
    assert result.get("step_id") == "bluetooth_mat_pair"
    assert mat.connect_calls == 0
    return result


async def _submit(hass: HomeAssistant, flow_id: str) -> Any:
    return await hass.config_entries.flow.async_configure(flow_id, {})


async def test_flow_no_devices(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_bluetooth(hass)
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "no_devices"


async def test_flow_bluetooth_unavailable(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.discovered = RuntimeError("no bluetooth")
    result = await _flow_to_bluetooth(hass)
    assert result.get("reason") == "bluetooth_unavailable"


async def test_flow_hides_configured_and_aborts_when_picked(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.discovered = [transport.DiscoveredMat(ADDRESS, "매트")]
    picker = await _flow_to_bluetooth(hass)
    make_entry(hass)
    result = dict(
        await hass.config_entries.flow.async_configure(
            picker["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    )
    assert result.get("reason") == "already_configured"

    assert (await _flow_to_bluetooth(hass)).get("reason") == "no_devices"


async def test_flow_success_sends_registration_first(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    with patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None):
        done = await _submit(hass, result["flow_id"])
        await hass.async_block_till_done()
    assert done.get("type") is FlowResultType.CREATE_ENTRY
    assert done.get("data") == {CONF_ADDRESS: ADDRESS, CONF_MAT_ID: MAT_ID}
    assert mat.writes[0][1] == 0x01 and mat.writes[0][2:8] == b"\xfe" * 6
    assert mat.writes[1][2:8].hex() == MAT_ID
    assert mat.disconnected[0] is mat.clients[0]


async def test_flow_refusal_keeps_form_and_closes(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.register_replies = [reply(0)]
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_refused"}
    assert mat.disconnected == mat.clients


async def test_flow_timeout_keeps_form(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.register_replies = [None]
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_connect_failed"}
    assert mat.disconnected == mat.clients


async def test_flow_connect_failure_keeps_form(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.connect_error = OSError("no route")
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_connect_failed"}


async def test_flow_no_status_keeps_form(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.first_status = None
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_no_status"}
    assert mat.disconnected == mat.clients


async def test_flow_status_before_reply_2_does_not_count(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.status_before_reply2 = HEATING
    mat.first_status = None
    form = await _submit(hass, result["flow_id"])
    assert form.get("errors") == {"base": "mat_no_status"}
    assert mat.disconnected == mat.clients


async def test_flow_uses_the_status_after_reply_2(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.status_before_reply2 = status(flags=0x03)  # a single mat, were it to count
    with patch.object(hass.config_entries, "async_forward_entry_setups", return_value=None):
        done = await _submit(hass, result["flow_id"])
        await hass.async_block_till_done()
    assert done.get("type") is FlowResultType.CREATE_ENTRY


async def test_flow_single_mat_aborts(hass: HomeAssistant, mat: FakeMat) -> None:
    result = await _flow_to_pair(hass, mat)
    mat.first_status = status(flags=0x03)
    done = await _submit(hass, result["flow_id"])
    assert done.get("type") is FlowResultType.ABORT
    assert done.get("reason") == "single_mat"
    assert not hass.config_entries.async_entries(DOMAIN)
    assert mat.disconnected == mat.clients


# Instant reservations and sleep. Expected control frames are literals, not protocol output.


SLEEP_READY = status(left=31, right=32)
OFF_RESERVATION = status(mode=2, byte12=5)
SLEEP_RUNNING = status(mode=3, left=31, right=32)


async def schedule_command(hass: HomeAssistant, command: str) -> None:
    calls: dict[str, tuple[str, str, str, dict[str, str]]] = {
        "off_set": ("select", "off_reservation", "select_option", {"option": "5시간"}),
        "off_cancel": ("select", "off_reservation", "select_option", {"option": "해제"}),
        "on_set": ("select", "on_reservation", "select_option", {"option": "5시간"}),
        "on_cancel": ("select", "on_reservation", "select_option", {"option": "해제"}),
        "sleep_start": ("switch", "sleep", "turn_on", {}),
        "sleep_stop": ("switch", "sleep", "turn_off", {}),
    }
    domain, suffix, service, data = calls[command]
    await hass.services.async_call(
        domain,
        service,
        {"entity_id": unique(hass, domain, suffix), **data},
        blocking=True,
    )


async def set_on_reservation_temperature(hass: HomeAssistant, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": unique(hass, "number", "on_reservation_temperature"), "value": value},
        blocking=True,
    )


@pytest.mark.parametrize("mode", [1, 2, 3, 9])
async def test_off_reservation_frame_and_active_confirmation(
    hass: HomeAssistant, mat: FakeMat, mode: int
) -> None:
    mat.first_status = status(mode=mode, flags=0x0E, volume=0x23, left=31, right=32)
    await setup_mat(hass, mat)
    # A different value still confirms the reservation; the wire meaning is not yet proven.
    mat.respond = confirm_with(status(mode=2, byte12=9))
    await schedule_command(hass, "off_set")
    assert mat.controls() == [
        bytes.fromhex("b28002062002241e1c231f2005fefefefefefe63")
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "select", "off_reservation")).state == "9시간"


async def test_off_reservation_cancel_frame_and_mode_confirmation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(mode=2, flags=0x0E, volume=0x23, left=31, right=32, byte12=5)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(HEATING)
    await schedule_command(hass, "off_cancel")
    assert mat.controls() == [
        bytes.fromhex("b28001062002241e1c231f20fefefefefefefe5b")
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "select", "off_reservation")).state == "해제"


async def test_on_reservation_fractional_temperature_frame_and_confirmation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(mode=6, flags=0x0C, volume=0x23)
    await setup_mat(hass, mat)
    temperature = unique(hass, "number", "on_reservation_temperature")
    initial = state_of(hass, temperature)
    assert float(initial.state) == 30
    assert initial.attributes["step"] == 1
    assert (initial.attributes["min"], initial.attributes["max"]) == (28, 45)
    await set_on_reservation_temperature(hass, 32.5)
    await hass.async_block_till_done()
    assert float(state_of(hass, temperature).state) == 33
    assert mat.controls() == []
    mat.respond = confirm_with(status(mode=6, flags=0, left=1, right=9))
    await schedule_command(hass, "on_set")
    assert mat.controls() == [
        bytes.fromhex("b280060420fe241e1cfe010500fefe21fefefe21")
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "select", "on_reservation")).state == "9시간"


@pytest.mark.parametrize("mode", [2, 6])
async def test_on_reservation_cancel_echoes_its_value_and_may_power_on(
    hass: HomeAssistant, mat: FakeMat, mode: int
) -> None:
    mat.first_status = status(
        mode=mode, flags=0x0C, volume=0x23, left=1, right=5, byte13=5
    )
    await setup_mat(hass, mat)
    answers = iter([HEATING, OFF_STATUS])
    mat.respond = lambda _frame: next(answers)
    await schedule_command(hass, "on_cancel")
    assert mat.controls() == [
        bytes.fromhex("b280060620fe241e1cfe0105fefefe00fefefe00"),
        bytes.fromhex("b280060000fe241e1cfe0000000000fefefefed8"),
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "select", "on_reservation")).state == "해제"
    assert state_of(hass, unique(hass, "switch", "power")).state == "off"


@pytest.mark.parametrize("mode", [1, 2, 9])
async def test_sleep_two_sides_frame_and_mode_confirmation(
    hass: HomeAssistant, mat: FakeMat, mode: int
) -> None:
    mat.first_status = status(mode=mode, flags=0x0E, volume=0x23, left=31, right=32)
    await setup_mat(hass, mat)
    await choose(hass, unique(hass, "select", "left_sleep_hours"), "5시간")
    await choose(hass, unique(hass, "select", "right_sleep_hours"), "10시간")
    assert mat.controls() == []
    mat.respond = confirm_with(status(mode=3, left=31, right=32, byte13=6, byte14=9))
    await schedule_command(hass, "sleep_start")
    assert mat.controls() == [
        bytes.fromhex("b28003062002241e1c231f2000050a0101fefe78")
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "sleep")).state == "on"
    # Settings remain the user's chosen hours, independently of status hour bytes.
    assert state_of(hass, unique(hass, "select", "left_sleep_hours")).state == "5시간"
    assert state_of(hass, unique(hass, "select", "right_sleep_hours")).state == "10시간"


@pytest.mark.parametrize(
    ("heat", "left", "right", "expected"),
    [
        (3, 31, 45, "b28003020003241e1c231f2d0006ff0101fefe58"),
        (4, 45, 32, "b28003020004241e1c232d2000ff090101fefe5d"),
    ],
)
@pytest.mark.parametrize("mode", [2, 9])
async def test_sleep_from_reservation_uses_byte_5_and_checks_only_the_selected_side(
    hass: HomeAssistant, mat: FakeMat, heat: int, left: int, right: int, expected: str,
    mode: int,
) -> None:
    mat.first_status = status(mode=mode, heat=heat, left=left, right=right)
    await setup_mat(hass, mat)
    await choose(hass, unique(hass, "select", "left_sleep_hours"), "6시간")
    await choose(hass, unique(hass, "select", "right_sleep_hours"), "9시간")
    mat.respond = confirm_with(status(mode=3, heat=heat, left=left, right=right))
    await schedule_command(hass, "sleep_start")
    assert mat.controls() == [bytes.fromhex(expected)]


async def test_sleep_stop_frame_and_confirmation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(mode=3, flags=0x0E, volume=0x23, left=31, right=32)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(HEATING)
    await schedule_command(hass, "sleep_stop")
    assert mat.controls() == [
        bytes.fromhex("b28003062002241e1c231f200000000000fefe67")
    ]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "sleep")).state == "off"


SCHEDULE_COMMANDS = [
    ("off_set", HEATING, status(mode=2, byte12=9), "b28002020002241e1c231e1c05fefefefefefe3a"),
    ("off_cancel", OFF_RESERVATION, HEATING, "b28001020002241e1c231e1cfefefefefefefe32"),
    ("on_set", OFF_STATUS, OFF_RESERVED, "b280060000fe241e1cfe010500fefe1efefefefa"),
    ("on_cancel", OFF_RESERVED, OFF_STATUS, "b280060200fe241e1cfe0105fefefe00fefefedc"),
    ("sleep_start", SLEEP_READY, SLEEP_RUNNING, "b28003020002241e1c231f200008080101fefe55"),
    ("sleep_stop", SLEEP_RUNNING, HEATING, "b28003020002241e1c231f200000000000fefe43"),
]


@pytest.mark.parametrize(("command", "start", "confirmed", "expected"), SCHEDULE_COMMANDS)
async def test_schedule_commands_resend_confirm_and_report_final_error(
    hass: HomeAssistant,
    mat: FakeMat,
    command: str,
    start: bytes,
    confirmed: bytes,
    expected: str,
) -> None:
    mat.first_status = start
    await setup_mat(hass, mat)
    answers = iter([None, confirmed])
    mat.respond = lambda _frame: next(answers)
    await schedule_command(hass, command)
    assert mat.controls() == [bytes.fromhex(expected)] * 2

    await feed_and_settle(hass, mat, start)
    mat.respond = None
    with pytest.raises(HomeAssistantError) as raised:
        await schedule_command(hass, command)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert mat.controls() == [bytes.fromhex(expected)] * 4


@pytest.mark.parametrize(
    ("command", "start", "moved", "confirmed", "first", "second"),
    [
        (
            "off_set", HEATING,
            status(mode=3, flags=6, volume=0x23, left=32, right=33, byte9=0x25),
            OFF_RESERVATION,
            "b28002020002241e1c231e1c05fefefefefefe3a",
            "b28002062002241e1c25202105fefefefefefe67",
        ),
        (
            "off_cancel", OFF_RESERVATION,
            status(mode=2, flags=6, volume=0x23, left=32, right=33, byte9=0x25),
            HEATING,
            "b28001020002241e1c231e1cfefefefefefefe32",
            "b28001062002241e1c252021fefefefefefefe5f",
        ),
        (
            "on_set", OFF_STATUS,
            status(mode=6, flags=4, volume=0x23, left=1, right=9),
            OFF_RESERVED,
            "b280060000fe241e1cfe010500fefe1efefefefa",
            "b280060420fe241e1cfe010500fefe1efefefe1e",
        ),
        (
            "on_cancel", OFF_RESERVED,
            status(mode=2, flags=4, volume=0x23, byte13=9),
            OFF_STATUS,
            "b280060200fe241e1cfe0105fefefe00fefefedc",
            "b280060620fe241e1cfe0109fefefe00fefefe04",
        ),
        (
            "sleep_start", SLEEP_READY,
            status(flags=6, volume=0x23, left=32, right=33, byte9=0x25),
            SLEEP_RUNNING,
            "b28003020002241e1c231f200008080101fefe55",
            "b28003062002241e1c2520210008080101fefe7d",
        ),
        (
            "sleep_stop", SLEEP_RUNNING,
            status(mode=3, flags=6, volume=0x23, left=32, right=33, byte9=0x25),
            HEATING,
            "b28003020002241e1c231f200000000000fefe43",
            "b28003062002241e1c2520210000000000fefe6b",
        ),
    ],
)
async def test_schedule_resends_rebuild_echoes_without_changing_the_action(
    hass: HomeAssistant,
    mat: FakeMat,
    command: str,
    start: bytes,
    moved: bytes,
    confirmed: bytes,
    first: str,
    second: str,
) -> None:
    mat.first_status = start
    await setup_mat(hass, mat)
    mat.during_write = moved  # before the confirmation window, so this cannot confirm
    answers = iter([None, confirmed])
    mat.respond = lambda _frame: next(answers)
    await schedule_command(hass, command)
    assert mat.controls() == [bytes.fromhex(first), bytes.fromhex(second)]


@pytest.mark.parametrize(
    ("command", "start", "moved"),
    [
        ("off_set", HEATING, status(mode=2, flags=0)),
        ("off_set", HEATING, status(mode=3, heat=0)),
        ("off_cancel", OFF_RESERVATION, OFF_STATUS),
        ("off_cancel", OFF_RESERVATION, HEATING),
        ("on_set", OFF_STATUS, HEATING),
        ("on_set", OFF_STATUS, status(mode=2, flags=0, byte13=5)),
        ("sleep_start", SLEEP_READY, status(heat=3, left=31, right=32)),
        ("sleep_start", SLEEP_READY, status(left=34, right=32)),
        ("sleep_start", SLEEP_READY, status(model=0x22, left=31, right=32)),
        ("sleep_start", SLEEP_READY, SLEEP_RUNNING),
        ("sleep_stop", SLEEP_RUNNING, HEATING),
        ("sleep_stop", SLEEP_RUNNING, status(mode=3, flags=0)),
    ],
)
async def test_schedule_resend_stops_when_the_original_condition_no_longer_holds(
    hass: HomeAssistant, mat: FakeMat, command: str, start: bytes, moved: bytes
) -> None:
    mat.first_status = start
    await setup_mat(hass, mat)
    mat.during_write = moved
    with pytest.raises(HomeAssistantError) as raised:
        await schedule_command(hass, command)
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    expected = next(row[3] for row in SCHEDULE_COMMANDS if row[0] == command)
    assert mat.controls() == [bytes.fromhex(expected)]


UNRESERVED_STATES = [
    HEATING,
    OFF_STATUS,
    status(mode=3, flags=0),
    *[status(mode=mode) for mode in (4, 5, 7, 8, 9, 99)],
    status(heat=0),
]
NO_OP_SCHEDULES = [
    *[(command, frame) for command in ("off_cancel", "on_cancel", "sleep_stop")
      for frame in UNRESERVED_STATES],
    ("off_cancel", OFF_RESERVED),
    ("off_cancel", status(mode=2, flags=0)),
    ("off_cancel", SLEEP_RUNNING),
    ("on_cancel", OFF_RESERVATION),
    ("on_cancel", SLEEP_RUNNING),
    ("sleep_stop", OFF_RESERVATION),
    ("sleep_stop", OFF_RESERVED),
    ("sleep_stop", status(mode=2, flags=0)),
    ("sleep_start", SLEEP_RUNNING),
    ("sleep_start", status(mode=3, heat=0, model=0x22)),
]


@pytest.mark.parametrize(("command", "frame"), NO_OP_SCHEDULES)
async def test_schedule_defined_no_ops_in_every_state(
    hass: HomeAssistant, mat: FakeMat, command: str, frame: bytes
) -> None:
    mat.first_status = frame
    await setup_mat(hass, mat)
    await schedule_command(hass, command)
    assert mat.controls() == []


@pytest.mark.parametrize(
    ("command", "frame"),
    [
        *[("off_set", status(mode=mode)) for mode in (4, 5, 6, 7, 8)],
        ("off_set", status(mode=2, flags=0)),
        ("off_set", status(mode=3, flags=0)),
        *[("off_set", status(mode=mode, heat=heat)) for mode in (1, 2, 3, 9) for heat in (0, 9)],
        *[("on_set", status(mode=mode)) for mode in (1, 2, 3, 4, 5, 7, 8, 9)],
        ("on_set", status(mode=2, flags=0)),
        *[("sleep_start", status(mode=mode)) for mode in (4, 5, 6, 7, 8)],
        ("sleep_start", status(mode=2, flags=0)),
        ("sleep_start", status(mode=3, flags=0)),
        *[("sleep_start", status(mode=mode, heat=heat)) for mode in (1, 2, 9) for heat in (0, 9)],
        ("off_set", status(mode=9, flags=0)),
        ("sleep_start", status(mode=9, flags=0)),
    ],
)
async def test_schedule_commands_refuse_other_states_and_invalid_heat_types(
    hass: HomeAssistant, mat: FakeMat, command: str, frame: bytes
) -> None:
    mat.first_status = frame
    await setup_mat(hass, mat)
    expected = (
        connection.ERROR_SLEEP_STATE if command == "sleep_start"
        else connection.ERROR_RESERVATION_STATE
    )
    with pytest.raises(ServiceValidationError) as raised:
        await schedule_command(hass, command)
    assert str(raised.value) == expected
    assert mat.controls() == []


@pytest.mark.parametrize("mode", [2, 3, 9])
@pytest.mark.parametrize(
    "command",
    [
        lambda hass: set_temp(hass, "left", 31),
        lambda hass: set_mode(hass, "left", "heat"),
        lambda hass: set_mode(hass, "left", "off"),
        lambda hass: set_power(hass, "turn_on"),
        lambda hass: turn(hass, "turn_on", unique(hass, "switch", "lock")),
        lambda hass: turn(hass, "turn_off", unique(hass, "switch", "lock")),
        lambda hass: choose(hass, unique(hass, "select", "volume"), "2단계"),
        lambda hass: turn(hass, "turn_on", unique(hass, "switch", "fast_heat")),
        lambda hass: turn(hass, "turn_off", unique(hass, "switch", "fast_heat")),
        lambda hass: press(hass, unique(hass, "button", "ion_care_run")),
        lambda hass: choose(hass, unique(hass, "select", "ion_care_period"), "3시간"),
        lambda hass: press(hass, unique(hass, "button", "left_match_temperature")),
        lambda hass: press(hass, unique(hass, "button", "right_match_temperature")),
    ],
)
async def test_other_controls_stay_refused_in_reservation_and_sleep(
    hass: HomeAssistant,
    mat: FakeMat,
    mode: int,
    command: Callable[[HomeAssistant], Any],
) -> None:
    mat.first_status = status(mode=mode)
    await setup_mat(hass, mat)
    with pytest.raises(ServiceValidationError):
        await command(hass)
    assert mat.controls() == []


@pytest.mark.parametrize(
    "frame",
    [status(mode=4), status(mode=5), status(mode=7), status(mode=8), status(heat=0)],
)
async def test_power_off_stays_refused_in_unaccepted_modes(
    hass: HomeAssistant, mat: FakeMat, frame: bytes
) -> None:
    mat.first_status = frame
    await setup_mat(hass, mat)
    with pytest.raises(ServiceValidationError):
        await set_power(hass, "turn_off")
    assert mat.controls() == []


@pytest.mark.parametrize(
    ("suffix", "frame", "expected"),
    [
        ("off_reservation", OFF_RESERVATION, "5시간"),
        ("off_reservation", status(mode=2, byte12=99), "설정됨"),
        ("off_reservation", HEATING, "해제"),
        ("on_reservation", OFF_RESERVED, "5시간"),
        ("on_reservation", status(mode=6, left=1, right=99), "설정됨"),
        ("on_reservation", status(mode=2, flags=0, byte13=7), "7시간"),
        ("on_reservation", status(mode=2, flags=0, byte13=99), "설정됨"),
        ("on_reservation", OFF_STATUS, "해제"),
    ],
)
async def test_reservation_select_active_inactive_and_unknown_values(
    hass: HomeAssistant, mat: FakeMat, suffix: str, frame: bytes, expected: str
) -> None:
    await setup_mat(hass, mat)
    await feed_and_settle(hass, mat, frame)
    entity = unique(hass, "select", suffix)
    assert state_of(hass, entity).state == expected
    await feed_and_settle(hass, mat, HEATING)
    assert state_of(hass, entity).state == "해제"


@pytest.mark.parametrize("model", [0xFE, 0x11, 0x12, 0x13])
async def test_reservation_and_sleep_option_lists(
    hass: HomeAssistant, mat: FakeMat, model: int
) -> None:
    mat.first_status = status(model=model)
    await setup_mat(hass, mat)
    off = state_of(hass, unique(hass, "select", "off_reservation"))
    hours = range(1, 15) if model == 0x13 else range(3, 13)
    assert off.attributes["options"] == ["해제", *[f"{h}시간" for h in hours]]
    on = state_of(hass, unique(hass, "select", "on_reservation"))
    assert on.attributes["options"] == ["해제", *[f"{h}시간" for h in range(3, 13)]]
    for side in ("left", "right"):
        sleep_hours = state_of(hass, unique(hass, "select", f"{side}_sleep_hours"))
        assert sleep_hours.attributes["options"] == [f"{h}시간" for h in range(5, 11)]
        assert sleep_hours.state == "8시간"


@pytest.mark.parametrize(
    ("model", "left", "right"),
    [
        (0xFE, 29, 30), (0xFE, 30, 38),
        (0x02, 29, 30), (0x02, 30, 36),
        (0x12, 29, 30), (0x12, 30, 34),
        (0x22, 31, 32), (0xFF, 31, 32),
    ],
)
async def test_sleep_refuses_targets_outside_each_range_or_an_unknown_range(
    hass: HomeAssistant, mat: FakeMat, model: int, left: int, right: int
) -> None:
    mat.first_status = status(model=model, left=left, right=right)
    await setup_mat(hass, mat)
    with pytest.raises(ServiceValidationError) as raised:
        await schedule_command(hass, "sleep_start")
    assert str(raised.value) == "수면 적정 온도로 변경 후 실행해 주세요."
    assert mat.controls() == []


@pytest.mark.parametrize("heat", [3, 4])
async def test_sleep_from_reservation_refuses_the_selected_sides_target_outside_range(
    hass: HomeAssistant, mat: FakeMat, heat: int
) -> None:
    mat.first_status = status(mode=2, heat=heat, left=34, right=34)
    await setup_mat(hass, mat)
    with pytest.raises(ServiceValidationError) as raised:
        await schedule_command(hass, "sleep_start")
    assert str(raised.value) == "수면 적정 온도로 변경 후 실행해 주세요."
    assert mat.controls() == []


@pytest.mark.parametrize(
    ("model", "high", "expected"),
    [
        (0xFE, 37, "b28003020002241e1c231e250008080101fefe59"),
        (0x02, 35, "b28003020002241e1c231e230008080101fefe57"),
        (0x12, 33, "b28003020002241e1c231e210008080101fefe55"),
    ],
)
async def test_sleep_accepts_the_boundaries_of_each_range(
    hass: HomeAssistant, mat: FakeMat, model: int, high: int, expected: str
) -> None:
    mat.first_status = status(model=model, left=30, right=high)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(status(mode=3, model=model, left=30, right=high))
    await schedule_command(hass, "sleep_start")
    assert mat.controls() == [bytes.fromhex(expected)]


@pytest.mark.parametrize("temperature", [25, 33, 50])
async def test_schedule_settings_survive_reload_including_temperature_endpoints(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], temperature: int
) -> None:
    mat.first_status = status(range=3)
    entry = await setup_mat(hass, mat)
    await set_on_reservation_temperature(hass, temperature)
    await choose(hass, unique(hass, "select", "left_sleep_hours"), "6시간")
    await choose(hass, unique(hass, "select", "right_sleep_hours"), "10시간")
    assert mat.controls() == []
    original = entry.runtime_data
    # Load before any live status, then receive a narrower range. Stored 25/50 stay intact.
    mat.first_status = OFF_STATUS
    assert await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data is not original
    assert float(state_of(hass, unique(hass, "number", "on_reservation_temperature")).state) == temperature
    assert state_of(hass, unique(hass, "select", "left_sleep_hours")).state == "6시간"
    assert state_of(hass, unique(hass, "select", "right_sleep_hours")).state == "10시간"
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    saved = hass_storage[key]["data"]
    assert saved["enabled"] is True
    assert saved["heating_status"] == status(range=3).hex()
    assert saved["on_reservation_temperature"] == temperature
    assert saved["left_sleep_hours"] == 6
    assert saved["right_sleep_hours"] == 10


@pytest.mark.parametrize("stored", [None, True, False, "30", [], {}, 24, 51, 30.5])
async def test_malformed_on_reservation_temperature_defaults(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], stored: Any
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key,
        "data": {"on_reservation_temperature": stored},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert float(state_of(hass, unique(hass, "number", "on_reservation_temperature")).state) == 30


@pytest.mark.parametrize("stored", [None, True, False, "8", [], {}, 4, 11, 8.5])
async def test_malformed_sleep_hours_default_on_both_sides(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], stored: Any
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key,
        "data": {"left_sleep_hours": stored, "right_sleep_hours": stored},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    for side in ("left", "right"):
        assert state_of(hass, unique(hass, "select", f"{side}_sleep_hours")).state == "8시간"


async def test_older_store_gives_schedule_defaults_and_preserves_existing_keys(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key,
        "data": {"enabled": True, "heating_status": HEATING.hex()},
    }
    mat.first_status = OFF_STATUS
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data.enabled is True
    assert entry.runtime_data.stored_status.raw == HEATING
    assert float(state_of(hass, unique(hass, "number", "on_reservation_temperature")).state) == 30
    for side in ("left", "right"):
        assert state_of(hass, unique(hass, "select", f"{side}_sleep_hours")).state == "8시간"


@pytest.mark.parametrize("temperature", [25, 50])
async def test_on_reservation_refuses_stored_temperature_outside_the_live_range(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], temperature: int
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1, "minor_version": 1, "key": key,
        "data": {"on_reservation_temperature": temperature},
    }
    mat.first_status = OFF_STATUS
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert entry.runtime_data.on_reservation_temperature == temperature
    with pytest.raises(ServiceValidationError) as raised:
        await schedule_command(hass, "on_set")
    assert str(raised.value) == connection.ERROR_RESERVATION_TEMPERATURE
    assert mat.controls() == []


async def test_on_reservation_resend_stops_if_the_live_range_excludes_its_temperature(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = status(mode=6, flags=0, range=3)
    await setup_mat(hass, mat)
    await set_on_reservation_temperature(hass, 25)
    mat.during_write = status(mode=6, flags=0, range=4)
    with pytest.raises(HomeAssistantError) as raised:
        await schedule_command(hass, "on_set")
    assert not isinstance(raised.value, ServiceValidationError)
    assert str(raised.value) == connection.ERROR_UNCONFIRMED
    assert mat.controls() == [
        bytes.fromhex("b280060000fe241e1cfe010500fefe19fefefef5")
    ]
