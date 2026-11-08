"""Smoke run: Home Assistant 2026.11.0 in-process with the synthetic Navien devices.

Usage: python devtools/ha2026_11_smoke.py (with the 2026.11 environment, no arguments)
Exits 2 unless the installed homeassistant is 2026.11.0. Otherwise it writes a temporary
configuration (recorder on a SQLite file inside it, logger at warning, the integration linked
under custom_components), starts Home Assistant through homeassistant.bootstrap with the Navien
boundary patched as devtools/ui_check/run_hass.py patches it, adds the entry through the config
flow with synthetic credentials, stops Home Assistant and removes the directory. Prints PASS and
exits 0 only when the entry is loaded, the mat's thermostats show make_mat's values, the boiler's
and the Airone's entities exist and no logger under custom_components.navien_smarthome emitted a
WARNING or higher record; otherwise prints each failure and exits 1.

The run is kept off the network by two mechanisms and checked by a third:
1. http binds to 127.0.0.1 on a free port, as devtools/ui_check/check.py does.
2. .storage/core.config_entries is seeded with one marketplace entry disabled by the user, so
   2026.11's default marketplace integration neither creates its entry nor fetches its catalog.
3. install_network_guard, installed before Home Assistant is imported, refuses and records a
   Python-level TCP connect outside loopback and a name lookup other than localhost; a recorded
   attempt fails the run. It does not see a UDP sendto or sockets a C library opens itself, so
   it is the check that nothing Python-level tried, not what keeps the run offline.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import socket
import sys
import tempfile
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CORE = "2026.11.0"
NAVIEN_LOGGER = "custom_components.navien_smarthome"
USERNAME = "nobody@example.invalid"
PASSWORD = "not-a-password"  # synthetic, as in tests_ha/conftest.py
CONFIG = """homeassistant:
  name: 나비엔 2026.11 검증
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
recorder:
  db_url: sqlite:///{db}
logger:
  default: warning
"""


class GuardRefusal(ConnectionRefusedError):
    """A connect the network guard refused."""


class GuardLookupRefusal(socket.gaierror):
    """A name lookup the network guard refused."""


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _connect_refused(sock: socket.socket, address: Any) -> bool:
    """Unix and datagram sockets pass; any other socket only to a loopback address."""
    if sock.family == getattr(socket, "AF_UNIX", None) or sock.type == socket.SOCK_DGRAM:
        return False
    host = address[0] if isinstance(address, tuple) and address else None
    return not _is_loopback(host)


def _lookup_refused(host: object) -> bool:
    """Numeric addresses and localhost pass; any other name is refused."""
    if host is None or _is_loopback(host):
        return False
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        ipaddress.ip_address(str(host).partition("%")[0])
    except ValueError:
        return True
    return False


def install_network_guard(attempts: list[str]) -> Callable[[], None]:
    """Wrap socket.socket.connect, connect_ex and socket.getaddrinfo; return the undo.

    Each refused call appends its target to `attempts`.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        if _connect_refused(self, address):
            attempts.append(f"connect {address!r}")
            raise GuardRefusal(errno.ECONNREFUSED, f"network guard refused {address!r}")
        original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if _connect_refused(self, address):
            attempts.append(f"connect_ex {address!r}")
            return errno.ECONNREFUSED
        return original_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if _lookup_refused(host):
            attempts.append(f"getaddrinfo {host!r}")
            raise GuardLookupRefusal(
                socket.EAI_NONAME, f"network guard refused a lookup of {host!r}"
            )
        return original_getaddrinfo(host, *args, **kwargs)

    replaced = (
        (socket.socket, "connect", connect, original_connect),
        (socket.socket, "connect_ex", connect_ex, original_connect_ex),
        (socket, "getaddrinfo", getaddrinfo, original_getaddrinfo),
    )
    for owner, name, wrapper, _original in replaced:
        setattr(owner, name, wrapper)

    def undo() -> None:
        for owner, name, _wrapper, original in replaced:
            setattr(owner, name, original)

    return undo


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_config(config: Path) -> None:
    """Write configuration.yaml, the integration link and the disabled marketplace entry."""
    from homeassistant import config_entries
    from homeassistant.util import dt as dt_util
    from homeassistant.util.ulid import ulid_now

    db = config / "home-assistant_v2.db"
    (config / "configuration.yaml").write_text(
        CONFIG.format(port=free_port(), db=db), encoding="utf-8"
    )
    (config / "custom_components").mkdir()
    (config / "custom_components" / "navien_smarthome").symlink_to(
        ROOT / "custom_components" / "navien_smarthome"
    )
    now = dt_util.utcnow().isoformat()
    marketplace = {
        "created_at": now,
        "data": {},
        "disabled_by": "user",
        "discovery_keys": {},
        "domain": "marketplace",
        "entry_id": ulid_now(),
        "minor_version": 1,
        "modified_at": now,
        "options": {},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "system",
        "subentries": [],
        "title": "",
        "unique_id": None,
        "version": 1,
    }
    storage = config / ".storage"
    storage.mkdir()
    (storage / config_entries.STORAGE_KEY).write_text(
        json.dumps(
            {
                "version": config_entries.STORAGE_VERSION,
                "minor_version": config_entries.STORAGE_VERSION_MINOR,
                "key": config_entries.STORAGE_KEY,
                "data": {"entries": [marketplace]},
            }
        ),
        encoding="utf-8",
    )


async def _run(config: Path) -> list[str]:
    """Start Home Assistant, add the entry, check it and stop; return the failures."""
    from homeassistant import bootstrap
    from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
    from homeassistant.data_entry_flow import FlowResultType
    from homeassistant.helpers import entity_registry as er
    from homeassistant.runner import RuntimeConfig

    from custom_components.navien_smarthome.const import DOMAIN
    from tests_ha.devices import make_airone, make_boiler, make_mat

    failures: list[str] = []
    hass = await bootstrap.async_setup_hass(
        RuntimeConfig(config_dir=str(config), skip_pip=True)
    )
    if hass is None:
        return ["Home Assistant setup failed"]
    try:
        if hass.config.recovery_mode:
            return ["Home Assistant started in recovery mode"]
        await hass.async_start()
        await hass.async_block_till_done()

        market = hass.config_entries.async_entries("marketplace")
        if len(market) != 1:
            failures.append(f"expected one marketplace entry, found {len(market)}")
        else:
            if market[0].disabled_by is not ConfigEntryDisabler.USER:
                failures.append(f"marketplace entry disabled_by {market[0].disabled_by!r}")
            if market[0].state is not ConfigEntryState.NOT_LOADED:
                failures.append(f"marketplace entry state {market[0].state}")

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
        )
        await hass.async_block_till_done()
        entry = result.get("result")
        if result.get("type") is not FlowResultType.CREATE_ENTRY or entry is None:
            failures.append(f"config flow ended with {result.get('type')}")
            return failures
        if entry.state is not ConfigEntryState.LOADED:
            failures.append(f"Navien entry state {entry.state}")
            return failures

        registry = er.async_get(hass)
        mat = make_mat()
        control = mat.heat_control
        assert control is not None
        for zone in mat.zones:
            unique_id = f"{mat.device_id}_{zone}_thermostat"
            entity_id = registry.async_get_entity_id("climate", DOMAIN, unique_id)
            state = hass.states.get(entity_id) if entity_id else None
            if state is None:
                failures.append(f"no climate state for {unique_id}")
                continue
            expected = {
                "current_temperature": mat.zone_current(zone),
                "temperature": mat.zone_setting(zone),
                "target_temp_step": control.step,
            }
            for name, value in expected.items():
                if state.attributes.get(name) != value:
                    failures.append(
                        f"{entity_id} {name} is {state.attributes.get(name)!r}, expected {value!r}"
                    )
        entries = er.async_entries_for_config_entry(registry, entry.entry_id)
        for label, device in (("boiler", make_boiler()), ("Airone", make_airone())):
            if not any(
                item.unique_id.startswith(device.device_id)
                and hass.states.get(item.entity_id) is not None
                for item in entries
            ):
                failures.append(f"no entity of the synthetic {label}")
    finally:
        await hass.async_stop()
    return failures


def main() -> int:
    try:
        installed = version("homeassistant")
    except PackageNotFoundError:
        installed = "(none)"
    if installed != EXPECTED_CORE:
        print(f"FAIL: homeassistant {installed} is installed, this run needs {EXPECTED_CORE}")
        return 2

    attempts: list[str] = []
    install_network_guard(attempts)
    sys.path.insert(0, str(ROOT))
    from ui_check.run_hass import navien_boundary

    collector = _Collector()
    logging.getLogger(NAVIEN_LOGGER).addHandler(collector)
    failures: list[str] = []
    try:
        with (
            tempfile.TemporaryDirectory(prefix="navien-ha2026-11-") as config,
            navien_boundary(),
        ):
            write_config(Path(config))
            failures = asyncio.run(_run(Path(config)))
    except Exception as err:  # reported with the other failures
        failures.append(f"{type(err).__name__}: {err}")
    failures.extend(
        f"{record.levelname} [{record.name}] {record.getMessage()}"
        for record in collector.records
    )
    failures.extend(f"network guard recorded {attempt}" for attempt in attempts)
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print(f"PASS: Home Assistant {installed} loaded Navien Smart with the synthetic devices")
    return 0


if __name__ == "__main__":
    sys.exit(main())
