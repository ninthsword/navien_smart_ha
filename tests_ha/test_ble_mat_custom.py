"""Custom sleep through HA's options flow and services; all control vectors are literals."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.navien_smarthome.ble_mat import CONF_MAT_MODEL, connection
from custom_components.navien_smarthome.ble_mat.programs import CONF_CUSTOM_PROGRAMS
from custom_components.navien_smarthome.const import DOMAIN

from .test_ble_mat import (
    HEATING,
    OFF_STATUS,
    FakeMat,
    choose,
    confirm_with,
    make_entry,
    mat_options_form,
    serialize_form,
    setup_mat,
    state_of,
    status,
    turn,
    unique,
    until,
)
from .test_ble_mat import mat as mat

LEFT = [{"hours": 2, "temperature": 30}, {"hours": 3, "temperature": 31}]
RIGHT = [{"hours": 4, "temperature": 32}]
LEFT_FIVE = [
    {"hours": hours, "temperature": temperature}
    for hours, temperature in ((1, 28), (2, 30), (3, 32), (2, 34), (4, 36))
]
RIGHT_FIVE = [{"hours": 1, "temperature": t} for t in (29, 30, 31, 32, 33)]
READY = status(flags=0x0E, volume=0x23, left=31, right=32)


def custom_status(state: int, **fields: int) -> bytes:
    return status(mode=9, flags=0x0E, volume=0x23, left=31, right=32, byte14=state, **fields)


LEFT_FRAME = bytes.fromhex("b28009062003010202030000001e1f000000fef5")
RIGHT_FRAME = bytes.fromhex("b28009062004020104000000002000000000fed8")
BOTH_LEFT_FRAME = bytes.fromhex("b28009062002010202030000001e1f000000fef4")
BOTH_RIGHT_FRAME = bytes.fromhex("b28009062002020104000000002000000000fed6")
STOP_FRAME = bytes.fromhex("b28001062002241e1c231f20fefefefefefefe5b")


async def setup_custom(
    hass: HomeAssistant, mat: FakeMat, program: Any, first: bytes = READY
) -> MockConfigEntry:
    mat.first_status = first
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_CUSTOM_PROGRAMS: {"1": program}})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    return entry


async def custom_switch(hass: HomeAssistant, on: bool) -> None:
    await turn(hass, "turn_on" if on else "turn_off", unique(hass, "switch", "custom_sleep"))


async def configure(hass: HomeAssistant, result: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return dict(await hass.config_entries.options.async_configure(result["flow_id"], data))


def side_input(enabled: bool = True, **fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"enabled": enabled}
    for i in range(1, 6):
        result[f"hours_{i}"] = 0
        result[f"temperature_{i}"] = 30
    return {**result, **fields}


async def program_form(
    hass: HomeAssistant, entry: MockConfigEntry, number: str = "1"
) -> dict[str, Any]:
    result = await mat_options_form(hass, entry, "custom_program")
    assert result.get("step_id") == "custom_program"
    fields = serialize_form(result)
    assert {field["name"] for field in fields} == {"program", "reset"}
    assert fields[0]["selector"]["select"]["options"] == ["1", "2", "3"]
    return await configure(hass, result, {"program": number, "reset": False})


@pytest.mark.parametrize(
    ("stored", "expected_left", "expected_right"),
    [
        (None, side_input(), side_input()),
        ({"left": LEFT, "right": []}, side_input(hours_1=2, hours_2=3, temperature_2=31), side_input(False)),
        ({"left": [], "right": RIGHT}, side_input(False), side_input(hours_1=4, temperature_1=32)),
    ],
)
async def test_custom_form_prefills_each_side_and_unused_rows(
    hass: HomeAssistant, stored: Any, expected_left: dict[str, Any], expected_right: dict[str, Any]
) -> None:
    entry = make_entry(hass)
    if stored is not None:
        hass.config_entries.async_update_entry(entry, options={CONF_CUSTOM_PROGRAMS: {"2": stored}})
    result = await program_form(hass, entry, "2")
    assert result.get("step_id") == "custom_left"
    defaults = {field["name"]: field.get("default") for field in serialize_form(result)}
    assert defaults == expected_left
    result = await configure(hass, result, side_input(hours_1=2))
    assert result.get("step_id") == "custom_right"
    defaults = {field["name"]: field.get("default") for field in serialize_form(result)}
    assert defaults == {**expected_right, "copy_left": False}


async def test_custom_save_preserves_other_programs_and_name_and_reloads(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = make_entry(hass)
    programs = {"1": {"left": LEFT, "right": RIGHT}, "3": {"left": [], "right": RIGHT}}
    hass.config_entries.async_update_entry(
        entry, options={CONF_MAT_MODEL: "EQM560", CONF_CUSTOM_PROGRAMS: programs}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    previous = entry.runtime_data
    result = await program_form(hass, entry, "2")
    result = await configure(hass, result, side_input(hours_1=1, temperature_1=25))
    with patch.object(hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload) as reload:
        result = await configure(hass, result, side_input(hours_1=12, temperature_1=50))
        await hass.async_block_till_done()
        await until(lambda: entry.runtime_data.status is not None)
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_MAT_MODEL: "EQM560",
        CONF_CUSTOM_PROGRAMS: {**programs, "2": {
            "left": [{"hours": 1, "temperature": 25}],
            "right": [{"hours": 12, "temperature": 50}],
        }},
    }
    reload.assert_called_once_with(entry.entry_id)
    assert entry.runtime_data is not previous
    assert mat.disconnected
    assert mat.controls() == []


@pytest.mark.parametrize("number", ["1", "2", "3"])
async def test_custom_reset_removes_only_the_chosen_program(
    hass: HomeAssistant, number: str
) -> None:
    entry = make_entry(hass)
    programs = {str(i): {"left": LEFT, "right": RIGHT} for i in range(1, 4)}
    hass.config_entries.async_update_entry(
        entry, options={CONF_MAT_MODEL: "EQM560", CONF_CUSTOM_PROGRAMS: programs}
    )
    result = await mat_options_form(hass, entry, "custom_program")
    result = await configure(hass, result, {"program": number, "reset": True})
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_MAT_MODEL: "EQM560",
        CONF_CUSTOM_PROGRAMS: {key: value for key, value in programs.items() if key != number},
    }


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({}, "custom_hours"),
        ({"hours_1": 12, "hours_2": 1}, "custom_hours"),
        ({"hours_2": 1}, "custom_contiguous"),
        ({"hours_1": 1, "hours_3": 1}, "custom_contiguous"),
        ({"hours_1": 1.5}, "custom_rows"),
        ({"hours_1": 1, "temperature_1": 30.5}, "custom_rows"),
    ],
)
async def test_custom_validation_keeps_the_side_form_open(
    hass: HomeAssistant, side: str, fields: dict[str, Any], error: str
) -> None:
    entry = make_entry(hass)
    result = await program_form(hass, entry)
    if side == "right":
        result = await configure(hass, result, side_input(hours_1=1))
    result = await configure(hass, result, side_input(**fields))
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == f"custom_{side}"
    assert result.get("errors") == {"base": error}
    assert entry.options == {}


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    ("fields", "refused"),
    [
        ({"hours_1": -1}, "hours_1"),
        ({"hours_1": 13}, "hours_1"),
        ({"hours_1": 1, "temperature_1": 24}, "temperature_1"),
        ({"hours_1": 1, "temperature_1": 51}, "temperature_1"),
    ],
)
async def test_custom_row_outside_its_range_is_refused_by_the_form_schema(
    hass: HomeAssistant, side: str, fields: dict[str, Any], refused: str
) -> None:
    entry = make_entry(hass)
    result = await program_form(hass, entry)
    if side == "right":
        result = await configure(hass, result, side_input(hours_1=1))
    with pytest.raises(InvalidData) as refusal:
        await configure(hass, result, side_input(**fields))
    assert set(refusal.value.schema_errors) == {refused}
    assert hass.config_entries.options.async_get(result["flow_id"]).get("step_id") == f"custom_{side}"
    assert entry.options == {}


@pytest.mark.parametrize(
    ("left", "right", "expected", "error"),
    [
        (True, True, {"left": [{"hours": 2, "temperature": 31}], "right": [{"hours": 2, "temperature": 31}]}, None),
        (True, False, {"left": [{"hours": 2, "temperature": 31}], "right": []}, None),
        (False, True, None, "custom_copy_left"),
        (False, False, None, "custom_side_required"),
    ],
)
async def test_custom_copy_only_applies_to_an_enabled_right_side(
    hass: HomeAssistant, left: bool, right: bool, expected: Any, error: str | None
) -> None:
    entry = make_entry(hass)
    result = await program_form(hass, entry)
    result = await configure(hass, result, side_input(left, hours_1=2, temperature_1=31))
    result = await configure(hass, result, side_input(right, copy_left=True, hours_1=4, temperature_1=40))
    if error:
        assert result.get("errors") == {"base": error}
        assert result.get("step_id") == "custom_right"
        assert entry.options == {}
    else:
        assert result.get("type") is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_CUSTOM_PROGRAMS] == {"1": expected}


async def test_custom_disabled_left_stores_no_rows(hass: HomeAssistant) -> None:
    entry = make_entry(hass)
    result = await program_form(hass, entry)
    result = await configure(hass, result, side_input(False, hours_1=2))
    result = await configure(hass, result, side_input(hours_1=3))
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CUSTOM_PROGRAMS] == {
        "1": {"left": [], "right": [{"hours": 3, "temperature": 30}]}
    }


@pytest.mark.parametrize("first", [custom_status(1), custom_status(2), custom_status(3)])
async def test_custom_edit_aborts_while_running(
    hass: HomeAssistant, mat: FakeMat, first: bytes
) -> None:
    mat.first_status = first
    entry = await setup_mat(hass, mat)
    result = await mat_options_form(hass, entry, "custom_program")
    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "custom_sleep_running"


@pytest.mark.parametrize("loaded", [False, True])
async def test_custom_edit_allowed_without_connection_or_live_status(
    hass: HomeAssistant, mat: FakeMat, loaded: bool
) -> None:
    entry = make_entry(hass)
    if loaded:
        mat.first_status = None
        assert await hass.config_entries.async_setup(entry.entry_id)
        await until(lambda: entry.runtime_data.authenticated)
        assert entry.runtime_data.status is None
    result = await program_form(hass, entry)
    assert result.get("step_id") == "custom_left"


MALFORMED_PROGRAMS = [
    None, [], {}, {"left": [], "right": []}, {"left": LEFT},
    {"left": "invalid", "right": []},
    *[{"left": [{"hours": h, "temperature": t}], "right": []}
      for h, t in ((0, 30), (13, 30), (True, 30), (1.5, 30), (1, 24), (1, 51), (1, 30.5))],
    {"left": [{"hours": 7, "temperature": 30}] * 2, "right": []},
    {"left": [{"hours": 1, "temperature": 30}] * 6, "right": []},
]


@pytest.mark.parametrize("program", MALFORMED_PROGRAMS)
async def test_malformed_program_prefills_as_absent_and_cannot_start(
    hass: HomeAssistant, mat: FakeMat, program: Any
) -> None:
    entry = await setup_custom(hass, mat, program)
    result = await program_form(hass, entry)
    assert {field["name"]: field.get("default") for field in serialize_form(result)} == side_input()
    with pytest.raises(ServiceValidationError, match=connection.ERROR_CUSTOM_PROGRAM):
        await custom_switch(hass, True)
    assert mat.controls() == []


@pytest.mark.parametrize(
    ("program", "frames", "states"),
    [
        ({"left": LEFT, "right": []}, [LEFT_FRAME], [1]),
        ({"left": [], "right": RIGHT}, [RIGHT_FRAME], [2]),
        ({"left": LEFT, "right": RIGHT}, [BOTH_LEFT_FRAME, BOTH_RIGHT_FRAME], [1, 3]),
        ({"left": LEFT_FIVE, "right": []}, [bytes.fromhex("b28009062003010501020302041c1e202224fe62")], [1]),
        ({"left": [], "right": RIGHT_FIVE}, [bytes.fromhex("b28009062004020501010101011d1e1f2021fe58")], [2]),
        ({"left": LEFT_FIVE, "right": RIGHT_FIVE}, [
            bytes.fromhex("b28009062002010501020302041c1e202224fe61"),
            bytes.fromhex("b28009062002020501010101011d1e1f2021fe56"),
        ], [1, 3]),
    ],
)
async def test_custom_literal_frames_for_one_and_two_sides_and_five_sections(
    hass: HomeAssistant, mat: FakeMat, program: Any, frames: list[bytes], states: list[int]
) -> None:
    await setup_custom(hass, mat, program)
    answers = iter(custom_status(state) for state in states)
    mat.respond = lambda _frame: next(answers)
    await custom_switch(hass, True)
    assert mat.controls() == frames
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == "on"


@pytest.mark.parametrize("mode", [1, 2, 3, 9])
@pytest.mark.parametrize(
    ("heat", "program", "frame", "state"),
    [(3, {"left": LEFT, "right": []}, LEFT_FRAME, 1),
     (4, {"left": [], "right": RIGHT}, RIGHT_FRAME, 2)],
)
async def test_custom_start_uses_byte_5_in_all_accepted_modes(
    hass: HomeAssistant, mat: FakeMat, mode: int, heat: int, program: Any, frame: bytes, state: int
) -> None:
    await setup_custom(hass, mat, program, status(mode=mode, flags=0x0E, volume=0x23, heat=heat, byte14=0))
    mat.respond = confirm_with(custom_status(state, heat=heat))
    await custom_switch(hass, True)
    assert mat.controls() == [frame]


@pytest.mark.parametrize("mode", [1, 2, 3, 9])
@pytest.mark.parametrize("heat", [3, 4])
async def test_custom_refuses_a_program_side_that_is_off_in_byte_5(
    hass: HomeAssistant, mat: FakeMat, mode: int, heat: int
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT}, status(mode=mode, heat=heat, byte14=0))
    with pytest.raises(ServiceValidationError) as raised:
        await custom_switch(hass, True)
    assert str(raised.value) == "작동할 편측의 난방이 꺼져 있습니다. 난방을 켠 후 재시도 해주세요."
    assert mat.controls() == []


@pytest.mark.parametrize("first", [
    OFF_STATUS,
    *[status(mode=mode, flags=0, byte14=0) for mode in (1, 2, 3, 9)],
    *[status(mode=mode, heat=heat, byte14=0) for mode in (1, 2, 3, 9) for heat in (0, 9)],
])
async def test_custom_start_refuses_unaccepted_states(
    hass: HomeAssistant, mat: FakeMat, first: bytes
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT}, first)
    with pytest.raises(ServiceValidationError, match=connection.ERROR_CUSTOM_STATE):
        await custom_switch(hass, True)
    assert mat.controls() == []


@pytest.mark.parametrize("mode", [4, 5, 7, 8, 99])
async def test_custom_modes_without_capabilities_are_unavailable_and_refused(
    hass: HomeAssistant, mat: FakeMat, mode: int
) -> None:
    entry = await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT}, status(mode=mode))
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == "unavailable"
    # HA skips an unavailable entity; the connection also refuses a direct request.
    await custom_switch(hass, True)
    with pytest.raises(ServiceValidationError, match=connection.ERROR_CUSTOM_STATE):
        await entry.runtime_data.async_set_custom_sleep(True)
    assert mat.controls() == []


@pytest.mark.parametrize("temperature", [25, 50])
async def test_custom_refuses_sections_outside_the_live_heat_range(
    hass: HomeAssistant, mat: FakeMat, temperature: int
) -> None:
    await setup_custom(hass, mat, {"left": [{"hours": 1, "temperature": temperature}], "right": []})
    with pytest.raises(ServiceValidationError, match=connection.ERROR_CUSTOM_TEMPERATURE):
        await custom_switch(hass, True)
    assert mat.controls() == []


@pytest.mark.parametrize("state", [1, 2, 3])
async def test_custom_start_running_is_a_no_op_even_with_an_absent_selected_program(
    hass: HomeAssistant, mat: FakeMat, state: int
) -> None:
    await setup_custom(hass, mat, None, custom_status(state, heat=0))
    await choose(hass, unique(hass, "select", "custom_program"), "프로그램 3")
    await custom_switch(hass, True)
    assert mat.controls() == []


@pytest.mark.parametrize(
    ("program", "states", "frames"),
    [
        ({"left": LEFT, "right": []}, [0, 1], [LEFT_FRAME] * 2),
        ({"left": LEFT, "right": []}, [2, 1], [LEFT_FRAME] * 2),
        ({"left": [], "right": RIGHT}, [0, 2], [RIGHT_FRAME] * 2),
        ({"left": [], "right": RIGHT}, [1, 2], [RIGHT_FRAME] * 2),
        ({"left": LEFT, "right": RIGHT}, [0, 1, 1, 3], [BOTH_LEFT_FRAME] * 2 + [BOTH_RIGHT_FRAME] * 2),
        ({"left": LEFT, "right": RIGHT}, [2, 1, 2, 3], [BOTH_LEFT_FRAME] * 2 + [BOTH_RIGHT_FRAME] * 2),
    ],
)
async def test_custom_each_half_resends_once_and_checks_its_own_acknowledgement(
    hass: HomeAssistant, mat: FakeMat, program: Any, states: list[int], frames: list[bytes]
) -> None:
    await setup_custom(hass, mat, program)
    answers = iter(custom_status(state) for state in states)
    mat.respond = lambda _frame: next(answers)
    await custom_switch(hass, True)
    assert mat.controls() == frames


@pytest.mark.parametrize(
    ("program", "states", "frames"),
    [
        ({"left": LEFT, "right": []}, [0, 0], [LEFT_FRAME] * 2),
        ({"left": [], "right": RIGHT}, [0, 0], [RIGHT_FRAME] * 2),
        ({"left": LEFT, "right": RIGHT}, [0, 0], [BOTH_LEFT_FRAME] * 2),
        ({"left": LEFT, "right": RIGHT}, [1, 1, 1], [BOTH_LEFT_FRAME] + [BOTH_RIGHT_FRAME] * 2),
    ],
)
async def test_custom_final_error_stops_after_one_resend_per_half(
    hass: HomeAssistant, mat: FakeMat, program: Any, states: list[int], frames: list[bytes]
) -> None:
    await setup_custom(hass, mat, program)
    answers = iter(custom_status(state) for state in states)
    mat.respond = lambda _frame: next(answers)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED) as raised:
        await custom_switch(hass, True)
    assert not isinstance(raised.value, ServiceValidationError)
    assert mat.controls() == frames
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == ("on" if states[-1] else "off")


@pytest.mark.parametrize("moved", [HEATING, status(mode=2), status(mode=3), OFF_STATUS, status(mode=9, flags=0)])
async def test_custom_second_half_stops_if_the_mat_moved_on_after_confirming_left(
    hass: HomeAssistant, mat: FakeMat, moved: bytes
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})

    def answer(_frame: bytes) -> None:
        hass.loop.call_soon(mat.feed, custom_status(1))
        hass.loop.call_soon(mat.feed, moved)

    mat.respond = answer
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, True)
    assert mat.controls() == [BOTH_LEFT_FRAME]
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == "off"


@pytest.mark.parametrize("moved", [HEATING, status(mode=2), status(mode=3), OFF_STATUS])
@pytest.mark.parametrize("half", [1, 2])
async def test_custom_resend_requires_powered_mode_9_for_either_half(
    hass: HomeAssistant, mat: FakeMat, moved: bytes, half: int
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})
    answers = iter([moved] if half == 1 else [custom_status(1), moved])
    mat.respond = lambda _frame: next(answers)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, True)
    assert mat.controls() == ([BOTH_LEFT_FRAME] if half == 1 else [BOTH_LEFT_FRAME, BOTH_RIGHT_FRAME])


@pytest.mark.parametrize("ending", ["custom", "off", "other", "lost"])
async def test_partial_start_follows_live_state_and_recovers_by_off_then_on(
    hass: HomeAssistant, mat: FakeMat, ending: str
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})

    def answer(frame: bytes) -> bytes | None:
        if frame[6] == 1:
            return custom_status(1)
        if ending == "lost":
            mat.first_status = None
            hass.loop.call_soon(mat.lose_link)
            return None
        return {"custom": custom_status(1), "off": OFF_STATUS, "other": HEATING}[ending]

    mat.respond = answer
    with pytest.raises(HomeAssistantError):
        await custom_switch(hass, True)
    await hass.async_block_till_done()
    expected = {"custom": "on", "off": "off", "other": "off", "lost": "unavailable"}[ending]
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == expected
    assert mat.controls() == [BOTH_LEFT_FRAME] + [BOTH_RIGHT_FRAME] * (2 if ending == "custom" else 1)
    if ending == "custom":
        sent = len(mat.controls())
        await custom_switch(hass, True)
        assert len(mat.controls()) == sent
        mat.respond = confirm_with(READY)
        await custom_switch(hass, False)
        assert mat.controls()[-1] == STOP_FRAME
        answers = iter([custom_status(1), custom_status(3)])
        mat.respond = lambda _frame: next(answers)
        await custom_switch(hass, True)
        assert mat.controls()[-2:] == [BOTH_LEFT_FRAME, BOTH_RIGHT_FRAME]


async def test_custom_stop_literal_frame_and_retry_confirmation(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    mat.first_status = custom_status(3)
    await setup_mat(hass, mat)
    answers = iter([custom_status(3), READY])
    mat.respond = lambda _frame: next(answers)
    await custom_switch(hass, False)
    assert mat.controls() == [STOP_FRAME] * 2
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == "off"


async def test_custom_stop_final_error(hass: HomeAssistant, mat: FakeMat) -> None:
    mat.first_status = custom_status(1)
    await setup_mat(hass, mat)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, False)
    assert mat.controls() == [STOP_FRAME] * 2


@pytest.mark.parametrize("moved", [OFF_STATUS, status(mode=2), status(mode=3), custom_status(0)])
async def test_custom_stop_resend_stops_if_custom_sleep_ended(
    hass: HomeAssistant, mat: FakeMat, moved: bytes
) -> None:
    mat.first_status = custom_status(3)
    await setup_mat(hass, mat)
    mat.respond = confirm_with(moved)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, False)
    assert mat.controls() == [STOP_FRAME]


@pytest.mark.parametrize("first", [
    HEATING, OFF_STATUS, custom_status(0), status(mode=9, flags=0),
    *[status(mode=mode) for mode in (2, 3, 4, 5, 7, 8, 99)],
])
async def test_custom_stop_no_ops_in_every_nonrunning_state(
    hass: HomeAssistant, mat: FakeMat, first: bytes
) -> None:
    mat.first_status = first
    await setup_mat(hass, mat)
    await custom_switch(hass, False)
    assert mat.controls() == []


async def test_custom_readback_counts_as_activity_without_replacing_live_status(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_mat(hass, mat)
    previous = entry.runtime_data.status
    entry.runtime_data._activity.clear()
    mat.feed(custom_status(3, range=0xFF))
    assert entry.runtime_data._activity.is_set()
    assert entry.runtime_data.status is previous
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == "off"


async def test_custom_readback_does_not_confirm_a_command(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    entry = await setup_custom(hass, mat, {"left": LEFT, "right": []})
    previous = entry.runtime_data.status
    mat.respond = confirm_with(custom_status(1, range=0xFF))
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, True)
    assert mat.controls() == [LEFT_FRAME]
    assert entry.runtime_data.status is previous


async def test_custom_program_selection_survives_reload_without_sending(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_mat(hass, mat)
    entity = unique(hass, "select", "custom_program")
    assert state_of(hass, entity).attributes["options"] == ["프로그램 1", "프로그램 2", "프로그램 3"]
    assert state_of(hass, entity).state == "프로그램 1"
    await choose(hass, entity, "프로그램 3")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert state_of(hass, entity).state == "프로그램 3"
    assert hass_storage[f"{DOMAIN}.ble_mat.{entry.entry_id}"]["data"]["custom_program"] == 3
    assert mat.controls() == []


@pytest.mark.parametrize("stored", [None, True, False, "2", [], {}, 0, 4, 1.5])
async def test_malformed_selected_program_defaults_to_one(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any], stored: Any
) -> None:
    entry = make_entry(hass)
    key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"custom_program": stored}}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    assert state_of(hass, unique(hass, "select", "custom_program")).state == "프로그램 1"


@pytest.mark.parametrize("model", [0xFE, 0x11])
async def test_custom_entities_unavailable_without_model_extras(
    hass: HomeAssistant, mat: FakeMat, model: int
) -> None:
    mat.first_status = status(model=model)
    await setup_mat(hass, mat)
    for domain, suffix in (("select", "custom_program"), ("switch", "custom_sleep")):
        assert state_of(hass, unique(hass, domain, suffix)).state == "unavailable"


async def test_custom_two_halves_hold_the_command_lock(hass: HomeAssistant, mat: FakeMat) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})
    first_sent = asyncio.Event()

    def answer(frame: bytes) -> bytes | None:
        if frame[2] == 9 and frame[6] == 1:
            first_sent.set()
            return None
        return custom_status(3) if frame[2] == 9 else OFF_STATUS

    mat.respond = answer
    start = asyncio.create_task(custom_switch(hass, True))
    await first_sent.wait()
    off = asyncio.create_task(turn(hass, "turn_off", unique(hass, "switch", "power")))
    mat.feed(custom_status(1))
    await asyncio.gather(start, off)
    assert mat.controls() == [
        BOTH_LEFT_FRAME, BOTH_RIGHT_FRAME,
        bytes.fromhex("b280060420fe241e1cfe0000000000fefefefefc"),
    ]


@pytest.mark.parametrize("number", ["1", "2", "3"])
async def test_custom_starts_the_selected_program(
    hass: HomeAssistant, mat: FakeMat, number: str
) -> None:
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, options={
        CONF_CUSTOM_PROGRAMS: {number: {"left": [], "right": RIGHT}},
    })
    mat.first_status = READY
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.status is not None)
    await hass.async_block_till_done()
    await choose(hass, unique(hass, "select", "custom_program"), f"프로그램 {number}")
    assert mat.controls() == []
    mat.respond = confirm_with(custom_status(2))
    await custom_switch(hass, True)
    assert mat.controls() == [RIGHT_FRAME]


async def test_custom_start_with_no_stored_programs_is_refused(
    hass: HomeAssistant, mat: FakeMat
) -> None:
    await setup_mat(hass, mat)
    with pytest.raises(ServiceValidationError, match=connection.ERROR_CUSTOM_PROGRAM):
        await custom_switch(hass, True)
    assert mat.controls() == []
