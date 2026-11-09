"""Home Assistant's custom sleep programs, independent of the phone's programs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

CONF_CUSTOM_PROGRAMS = "custom_programs"
PROGRAM_OPTIONS = [f"프로그램 {number}" for number in range(1, 4)]


class Section(TypedDict):
    hours: int
    temperature: int


class CustomProgram(TypedDict):
    left: list[Section]
    right: list[Section]


def read_program(options: Mapping[str, Any], number: str) -> CustomProgram | None:
    """Return a validated copy; a malformed stored program counts as absent."""
    programs = options.get(CONF_CUSTOM_PROGRAMS)
    if not isinstance(programs, dict) or number not in ("1", "2", "3"):
        return None
    value = programs.get(number)
    if not isinstance(value, dict):
        return None
    sides: list[list[Section]] = []
    for side in ("left", "right"):
        rows = value.get(side)
        if not isinstance(rows, list) or len(rows) > 5:
            return None
        sections: list[Section] = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            hours, temperature = row.get("hours"), row.get("temperature")
            if (
                type(hours) is not int
                or not 1 <= hours <= 12
                or type(temperature) is not int
                or not 25 <= temperature <= 50
            ):
                return None
            sections.append({"hours": hours, "temperature": temperature})
        if sum(section["hours"] for section in sections) > 12:
            return None
        sides.append(sections)
    if not any(sides):
        return None
    return {"left": sides[0], "right": sides[1]}
