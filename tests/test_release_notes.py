"""tools/release_notes.py turns a pushed tag into release notes, or refuses it.

The release workflow creates a GitHub release only when this script succeeds, so a tag whose
version differs from the manifest, or whose CHANGELOG section is missing or empty, must exit 1
with one `release_notes: ` line and nothing on stdout.

Each case runs the script as a subprocess in a temporary directory holding its own
CHANGELOG.md and manifest.json, so the repository's real CHANGELOG is never read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from harness import ROOT, Report

SCRIPT = ROOT / "tools" / "release_notes.py"

CHANGELOG = """# Changelog

## Unreleased — next

- Not released yet.

## v1.2.0 — 2026-10-01

**Second release.**

- 한국어 항목 — with an em dash.

## v1.1.0 — 2026-09-01

- First release.

## v1.0.0 — 2026-08-01

## v0.9.0 — 2026-07-01

- Older.
"""

r = Report()


def run(tag: str, version: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        manifest = root / "custom_components" / "navien_smarthome" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), tag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=root,
            check=False,
        )


def refused(result: subprocess.CompletedProcess[str], needle: str, label: str) -> None:
    lines = result.stderr.splitlines()
    r.ok(result.returncode == 1, f"{label}: exit 1")
    r.ok(result.stdout == "", f"{label}: nothing on stdout")
    r.ok(
        len(lines) == 1 and lines[0].startswith("release_notes: ") and needle in lines[0],
        f"{label}: one release_notes: line naming {needle!r}",
    )


r.section("matching tag prints that section's body")
ok = run("v1.2.0", "1.2.0")
r.ok(ok.returncode == 0, "exit 0")
r.ok(
    ok.stdout == "**Second release.**\n\n- 한국어 항목 — with an em dash.\n",
    "exactly the body, blank edges removed",
)
r.ok(ok.stderr == "", "nothing on stderr")

r.section("the next ## heading ends the section")
first = run("v1.1.0", "1.1.0")
r.ok(first.returncode == 0, "exit 0")
r.ok(first.stdout == "- First release.\n", "stops before ## v1.0.0")

r.section("refused tags")
refused(run("v1.2.1", "1.2.0"), "1.2.0", "tag differs from the manifest version")
refused(run("v2.0.0", "2.0.0"), "no section", "missing section")
refused(run("v1.0.0", "1.0.0"), "empty", "empty section")

sys.exit(r.finish())
