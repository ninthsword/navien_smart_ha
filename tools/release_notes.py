"""Print a release's notes from CHANGELOG.md, refusing a tag that does not match the manifest.

    python3 tools/release_notes.py v0.18.0

Run from the repository root. The release workflow calls it before it creates a GitHub release,
so a tag pushed with the wrong version or without its CHANGELOG section creates nothing: it
prints one `release_notes: ` line to stderr and exits 1 with nothing on stdout.

Standard library only, so the workflow needs no dependency installed.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")
MANIFEST = Path("custom_components/navien_smarthome/manifest.json")


class NotesError(Exception):
    """A problem that stops the release, named in one line."""


def notes_for(tag: str) -> str:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        changelog = CHANGELOG.read_text(encoding="utf-8")
    except (OSError, ValueError) as err:
        raise NotesError(f"cannot read {err}") from err
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str):
        raise NotesError(f"{MANIFEST} has no string version")
    if tag != f"v{version}":
        raise NotesError(f"tag {tag} does not match manifest version {version}")

    # The CHANGELOG writes release headings as "## vX.Y.Z — YYYY-MM-DD" (an em dash). When a
    # heading appears twice the first one wins.
    heading = re.compile(rf"## {re.escape(tag)} — \d{{4}}-\d{{2}}-\d{{2}}\s*")
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.fullmatch(line)), None)
    if start is None:
        raise NotesError(f"CHANGELOG.md has no section '## {tag} — YYYY-MM-DD'")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    text = "\n".join(body).strip("\n")
    if not text.strip():
        raise NotesError(f"CHANGELOG.md section for {tag} is empty")
    return text + "\n"


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2:
            raise NotesError("usage: python3 tools/release_notes.py TAG")
        notes = notes_for(argv[1])
    except NotesError as err:
        print(f"release_notes: {err}", file=sys.stderr)
        return 1
    # CHANGELOG.md holds em dashes and Korean text; write UTF-8 whatever the locale.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
