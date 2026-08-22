"""시험을 전부 돌린다.

    python3 tests/run.py            전부
    python3 tests/run.py filter     이름에 filter 가 들어간 것만

**Home Assistant 도 외부 패키지도 필요 없습니다.** 파이썬만 있으면 됩니다.
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
        print(f"돌릴 시험이 없습니다 (필터: {want!r})")
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
            if line.endswith("실패") and "통과" in line:
                passed, rest = line.split(" 통과 / ", 1)
                total += int(passed)
                failed += int(rest.split()[0])
        if run.returncode != 0:
            broken.append(path.name)

    print(f"\n{'═' * 60}")
    # **죽은 파일도 실패로 센다.** 시험 파일이 중간에 예외로 죽으면 그 파일의
    # 「N 통과 / M 실패」 줄 자체가 안 나온다. 예전에는 합계가 그 파일을 통째로
    # 빼고 「0 실패」로 초록이었다 — 종료 코드만 1 이었다. 수를 보고 판단하는
    # 사람에게는 통과로 보인다.
    print(f"합계: {total} 통과 / {failed + len(broken)} 실패")
    if broken:
        print(f"돌다 죽은 파일 {len(broken)}개: " + ", ".join(broken))
        print("  ↑ 위 합계에 이 파일들의 통과 수는 빠져 있습니다.")
    return 1 if broken or failed else 0


if __name__ == "__main__":
    sys.exit(main())
