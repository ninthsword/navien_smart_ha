"""Prepare source-derived typing exports only in this repository's HA 2026.10 venv."""

from __future__ import annotations

import ast
import hashlib
import importlib
import stat
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from types import ModuleType

VERSIONS = {
    "homeassistant": "2026.10.0",
    "pytest-homeassistant-custom-component": "0.13.370",
    "probatio": "0.13.0",
}
VOLUPTUOUS_EXPORTS = (
    "Schema", "Required", "Optional", "In", "Marker", "Invalid",
    "All", "Length", "Coerce", "Range",
)
COMPONENT_EXPORTS = (
    ("binary_sensor", "BinarySensorDeviceClass"),
    ("switch", "SwitchDeviceClass"),
    ("fan", "FanEntityFeature"),
)
OUTPUT_PATHS = (
    "voluptuous/__init__.pyi",
    *(f"homeassistant/components/{component}/__init__.pyi" for component, _ in COMPONENT_EXPORTS),
)


class TypingGuardError(RuntimeError):
    """The pinned environment or source shape differs from the reviewed contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise TypingGuardError(message)


def validate_venv(repo: Path, prefix: Path, base_prefix: Path, cwd: Path) -> Path:
    expected = repo.resolve() / ".venv-ha2026.10"
    require(cwd.resolve() == repo.resolve(), "Run from the current repository root")
    require(expected.is_dir() and not expected.is_symlink(), "Dedicated HA 2026.10 venv required")
    resolved = prefix.resolve()
    require(resolved == expected.resolve(), "Interpreter must belong to this repository's HA 2026.10 venv")
    require(resolved != base_prefix.resolve(), "A base interpreter cannot prepare typing overlays")
    return resolved


def validate_versions(python: tuple[int, int, int], versions: Mapping[str, str]) -> None:
    require(python == (3, 14, 7), "Python 3.14.7 is required")
    for package, expected in VERSIONS.items():
        require(versions.get(package) == expected, f"Expected {package}=={expected}")


def reject_incompatible_voluptuous(probatio: ModuleType, modules: Mapping[str, ModuleType]) -> None:
    for name, module in tuple(modules.items()):
        if name != "voluptuous" and not name.startswith("voluptuous."):
            continue
        for symbol in VOLUPTUOUS_EXPORTS:
            if name == "voluptuous" or hasattr(module, symbol):
                require(
                    getattr(module, symbol, None) is getattr(probatio, symbol),
                    f"Reject pre-existing incompatible voluptuous reference: {name}.{symbol}",
                )


def runtime_identities() -> dict[str, ModuleType]:
    probatio = importlib.import_module("probatio")
    reject_incompatible_voluptuous(probatio, sys.modules)
    importlib.import_module("homeassistant")
    voluptuous = importlib.import_module("voluptuous")
    for symbol in VOLUPTUOUS_EXPORTS:
        require(getattr(voluptuous, symbol) is getattr(probatio, symbol), f"Runtime alias differs: {symbol}")
    components = {}
    for component, symbol in COMPONENT_EXPORTS:
        module = importlib.import_module(f"homeassistant.components.{component}")
        const = importlib.import_module(f"homeassistant.components.{component}.const")
        require(getattr(module, symbol) is getattr(const, symbol), f"Runtime enum identity differs: {symbol}")
        components[component] = module
    return components


def enum_import(tree: ast.Module, symbol: str, expected_alias: str | None) -> ast.alias:
    matches = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] == symbol or alias.asname == symbol:
                    matches.append((node, alias))
    require(len(matches) == 1, f"Expected exactly one native import of {symbol}")
    node, alias = matches[0]
    require(
        isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "const",
        f"Expected a relative .const import of {symbol}",
    )
    require(alias.name == symbol and alias.asname == expected_alias, f"Unexpected native alias for {symbol}")
    return alias


def validate_enum_ast(original: ast.Module, generated: ast.Module, symbol: str) -> None:
    enum_import(original, symbol, None)
    # Normalize a fresh tree so validation never changes either caller's AST.
    normalized = ast.parse(ast.unparse(generated))
    enum_import(normalized, symbol, symbol).asname = None
    require(
        ast.dump(original, include_attributes=False) == ast.dump(normalized, include_attributes=False),
        f"Unauthorized AST modification while exporting {symbol}",
    )


def derive_enum_stub(source: bytes, symbol: str) -> bytes:
    original = ast.parse(source)
    generated = ast.parse(source)
    enum_import(generated, symbol, None).asname = symbol
    rendered = (ast.unparse(generated) + "\n").encode()
    validate_enum_ast(original, ast.parse(rendered), symbol)
    return rendered


def contained_file(site: Path, path: Path) -> Path:
    require(site.is_dir() and site.resolve() == site, "Expected a real site-packages directory")
    require(path.is_relative_to(site), "Path is outside the dedicated site-packages directory")
    require(path.resolve() == path, f"Symbolic link or path escape is forbidden: {path.name}")
    return path


def install_overlays(site: Path, outputs: Mapping[Path, bytes]) -> int:
    allowed = {site / relative for relative in OUTPUT_PATHS}
    require(set(outputs) == allowed, "Typing output paths differ from the four-file whitelist")
    missing = []
    for target, expected in outputs.items():
        contained_file(site, target)
        require(target.parent.is_dir(), f"Missing installed package directory: {target.parent.name}")
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            missing.append(target)
            continue
        require(stat.S_ISREG(mode), f"Unexpected typing target type: {target.name}")
        require(target.read_bytes() == expected, f"Unexpected existing typing target bytes: {target.name}")
    # Every target has passed preflight before the first exclusive creation.
    for target in missing:
        with target.open("xb") as handle:
            handle.write(outputs[target])
    return len(missing)


def runtime_hashes(paths: Sequence[Path]) -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def protected_runtime_files(site: Path) -> list[Path]:
    paths = set()
    for package in (*VERSIONS, "voluptuous"):
        distribution = metadata.distribution(package)
        files = distribution.files
        if files is None:
            raise TypingGuardError(f"Missing installed file inventory: {package}")
        metadata_paths = []
        for file in files:
            if file.suffix != ".py" and file.name != "METADATA":
                continue
            path = contained_file(site, Path(str(distribution.locate_file(file))))
            require(path.is_file(), f"Missing protected runtime file: {file.name}")
            paths.add(path)
            if file.name == "METADATA":
                metadata_paths.append(path)
        require(len(metadata_paths) == 1, f"Expected exactly one METADATA file: {package}")
    return sorted(paths)


def main() -> None:
    require(len(sys.argv) == 1, "This generator accepts no arguments")
    repo = Path(__file__).resolve().parents[1]
    prefix = validate_venv(repo, Path(sys.prefix), Path(sys.base_prefix), Path.cwd())
    validate_versions(sys.version_info[:3], {package: metadata.version(package) for package in VERSIONS})
    site = Path(sysconfig.get_path("purelib"))
    require(site.resolve().is_relative_to(prefix), "site-packages must be inside the dedicated venv")
    protected = protected_runtime_files(site)
    before = runtime_hashes(protected)
    modules = runtime_identities()
    outputs = {
        site / OUTPUT_PATHS[0]: "".join(
            f"from probatio import {symbol} as {symbol}\n" for symbol in VOLUPTUOUS_EXPORTS
        ).encode()
    }
    for component, symbol in COMPONENT_EXPORTS:
        source = site / f"homeassistant/components/{component}/__init__.py"
        require(modules[component].__file__ is not None, f"Missing native module source: {component}")
        require(
            Path(str(modules[component].__file__)).resolve() == contained_file(site, source),
            f"Unexpected native module origin: {component}",
        )
        outputs[source.with_suffix(".pyi")] = derive_enum_stub(source.read_bytes(), symbol)
    created = install_overlays(site, outputs)
    require(runtime_hashes(protected) == before, "Original runtime or distribution metadata changed")
    runtime_identities()
    print(f"HA 2026.10 typing exports verified; {created} files created; runtime and metadata unchanged")


if __name__ == "__main__":
    main()
