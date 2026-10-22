"""Run every test.

    python3 tests/run.py            everything
    python3 tests/run.py filter     only files whose name contains "filter"

**Neither Home Assistant nor any external package is needed** — Python alone is enough.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(p for p in HERE.glob("test_*.py") if want in p.name)
    if not files:
        print(f"no tests to run (filter: {want!r})")
        return 1

    total = failed = 0
    broken: list[str] = []
    for path in files:
        print(f"\n{'═' * 60}\n{path.name}")
        run = subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True, cwd=HERE
        )
        print(run.stdout, end="")
        if run.stderr:
            print(run.stderr, end="", file=sys.stderr)
        for line in run.stdout.splitlines():
            if line.endswith("failed") and "passed" in line:
                passed, rest = line.split(" passed / ", 1)
                total += int(passed)
                failed += int(rest.split()[0])
        if run.returncode != 0:
            broken.append(path.name)

    print(f"\n{'═' * 60}")
    # **A file that dies counts as a failure.** When a test file dies part-way through an
    # exception, its own "N passed / M failed" line never appears. The total used to leave that
    # file out entirely and report green with 0 failures — only the exit code was 1. To anyone
    # reading the numbers, that looked like a pass.
    print(f"total: {total} passed / {failed + len(broken)} failed")
    if broken:
        print(f"{len(broken)} file(s) died while running: " + ", ".join(broken))
        print("  ^ the totals above do not include the passes from those files.")
    return 1 if broken or failed else 0


if __name__ == "__main__":
    sys.exit(main())
