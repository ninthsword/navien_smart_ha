"""The filter sensor reports life remaining, not usage (PR #18).

A contributor compared it against the app on a real device — sensor `87`, app "필터 87%
남음", usage 13%.

**Independently confirmed from the app code**, in the rule its filter-management screen uses
to turn a percentage into text:

    v >= 76        「필터가 충분하네요!」
    41 <= v < 76   「아직은 여유있네요!」
    11 <= v < 41   「곧 필터를 교체해야 해요!」
    v < 11         「필터를 교체해 주세요!」

**Higher means "plenty left" and lower means "replace it".** For a usage figure it would be
exactly the other way around. The real-device comparison and the app behaviour reach the
same conclusion independently.

Leaving the value uninverted is also right: it was the remaining life from the start, so
**the history already stored is correct.** Switching to `100 - x` would make the same number
mean the opposite on either side of that change.
"""

from __future__ import annotations

import ast
import sys

from harness import ROOT, Report, make_airone, source

r = Report()

SENSOR = source("sensor.py")
AIRONE = source("airone.py")
README = (ROOT / "README.md").read_text("utf-8")


r.section("the name says remaining")

r.ok("필터 잔량" in SENSOR, "one filter is named 필터 잔량")
r.ok('f"필터 {index + 1} 잔량"' in SENSOR, "several are named 필터 N 잔량")
r.ok("사용률" not in SENSOR, "no usage-rate wording left in sensor.py")
r.ok("사용률" not in AIRONE, "none left in airone.py either")
r.ok("필터 잔량" in README, "the README entity table agrees")


r.section("the value passes through uninverted")

device = make_airone(filters=[87, 42])
r.ok(device.filters[0]["percent"] == 87, "87 arrives as 87")
r.ok(device.filters[1]["percent"] == 42, "so does the second one")
r.ok("100 -" not in SENSOR and "100-" not in SENSOR, "nothing subtracts from 100 anywhere")
r.ok('"percent"' in AIRONE, "the diagnostics key name is unchanged, so the format holds")


r.section("a missing value stays empty rather than becoming 0")

# Some devices declare four filters on the outdoor unit and send values for only some of them
# (confirmed by a report). Filling a missing value with 0 reads as "replace it" and is false.
partial = make_airone(filters=[87, None, None, None])
r.ok(len(partial.filters) == 4, "as many slots as were declared")
r.ok(partial.filters[1]["percent"] is None, "a value that did not arrive is None")


r.section("the entity is unchanged, so existing history continues")

r.ok(
    'f"{device.device_id}_filter_{index}"' in SENSOR,
    "unique_id is untouched, so no new entity is created",
)
r.ok(
    "_attr_has_entity_name" in source("entity.py"),
    "only the display name changes; the entity_id holds",
)


r.section("the reasoning is recorded in the code")

cls = next(
    node
    for node in ast.walk(ast.parse(SENSOR))
    if isinstance(node, ast.ClassDef) and node.name == "AironeFilterSensor"
)
doc = ast.get_docstring(cls) or ""
r.ok("remaining" in doc, "it says remaining")
r.ok("usage.percent" in doc, "it records that the field name means the opposite")
r.ok("87" in doc, "it records the value compared on a real device")


sys.exit(r.finish())
