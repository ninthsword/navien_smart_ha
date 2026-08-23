"""Does every module actually import?

**Some things a syntax check never catches.** Still referencing a deleted import from a
class body, or renaming a constant and missing one use of it, only surface when the file is
really loaded. Those are the mistakes that kill the whole integration during setup.

The cheapest test here, and the one that catches something most often.
"""

from __future__ import annotations

import importlib
import sys

from harness import SRC, Report

r = Report()

r.section("integration modules")

modules = sorted(path.stem for path in SRC.glob("*.py"))
for name in modules:
    target = "navien_smarthome" if name == "__init__" else f"navien_smarthome.{name}"
    try:
        importlib.import_module(target)
        r.ok(True, name)
    except Exception as err:  # noqa: BLE001 — report whatever blows up
        r.ok(False, f"{name} — {type(err).__name__}: {err}")


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
