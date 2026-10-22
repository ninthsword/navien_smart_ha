"""Does every module actually import?

**Some things a syntax check never catches.** Still referencing a deleted import from a
class body, or renaming a constant and missing one use of it, only surface when the file is
really loaded. Those are the mistakes that kill the whole integration during setup.

The cheapest test here, and the one that catches something most often.
"""

from __future__ import annotations

import sys

from harness import SRC, Report


def _import_integration_module(name: str) -> object:
    """Import one known integration module through a literal import statement."""
    if name == "__init__":
        import navien_smarthome

        return navien_smarthome
    if name == "airone":
        import navien_smarthome.airone as airone

        return airone
    if name == "api":
        import navien_smarthome.api as api

        return api
    if name == "binary_sensor":
        import navien_smarthome.binary_sensor as binary_sensor

        return binary_sensor
    if name == "boiler":
        import navien_smarthome.boiler as boiler

        return boiler
    if name == "climate":
        import navien_smarthome.climate as climate

        return climate
    if name == "config_flow":
        import navien_smarthome.config_flow as config_flow

        return config_flow
    if name == "const":
        import navien_smarthome.const as const

        return const
    if name == "coordinator":
        import navien_smarthome.coordinator as coordinator

        return coordinator
    if name == "diagnostics":
        import navien_smarthome.diagnostics as diagnostics

        return diagnostics
    if name == "entity":
        import navien_smarthome.entity as entity

        return entity
    if name == "gas_statistics":
        import navien_smarthome.gas_statistics as gas_statistics

        return gas_statistics
    if name == "models":
        import navien_smarthome.models as models

        return models
    if name == "mqtt":
        import navien_smarthome.mqtt as mqtt

        return mqtt
    if name == "number":
        import navien_smarthome.number as number

        return number
    if name == "select":
        import navien_smarthome.select as select

        return select
    if name == "sensor":
        import navien_smarthome.sensor as sensor

        return sensor
    if name == "switch":
        import navien_smarthome.switch as switch

        return switch
    raise LookupError(f"unknown integration module: {name}")


r = Report()

r.section("integration modules")

modules = sorted(path.stem for path in SRC.glob("*.py"))
for name in modules:
    try:
        _import_integration_module(name)
        r.ok(True, name)
    except Exception as err:  # noqa: BLE001 — report whatever blows up
        r.ok(False, f"{name} — {type(err).__name__}: {err}")

try:
    _import_integration_module("__unknown_import_sentinel__")
except LookupError:
    r.ok(True, "unknown module names are rejected")
else:
    r.ok(False, "unknown module names are rejected")


r.section("public tools")

# The issue template tells users to run this file, so it always has to work.
cli = SRC.parent.parent / "tools" / "navien_cli.py"
r.ok(cli.exists(), "tools/navien_cli.py exists")
try:
    compile(cli.read_text("utf-8"), str(cli), "exec")
    r.ok(True, "syntax is valid")
except SyntaxError as err:
    r.ok(False, f"syntax error — {err}")


sys.exit(r.finish())
