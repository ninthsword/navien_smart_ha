"""Offline check: run Home Assistant with synthetic Navien devices and drive its frontend.

Usage: python devtools/ui_check/check.py --out DIR
DIR must be absent or empty. Starts devtools/ui_check/run_hass.py on 127.0.0.1 with a fresh
temporary configuration, completes onboarding, logs in through HA's own login page, adds Navien
Smart through the config flow, opens every device page at desktop and phone widths and stops HA.
Screenshots and the HA log (stdout and stderr) go to DIR. Prints PASS and exits 0 only when every
step passed and the HA log holds HA's custom-integration warning and no Navien error.
"""

from __future__ import annotations

import argparse
import http.client
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import drive  # noqa: E402

LOG_NAME = "home-assistant.log"
READY_TIMEOUT = 240.0
STOP_TIMEOUT = 60.0
CONFIG = """homeassistant:
  name: 나비엔 화면 검증
  country: KR
  language: ko
  time_zone: Asia/Seoul
  unit_system: metric
  latitude: 37.5
  longitude: 127.0
  elevation: 0
http:
  server_host: 127.0.0.1
  server_port: {port}
frontend:
config:
recorder:
"""
# HA's console format: "%(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s"
RECORD = re.compile(r"^\S+ \S+ (?P<level>[A-Z]+) \(")
# A traceback frame as Python's traceback module formats it: "  File "...", line N, in f", and the
# same line behind the "|" margins it adds for the sub-exceptions of an ExceptionGroup.
NAVIEN_FRAME = re.compile(
    r'^[\s|]*File "[^"]*custom_components[/\\]navien_smarthome[/\\][^"]*", line \d+'
)
CUSTOM_WARNING = re.compile(
    r"^\S+ \S+ WARNING \([^)]*\) \[homeassistant\.loader\] "
    r"We found a custom integration navien_smarthome which has not been tested"
)


def scan_log(lines: Iterable[str]) -> list[str]:
    """Return each ERROR/CRITICAL record naming navien_smarthome and each Navien traceback frame."""
    found = []
    for line in lines:
        record = RECORD.match(line)
        if record and record["level"] in ("ERROR", "CRITICAL") and "navien_smarthome" in line:
            found.append(line)
        elif NAVIEN_FRAME.match(line):
            found.append(line)
    return found


def log_verdict(out: Path) -> str | None:
    """Return why DIR/home-assistant.log fails the check, or None when it passes."""
    try:
        lines = (out / LOG_NAME).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as err:
        return f"cannot read {LOG_NAME}: {err}"
    if not any(CUSTOM_WARNING.match(line) for line in lines):
        return f"{LOG_NAME} lacks HA's custom-integration warning for navien_smarthome (stderr not captured)"
    errors = scan_log(lines)
    if errors:
        return "Home Assistant logged Navien errors:\n" + "\n".join(errors[:20])
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(
    port: int,
    process: subprocess.Popen[bytes],
    timeout: float = READY_TIMEOUT,
    interval: float = 2.0,
) -> None:
    """Wait for HA's onboarding API; fail as soon as the child exits first or at the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Home Assistant exited with {process.returncode} before it answered")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/api/onboarding")
            if conn.getresponse().status == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()
        time.sleep(interval)
    raise RuntimeError(f"Home Assistant did not answer on port {port} within {timeout:.0f} s")


def stop(process: subprocess.Popen[bytes]) -> None:
    """Terminate HA, kill it after 60 s, and reap it, also when the wait is interrupted."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise


def write_config(config: Path, port: int) -> None:
    (config / "configuration.yaml").write_text(CONFIG.format(port=port), encoding="utf-8")
    (config / "custom_components").mkdir()
    (config / "custom_components" / "navien_smarthome").symlink_to(
        ROOT / "custom_components" / "navien_smarthome"
    )


def describe(err: BaseException) -> str:
    text = str(err).strip() or type(err).__name__
    return f"{type(err).__name__}: {text}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="absent or empty output directory")
    args = parser.parse_args(argv)
    out: Path = args.out
    try:
        out = out.resolve()
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            print(f"FAIL: {out} exists and is not an empty directory", file=sys.stderr)
            return 1
        out.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        print(f"FAIL: cannot use {out} as the output directory: {describe(err)}", file=sys.stderr)
        return 1

    failure: BaseException | None = None
    try:
        port = free_port()
        with tempfile.TemporaryDirectory(prefix="navien-ui-check-") as config:
            write_config(Path(config), port)
            with (out / LOG_NAME).open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", str(HERE / "run_hass.py"), config],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    wait_ready(port, process)
                    drive.run(f"http://127.0.0.1:{port}", out)
                except BaseException as err:  # reported after HA is stopped
                    failure = err
                finally:
                    stop(process)
            # The child is reaped and the log file closed; the temporary configuration is removed next.
    except BaseException as err:
        failure = failure or err

    verdict = log_verdict(out)
    if failure is not None:
        reason = describe(failure)
        if verdict is not None:
            reason += "\n" + verdict
        print(f"FAIL: {reason}", file=sys.stderr)
        return 1
    if verdict is not None:
        print(f"FAIL: {verdict}", file=sys.stderr)
        return 1
    print(f"PASS: {len(list(out.glob('*.png')))} screenshots in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
