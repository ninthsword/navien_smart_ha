"""Failure-path tests for check.py that never start Home Assistant."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import traceback
import types
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import check  # noqa: E402

STAMP = "2026-10-19 18:46:41.123"
CUSTOM_WARNING = (
    f"{STAMP} WARNING (SyncWorker_0) [homeassistant.loader] We found a custom integration "
    "navien_smarthome which has not been tested by Home Assistant. This component might cause "
    "stability problems, be sure to disable it if you experience issues with Home Assistant"
)
MET_ERROR = f"{STAMP} ERROR (MainThread) [homeassistant.components.met] Error fetching met data"
RADIO_ERROR = (
    f"{STAMP} ERROR (MainThread) [homeassistant.components.radio_browser] "
    "Could not connect to Radio Browser API"
)
NAVIEN_ERROR = (
    f"{STAMP} ERROR (MainThread) [custom_components.navien_smarthome.coordinator] "
    "Unexpected error fetching navien_smarthome data"
)
NAVIEN_CRITICAL = (
    f"{STAMP} CRITICAL (MainThread) [homeassistant.config_entries] "
    "Error setting up entry for navien_smarthome"
)
NAVIEN_FRAME = (
    '  File "/work/custom_components/navien_smarthome/coordinator.py", line 120, '
    "in _async_update_data"
)

NAVIEN_FILE = "/work/custom_components/navien_smarthome/coordinator.py"
NAVIEN_SOURCE = "def _async_update_data():\n    raise ValueError('synthetic Navien failure')\n"
# The record HA logs around a task's traceback; it does not name navien_smarthome itself.
TASK_RECORD = f"{STAMP} ERROR (MainThread) [homeassistant] Error doing job: Task exception was never retrieved"


def navien_function() -> types.FunctionType:
    """Return a function compiled with a file name inside custom_components/navien_smarthome."""
    module = compile(NAVIEN_SOURCE, NAVIEN_FILE, "exec")
    code = next(const for const in module.co_consts if isinstance(const, types.CodeType))
    return types.FunctionType(code, {})


def raised(function: Callable[[], object]) -> BaseException:
    """Call `function` and return what it raised, its traceback kept (assertRaises drops it)."""
    try:
        function()
    except BaseException as err:  # the test inspects whatever the function raised
        return err
    raise AssertionError("nothing was raised")


def group_of_navien_errors() -> None:
    raise ExceptionGroup(
        "unhandled errors in a TaskGroup", [cast(Exception, raised(navien_function())) for _ in range(2)]
    )


def formatted(err: BaseException) -> list[str]:
    """The log lines Python's traceback module writes for `err` after HA's record line."""
    return [TASK_RECORD, *"".join(traceback.format_exception(err)).splitlines()]


def navien_frames(lines: list[str]) -> list[str]:
    return [line for line in lines if NAVIEN_FILE in line]


class ScanLogTest(unittest.TestCase):
    def test_reports_navien_error_critical_and_frame(self) -> None:
        lines = [NAVIEN_ERROR, NAVIEN_CRITICAL, "Traceback (most recent call last):", NAVIEN_FRAME]
        self.assertEqual(check.scan_log(lines), [NAVIEN_ERROR, NAVIEN_CRITICAL, NAVIEN_FRAME])

    def test_reports_frames_of_a_formatted_exception(self) -> None:
        lines = formatted(raised(navien_function()))
        frames = navien_frames(lines)
        self.assertTrue(frames)
        self.assertEqual(check.scan_log(lines), frames)

    def test_reports_frames_of_an_exception_group(self) -> None:
        lines = formatted(raised(group_of_navien_errors))
        frames = navien_frames(lines)
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(frame.lstrip().startswith("|") for frame in frames))
        self.assertNotIn("navien_smarthome", lines[0])
        self.assertEqual(check.scan_log(lines), frames)

    def test_ignores_foreign_errors_and_custom_warning(self) -> None:
        self.assertEqual(check.scan_log([MET_ERROR, RADIO_ERROR, CUSTOM_WARNING]), [])


class LogVerdictTest(unittest.TestCase):
    def verdict(self, *lines: str) -> str | None:
        with tempfile.TemporaryDirectory() as out:
            Path(out, check.LOG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
            return check.log_verdict(Path(out))

    def test_warning_and_foreign_errors_pass(self) -> None:
        self.assertIsNone(self.verdict(CUSTOM_WARNING, MET_ERROR, RADIO_ERROR))

    def test_navien_error_fails(self) -> None:
        verdict = self.verdict(CUSTOM_WARNING, MET_ERROR, RADIO_ERROR, NAVIEN_ERROR)
        self.assertIsNotNone(verdict)
        self.assertIn(NAVIEN_ERROR, verdict or "")

    def test_missing_custom_warning_fails(self) -> None:
        verdict = self.verdict(MET_ERROR, RADIO_ERROR)
        self.assertIsNotNone(verdict)
        self.assertIn("custom-integration warning", verdict or "")


class WaitReadyTest(unittest.TestCase):
    def test_fails_early_when_child_has_exited(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
        process.wait()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "exited with 3"):
            check.wait_ready(check.free_port(), process, timeout=120)
        self.assertLess(time.monotonic() - started, 10)


class FakeProcess:
    """Stands in for the HA child: its first wait is interrupted, as by Ctrl-C."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        if self.calls.count("wait") == 1:
            raise KeyboardInterrupt
        return self.returncode or 0


class StopTest(unittest.TestCase):
    def test_interrupted_wait_kills_and_reaps_then_propagates(self) -> None:
        process = FakeProcess()
        with self.assertRaises(KeyboardInterrupt):
            check.stop(cast("subprocess.Popen[bytes]", process))
        self.assertEqual(process.calls, ["terminate", "wait", "kill", "wait"])


class RefusedOutTest(unittest.TestCase):
    def test_non_empty_dir_is_refused_before_home_assistant_starts(self) -> None:
        with tempfile.TemporaryDirectory() as out:
            Path(out, "earlier.png").write_bytes(b"")
            started = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-B", str(HERE / "check.py"), "--out", out],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FAIL", result.stderr)
            self.assertNotIn("PASS", result.stdout)
            self.assertFalse(Path(out, check.LOG_NAME).exists())
            self.assertEqual(sorted(p.name for p in Path(out).iterdir()), ["earlier.png"])
            self.assertLess(time.monotonic() - started, 30)

    def test_unusable_dir_fails_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            Path(parent, "blocker").write_bytes(b"")
            out = Path(parent, "blocker", "shots")
            result = subprocess.run(
                [sys.executable, "-B", str(HERE / "check.py"), "--out", str(out)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FAIL", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("PASS", result.stdout)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
