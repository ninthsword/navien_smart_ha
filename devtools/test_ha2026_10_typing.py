"""Exercise typing guard failures without altering installed runtime packages."""

from __future__ import annotations

import ast
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from types import ModuleType

from devtools import ha2026_10_typing as tooling


class EnumSourceGuardTests(unittest.TestCase):
    symbol = "BinarySensorDeviceClass"
    source = b"from .const import BinarySensorDeviceClass\nvalue: int = 1\n"

    def test_only_designated_import_alias_changes(self):
        output = tooling.derive_enum_stub(self.source, self.symbol)
        generated = ast.parse(output)
        self.assertEqual(self.symbol, tooling.enum_import(generated, self.symbol, self.symbol).asname)
        tooling.validate_enum_ast(ast.parse(self.source), generated, self.symbol)

    def test_missing_duplicate_wrong_module_and_unexpected_alias_are_rejected(self):
        for source in (
            b"value: int = 1\n",
            self.source + b"from .const import BinarySensorDeviceClass\n",
            b"from other import BinarySensorDeviceClass\n",
            b"from .other import BinarySensorDeviceClass\n",
            b"from ..const import BinarySensorDeviceClass\n",
            b"import other.BinarySensorDeviceClass\n",
            b"from .const import BinarySensorDeviceClass as BinarySensorDeviceClass\n",
            b"from .const import BinarySensorDeviceClass as Renamed\n",
        ):
            with self.subTest(source=source), self.assertRaises(tooling.TypingGuardError):
                tooling.derive_enum_stub(source, self.symbol)

    def test_unauthorized_ast_modification_is_rejected(self):
        output = tooling.derive_enum_stub(self.source, self.symbol)
        for modified in (output + b"unexpected = True\n", output.replace(b"value: int", b"value: str")):
            with self.subTest(modified=modified), self.assertRaisesRegex(
                tooling.TypingGuardError, "Unauthorized AST modification"
            ):
                tooling.validate_enum_ast(ast.parse(self.source), ast.parse(modified), self.symbol)


class TargetGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.site = Path(self.temporary.name).resolve() / "site-packages"
        self.site.mkdir()
        self.outputs = {self.site / relative: b"# typing fixture\n" for relative in tooling.OUTPUT_PATHS}
        for target in self.outputs:
            target.parent.mkdir(parents=True, exist_ok=True)
        self.runtime = self.site / "homeassistant/components/fan/__init__.py"
        self.runtime.write_bytes(b"runtime_marker = 1\n")
        self.metadata = self.site / "fixture.dist-info/METADATA"
        self.metadata.parent.mkdir()
        self.metadata.write_bytes(b"Name: fixture\nVersion: 1\n")

    def test_idempotence_and_runtime_hashes_are_preserved(self):
        before = tooling.runtime_hashes([self.runtime, self.metadata])
        self.assertEqual(4, tooling.install_overlays(self.site, self.outputs))
        initial = {path: path.stat().st_mtime_ns for path in self.outputs}
        self.assertEqual(0, tooling.install_overlays(self.site, self.outputs))
        self.assertEqual(initial, {path: path.stat().st_mtime_ns for path in self.outputs})
        self.assertEqual(before, tooling.runtime_hashes([self.runtime, self.metadata]))
        self.assertEqual(self.outputs, {path: path.read_bytes() for path in self.outputs})

    def test_unexpected_last_target_bytes_fail_before_any_creation(self):
        last = next(reversed(self.outputs))
        last.write_bytes(b"unexpected existing bytes\n")
        before = tooling.runtime_hashes([last, self.runtime, self.metadata])
        with self.assertRaisesRegex(tooling.TypingGuardError, "Unexpected existing typing target bytes"):
            tooling.install_overlays(self.site, self.outputs)
        self.assertEqual(before, tooling.runtime_hashes([last, self.runtime, self.metadata]))
        self.assertTrue(all(not path.exists() for path in self.outputs if path != last))

    def test_symlink_target_fails_before_any_creation(self):
        last = next(reversed(self.outputs))
        last.symlink_to(self.runtime)
        before = self.runtime.read_bytes()
        with self.assertRaises(tooling.TypingGuardError):
            tooling.install_overlays(self.site, self.outputs)
        self.assertEqual(before, self.runtime.read_bytes())
        self.assertTrue(all(not path.exists() for path in self.outputs if path != last))

    def test_output_outside_whitelist_is_rejected(self):
        outputs = dict(self.outputs)
        outside = self.site.parent / "outside.pyi"
        outputs[outside] = b"unexpected\n"
        with self.assertRaisesRegex(tooling.TypingGuardError, "four-file whitelist"):
            tooling.install_overlays(self.site, outputs)
        self.assertFalse(outside.exists())
        self.assertTrue(all(not path.exists() for path in self.outputs))


class EnvironmentGuardTests(unittest.TestCase):
    def test_exact_versions_are_required(self):
        tooling.validate_versions((3, 14, 7), tooling.VERSIONS)
        with self.assertRaises(tooling.TypingGuardError):
            tooling.validate_versions((3, 14, 6), tooling.VERSIONS)
        for package in tooling.VERSIONS:
            versions = dict(tooling.VERSIONS)
            versions[package] = "wrong"
            with self.subTest(package=package), self.assertRaises(tooling.TypingGuardError):
                tooling.validate_versions((3, 14, 7), versions)

    def test_only_current_repository_dedicated_venv_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            prefix = repo / ".venv-ha2026.10"
            prefix.mkdir()
            self.assertEqual(prefix, tooling.validate_venv(repo, prefix, repo.parent, repo))
            for selected_prefix, base_prefix, cwd in (
                (repo, repo.parent, repo),
                (prefix, prefix, repo),
                (prefix, repo.parent, repo.parent),
            ):
                with self.subTest(prefix=selected_prefix, base=base_prefix, cwd=cwd), self.assertRaises(
                    tooling.TypingGuardError
                ):
                    tooling.validate_venv(repo, selected_prefix, base_prefix, cwd)

    def test_incompatible_preimport_is_rejected(self):
        probatio = ModuleType("probatio")
        voluptuous = ModuleType("voluptuous")
        for symbol in tooling.VOLUPTUOUS_EXPORTS:
            marker = object()
            setattr(probatio, symbol, marker)
            setattr(voluptuous, symbol, marker)
        tooling.reject_incompatible_voluptuous(probatio, {"voluptuous": voluptuous})
        voluptuous.__dict__["Schema"] = object()
        with self.assertRaisesRegex(tooling.TypingGuardError, "pre-existing incompatible voluptuous"):
            tooling.reject_incompatible_voluptuous(probatio, {"voluptuous": voluptuous})

    def test_fresh_process_preimport_of_real_voluptuous_is_rejected(self):
        try:
            metadata.distribution("voluptuous")
        except metadata.PackageNotFoundError:
            self.skipTest("Real voluptuous distribution is not installed")
        repo = Path(__file__).resolve().parents[1]
        protected = tooling.protected_runtime_files(Path(sysconfig.get_path("purelib")))
        before = tooling.runtime_hashes(protected)
        result = subprocess.run(
            [sys.executable, "-B", "-c", (
                "import runpy; import voluptuous; "
                "runpy.run_path('devtools/ha2026_10_typing.py', run_name='__main__')"
            )],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("pre-existing incompatible voluptuous", result.stderr)
        self.assertEqual(before, tooling.runtime_hashes(protected))


if __name__ == "__main__":
    unittest.main()
