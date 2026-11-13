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


async def menu_choice(
    hass: HomeAssistant, result: dict[str, Any], choice: str
) -> dict[str, Any]:
    assert result.get("type") is FlowResultType.MENU
    assert choice in result.get("menu_options", [])
    return await configure(hass, result, {"next_step_id": choice})


async def add_section(
    hass: HomeAssistant, result: dict[str, Any], side: str, hours: int, temperature: int
) -> dict[str, Any]:
    result = await menu_choice(hass, result, f"custom_{side}_add")
    assert result.get("step_id") == f"custom_{side}_add"
    return await configure(hass, result, {"hours": str(hours), "temperature": str(temperature)})


def assert_menu(
    result: dict[str, Any], side: str, options: list[str], sections: str, total: int,
    warning: str = "",
) -> None:
    assert result.get("type") is FlowResultType.MENU
    assert result.get("step_id") == f"custom_{side}"
    assert result.get("menu_options") == options
    assert result.get("description_placeholders") == {
        "sections": sections, "total": str(total), "remaining": str(12 - total), "warning": warning,
    }


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
    ("stored", "left_summary", "left_hours", "right_summary", "right_hours"),
    [
        (None, "구간이 없어 사용하지 않습니다.", 0, "구간이 없어 사용하지 않습니다.", 0),
        ({"left": LEFT, "right": []}, "1구간 2시간 30도, 2구간 3시간 31도", 5, "구간이 없어 사용하지 않습니다.", 0),
        ({"left": [], "right": RIGHT}, "구간이 없어 사용하지 않습니다.", 0, "1구간 4시간 32도", 4),
    ],
)
async def test_custom_program_choice_loads_each_side_into_menus(
    hass: HomeAssistant, stored: Any, left_summary: str, left_hours: int,
    right_summary: str, right_hours: int,
) -> None:
    entry = make_entry(hass)
    if stored is not None:
        hass.config_entries.async_update_entry(entry, options={CONF_CUSTOM_PROGRAMS: {"2": stored}})
    result = await program_form(hass, entry, "2")
    left_options = ["custom_left_add"]
    if left_hours:
        left_options += ["custom_left_remove", "custom_left_clear"]
    assert_menu(result, "left", [*left_options, "custom_right"], left_summary, left_hours)
    result = await menu_choice(hass, result, "custom_right")
    right_options = ["custom_right_add"]
    if right_hours:
        right_options += ["custom_right_remove", "custom_right_clear"]
    if left_hours:
        right_options.append("custom_copy_left")
    right_options.append("custom_left")
    if left_hours or right_hours:
        right_options.append("custom_save")
    assert_menu(result, "right", right_options, right_summary, right_hours)
    result = await menu_choice(hass, result, "custom_left")
    assert_menu(result, "left", [*left_options, "custom_right"], left_summary, left_hours)


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    ("rows", "actions", "summary", "total"),
    [
        ([], ["add"], "구간이 없어 사용하지 않습니다.", 0),
        ([{"hours": 1, "temperature": 30}], ["add", "remove", "clear"], "1구간 1시간 30도", 1),
        (RIGHT_FIVE, ["remove", "clear"], "1구간 1시간 29도, 2구간 1시간 30도, 3구간 1시간 31도, 4구간 1시간 32도, 5구간 1시간 33도", 5),
        ([{"hours": 12, "temperature": 30}], ["remove", "clear"], "1구간 12시간 30도", 12),
    ],
)
async def test_custom_menu_options_at_empty_partial_section_and_hour_limits(
    hass: HomeAssistant, side: str, rows: Any, actions: list[str], summary: str, total: int
) -> None:
    entry = make_entry(hass)
    program = {"left": rows if side == "left" else LEFT, "right": rows if side == "right" else []}
    hass.config_entries.async_update_entry(entry, options={CONF_CUSTOM_PROGRAMS: {"1": program}})
    result = await program_form(hass, entry)
    options = [f"custom_{side}_{action}" for action in actions]
    if side == "left":
        options += ["custom_right"]
    else:
        result = await menu_choice(hass, result, "custom_right")
        options += ["custom_copy_left", "custom_left", "custom_save"]
    assert_menu(result, side, options, summary, total)
    assert entry.options[CONF_CUSTOM_PROGRAMS] == {"1": program}


@pytest.mark.parametrize("side", ["left", "right"])
async def test_custom_hours_shrink_and_dropdown_defaults_follow_the_previous_section(
    hass: HomeAssistant, side: str
) -> None:
    entry = make_entry(hass)
    result = await program_form(hass, entry)
    if side == "right":
        result = await menu_choice(hass, result, "custom_right")
    for section_number, remaining, hours, temperature, default in ((1, 12, 1, 31, 30), (2, 11, 11, 32, 31)):
        result = await menu_choice(hass, result, f"custom_{side}_add")
        assert result.get("description_placeholders") == {"section": str(section_number), "remaining": str(remaining)}
        fields = {field["name"]: field for field in serialize_form(result)}
        assert set(fields) == {"hours", "temperature"}
        assert fields["hours"]["default"] == "1"
        assert fields["temperature"]["default"] == str(default)
        hours_selector = fields["hours"]["selector"]["select"]
        assert hours_selector["options"] == [
            {"value": str(hour), "label": f"{hour}시간"} for hour in range(1, remaining + 1)
        ]
        assert hours_selector["mode"] == "dropdown"
        assert not hours_selector.get("multiple", False)
        assert not hours_selector.get("custom_value", False)
        assert fields["temperature"]["selector"]["select"]["mode"] == "dropdown"
        result = await configure(hass, result, {"hours": str(hours), "temperature": str(temperature)})
        assert result.get("step_id") == f"custom_{side}"
        assert entry.options == {}
    options = [f"custom_{side}_remove", f"custom_{side}_clear"]
    options += ["custom_right"] if side == "left" else ["custom_left", "custom_save"]
    assert_menu(result, side, options, "1구간 1시간 31도, 2구간 11시간 32도", 12)


@pytest.mark.parametrize(
    ("live", "stored", "expected"),
    [
        (status(range=0), HEATING, (25, 43)),
        (status(mode=2, range=2), HEATING, (25, 48)),
        (status(mode=3, range=3), HEATING, (25, 50)),
        (status(mode=6, flags=0, range=4), status(range=3), (28, 45)),
        (custom_status(0, range=4), status(range=3), (28, 45)),
        (status(mode=4, range=3), HEATING, (28, 45)),
        (status(mode=5, range=0), status(range=2), (25, 48)),
        (status(mode=7, range=0), None, (25, 50)),
        (status(mode=8, range=0), None, (25, 50)),
        (None, HEATING, (28, 45)),
        (None, None, (25, 50)),
    ],
)
async def test_custom_temperature_dropdown_uses_live_capabilities_then_stored_then_fallback(
    hass: HomeAssistant, mat: FakeMat, hass_storage: dict[str, Any],
    live: bytes | None, stored: bytes | None, expected: tuple[int, int],
) -> None:
    entry = make_entry(hass)
    if stored is not None:
        key = f"{DOMAIN}.ble_mat.{entry.entry_id}"
        hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"heating_status": stored.hex()}}
    mat.first_status = live
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: entry.runtime_data.authenticated)
    if live is not None:
        await until(lambda: entry.runtime_data.status is not None)
    result = await program_form(hass, entry)
    result = await menu_choice(hass, result, "custom_left_add")
    fields = {field["name"]: field for field in serialize_form(result)}
    low, high = expected
    assert fields["temperature"]["selector"]["select"]["options"] == [
        {"value": str(degree), "label": f"{degree}도"} for degree in range(low, high + 1)
    ]
    assert fields["temperature"]["default"] == "30"
    assert fields["hours"]["default"] == "1"
    assert entry.options == {}
    assert mat.controls() == []


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("temperature", [25, 34, 50])
async def test_custom_stored_temperature_is_marked_preserved_and_defaults_within_the_range(
    hass: HomeAssistant, mat: FakeMat, side: str, temperature: int
) -> None:
    rows = [{"hours": 1, "temperature": temperature}]
    program = {"left": rows if side == "left" else [], "right": rows if side == "right" else []}
    entry = await setup_custom(hass, mat, program)
    result = await program_form(hass, entry)
    if side == "right":
        result = await menu_choice(hass, result, "custom_right")
    outside = temperature in (25, 50)
    placeholders = result.get("description_placeholders", {})
    assert placeholders.get("sections") == f"1구간 1시간 {temperature}도" + (" (범위 밖)" if outside else "")
    assert placeholders.get("warning") == ("온도 범위 밖의 구간이 있어 이 프로그램을 시작할 수 없습니다." if outside else "")
    result = await menu_choice(hass, result, f"custom_{side}_add")
    fields = {field["name"]: field for field in serialize_form(result)}
    assert fields["temperature"]["default"] == str(30 if outside else temperature)
    result = await configure(hass, result, {"hours": "1", "temperature": "30"})
    if side == "left":
        result = await menu_choice(hass, result, "custom_right")
    assert "custom_save" in result.get("menu_options", [])
    result = await menu_choice(hass, result, "custom_save")
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CUSTOM_PROGRAMS]["1"][side] == [*rows, {"hours": 1, "temperature": 30}]
    assert mat.controls() == []


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    ("fields", "refused"),
    [
        ({"hours": "0"}, "hours"),
        ({"hours": "12"}, "hours"),
        ({"hours": "1.5"}, "hours"),
        ({"temperature": "27"}, "temperature"),
        ({"temperature": "46"}, "temperature"),
        ({"temperature": "30.5"}, "temperature"),
    ],
)
async def test_custom_add_outside_offered_options_is_refused_without_leaving_the_form(
    hass: HomeAssistant, mat: FakeMat, side: str, fields: dict[str, Any], refused: str
) -> None:
    entry = await setup_mat(hass, mat)
    result = await program_form(hass, entry)
    if side == "right":
        result = await menu_choice(hass, result, "custom_right")
    result = await add_section(hass, result, side, 1, 30)
    result = await menu_choice(hass, result, f"custom_{side}_add")
    with pytest.raises(InvalidData) as refusal:
        await configure(hass, result, {"hours": "1", "temperature": "30", **fields})
    assert set(refusal.value.schema_errors) == {refused}
    assert hass.config_entries.options.async_get(result["flow_id"]).get("step_id") == f"custom_{side}_add"
    assert entry.options == {}
    result = await configure(hass, result, {"hours": "1", "temperature": "30"})
    assert result.get("description_placeholders", {}).get("total") == "2"


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("action", ["remove", "clear"])
async def test_custom_delete_last_or_clear_side_keeps_the_other_side(
    hass: HomeAssistant, side: str, action: str
) -> None:
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_CUSTOM_PROGRAMS: {"1": {"left": LEFT, "right": LEFT}}})
    result = await program_form(hass, entry)
    if side == "right":
        result = await menu_choice(hass, result, "custom_right")
    result = await menu_choice(hass, result, f"custom_{side}_{action}")
    expected = LEFT[:1] if action == "remove" else []
    assert result.get("description_placeholders", {}).get("sections") == ("1구간 2시간 30도" if expected else "구간이 없어 사용하지 않습니다.")
    if not expected:
        assert f"custom_{side}_remove" not in result.get("menu_options", [])
        assert f"custom_{side}_clear" not in result.get("menu_options", [])
    if side == "left":
        result = await menu_choice(hass, result, "custom_right")
    result = await menu_choice(hass, result, "custom_save")
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CUSTOM_PROGRAMS] == {"1": {
        "left": expected if side == "left" else LEFT,
        "right": expected if side == "right" else LEFT,
    }}


async def test_custom_copy_replaces_right_and_going_back_preserves_independent_sides(
    hass: HomeAssistant,
) -> None:
    entry = make_entry(hass)
    result = await program_form(hass, entry)
    result = await add_section(hass, result, "left", 2, 31)
    result = await menu_choice(hass, result, "custom_right")
    assert result.get("menu_options") == ["custom_right_add", "custom_copy_left", "custom_left", "custom_save"]
    result = await add_section(hass, result, "right", 4, 40)
    result = await menu_choice(hass, result, "custom_left")
    assert result.get("description_placeholders", {}).get("sections") == "1구간 2시간 31도"
    result = await menu_choice(hass, result, "custom_right")
    assert result.get("description_placeholders", {}).get("sections") == "1구간 4시간 40도"
    result = await menu_choice(hass, result, "custom_copy_left")
    assert result.get("description_placeholders", {}).get("sections") == "1구간 2시간 31도"
    assert entry.options == {}
    result = await menu_choice(hass, result, "custom_left")
    result = await menu_choice(hass, result, "custom_left_clear")
    result = await menu_choice(hass, result, "custom_right")
    assert_menu(result, "right", ["custom_right_add", "custom_right_remove", "custom_right_clear", "custom_left", "custom_save"], "1구간 2시간 31도", 2)
    result = await menu_choice(hass, result, "custom_save")
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CUSTOM_PROGRAMS] == {"1": {"left": [], "right": [{"hours": 2, "temperature": 31}]}}


async def test_custom_unsaved_edits_and_abandoning_a_flow_do_not_change_stored_programs(
    hass: HomeAssistant,
) -> None:
    entry = make_entry(hass)
    original = {CONF_MAT_MODEL: "EQM560", CONF_CUSTOM_PROGRAMS: {"1": {"left": LEFT, "right": RIGHT}}}
    hass.config_entries.async_update_entry(entry, options=original)
    result = await program_form(hass, entry)
    result = await menu_choice(hass, result, "custom_left_clear")
    result = await add_section(hass, result, "left", 1, 35)
    result = await menu_choice(hass, result, "custom_right")
    result = await menu_choice(hass, result, "custom_copy_left")
    assert entry.options == original
    hass.config_entries.options.async_abort(result["flow_id"])
    result = await program_form(hass, entry)
    assert result.get("description_placeholders", {}).get("sections") == "1구간 2시간 30도, 2구간 3시간 31도"
    result = await menu_choice(hass, result, "custom_right")
    assert result.get("description_placeholders", {}).get("sections") == "1구간 4시간 32도"
    assert entry.options == original


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
    result = await add_section(hass, result, "left", 1, 28)
    result = await menu_choice(hass, result, "custom_right")
    result = await add_section(hass, result, "right", 12, 45)
    assert entry.options == {CONF_MAT_MODEL: "EQM560", CONF_CUSTOM_PROGRAMS: programs}
    with patch.object(hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload) as reload:
        result = await menu_choice(hass, result, "custom_save")
        await hass.async_block_till_done()
        await until(lambda: entry.runtime_data.status is not None)
    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_MAT_MODEL: "EQM560",
        CONF_CUSTOM_PROGRAMS: {**programs, "2": {
            "left": [{"hours": 1, "temperature": 28}],
            "right": [{"hours": 12, "temperature": 45}],
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
    assert_menu(result, "left", ["custom_left_add", "custom_right"], "구간이 없어 사용하지 않습니다.", 0)
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


@pytest.mark.parametrize("moved", [HEATING, status(mode=2), status(mode=3), OFF_STATUS, status(mode=9, flags=0)])
async def test_custom_second_half_resend_requires_powered_mode_9(
    hass: HomeAssistant, mat: FakeMat, moved: bytes
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})
    answers = iter([custom_status(1), moved])
    mat.respond = lambda _frame: next(answers)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, True)
    assert mat.controls() == [BOTH_LEFT_FRAME, BOTH_RIGHT_FRAME]


@pytest.mark.parametrize("mode", [1, 2, 3])
@pytest.mark.parametrize(
    ("program", "frame"),
    [({"left": LEFT, "right": []}, LEFT_FRAME),
     ({"left": [], "right": RIGHT}, RIGHT_FRAME),
     ({"left": LEFT, "right": RIGHT}, BOTH_LEFT_FRAME)],
)
async def test_custom_first_frame_without_a_reply_resends_from_each_start_mode(
    hass: HomeAssistant, mat: FakeMat, mode: int, program: Any, frame: bytes
) -> None:
    first = status(mode=mode, flags=0x0E, volume=0x23, left=31, right=32)
    await setup_custom(hass, mat, program, first)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED) as raised:
        await custom_switch(hass, True)
    assert not isinstance(raised.value, ServiceValidationError)
    assert mat.controls() == [frame] * 2


@pytest.mark.parametrize("mode", [1, 2, 3])
async def test_custom_first_frame_resends_when_status_still_allows_start(
    hass: HomeAssistant, mat: FakeMat, mode: int
) -> None:
    await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})
    moved = status(mode=mode, flags=0x0E, volume=0x23, left=31, right=32)
    answers = iter([moved, custom_status(1), custom_status(3)])
    mat.respond = lambda _frame: next(answers)
    await custom_switch(hass, True)
    assert mat.controls() == [BOTH_LEFT_FRAME] * 2 + [BOTH_RIGHT_FRAME]


@pytest.mark.parametrize(
    ("program", "frame"),
    [({"left": LEFT, "right": []}, LEFT_FRAME),
     ({"left": [], "right": RIGHT}, RIGHT_FRAME),
     ({"left": LEFT, "right": RIGHT}, BOTH_LEFT_FRAME)],
)
async def test_custom_first_frame_stops_resending_if_powered_off(
    hass: HomeAssistant, mat: FakeMat, program: Any, frame: bytes
) -> None:
    await setup_custom(hass, mat, program)
    mat.respond = confirm_with(OFF_STATUS)
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, True)
    assert mat.controls() == [frame]


@pytest.mark.parametrize("mode", [1, 2, 3, 9])
@pytest.mark.parametrize(
    ("program", "heat", "frame"),
    [({"left": LEFT, "right": []}, 4, LEFT_FRAME),
     ({"left": [], "right": RIGHT}, 3, RIGHT_FRAME),
     ({"left": LEFT, "right": RIGHT}, 3, BOTH_LEFT_FRAME),
     ({"left": LEFT, "right": RIGHT}, 4, BOTH_LEFT_FRAME)],
)
async def test_custom_first_frame_stops_if_a_program_side_becomes_unselected(
    hass: HomeAssistant, mat: FakeMat, mode: int, program: Any, heat: int, frame: bytes
) -> None:
    await setup_custom(hass, mat, program)
    mat.respond = confirm_with(status(mode=mode, heat=heat, byte14=0))
    with pytest.raises(HomeAssistantError, match=connection.ERROR_UNCONFIRMED):
        await custom_switch(hass, True)
    assert mat.controls() == [frame]


@pytest.mark.parametrize("ending", ["custom", "off", "other", "lost"])
async def test_partial_start_follows_live_state_and_recovers_by_off_then_on(
    hass: HomeAssistant, mat: FakeMat, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    now = [100.0]
    monkeypatch.setattr(connection, "monotonic", lambda: now[0])
    entry = await setup_custom(hass, mat, {"left": LEFT, "right": RIGHT})

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
    expected = {"custom": "on", "off": "off", "other": "off", "lost": "on"}[ending]
    assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == expected
    assert mat.controls() == [BOTH_LEFT_FRAME] + [BOTH_RIGHT_FRAME] * (2 if ending == "custom" else 1)
    if ending == "lost":
        conn = entry.runtime_data
        assert conn.status is None
        assert conn._display_deadline == 103.0
        await until(lambda: mat.connect_calls == 2 and conn.authenticated)
        sent = list(mat.controls())
        with pytest.raises(HomeAssistantError, match=connection.ERROR_NO_LINK):
            await custom_switch(hass, False)
        with pytest.raises(HomeAssistantError, match=connection.ERROR_NO_LINK):
            await custom_switch(hass, True)
        assert mat.controls() == sent
        now[0] = 103.0
        conn._publish()
        assert state_of(hass, unique(hass, "switch", "custom_sleep")).state == "unavailable"
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
    assert mat.controls() == [LEFT_FRAME] * 2
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
