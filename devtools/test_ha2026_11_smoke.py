"""Tests for the network guard of devtools/ha2026_11_smoke.py, on the standard library alone."""

from __future__ import annotations

import contextlib
import io
import socket
import unittest
from importlib.metadata import PackageNotFoundError
from unittest import mock

import ha2026_11_smoke as smoke


class NetworkGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.attempts: list[str] = []
        self.addCleanup(smoke.install_network_guard(self.attempts))

    def test_refuses_and_records_tcp_connect_outside_loopback(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with self.assertRaises(smoke.GuardRefusal):
                sock.connect(("192.0.2.1", 80))
        self.assertEqual(self.attempts, ["connect ('192.0.2.1', 80)"])

    def test_refuses_and_records_lookup_of_a_name(self) -> None:
        with self.assertRaises(smoke.GuardLookupRefusal):
            socket.getaddrinfo("example.invalid", 80)
        self.assertEqual(self.attempts, ["getaddrinfo 'example.invalid'"])

    def test_allows_tcp_connect_to_loopback_listener(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect(listener.getsockname())
        self.assertEqual(self.attempts, [])

    def test_allows_udp_connect_to_multicast(self) -> None:
        # Only the guard's verdict is tested: a host without a route may refuse the connect
        # itself (for example ENETUNREACH), which is not the guard's refusal.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.connect(("224.0.0.251", 1))
            except smoke.GuardRefusal:
                self.fail("the guard refused a UDP connect")
            except OSError:
                pass
        self.assertEqual(self.attempts, [])

    def test_allows_numeric_lookup(self) -> None:
        infos = socket.getaddrinfo("192.0.2.1", 80, proto=socket.IPPROTO_TCP)
        self.assertIn("192.0.2.1", [info[4][0] for info in infos])
        self.assertEqual(self.attempts, [])

    def test_records_every_refused_attempt_in_order(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            self.assertNotEqual(sock.connect_ex(("192.0.2.1", 443)), 0)
        with self.assertRaises(socket.gaierror):
            socket.getaddrinfo("example.invalid", 443)
        self.assertEqual(
            self.attempts,
            ["connect_ex ('192.0.2.1', 443)", "getaddrinfo 'example.invalid'"],
        )


class VersionGateTest(unittest.TestCase):
    def test_exits_2_when_home_assistant_is_not_installed(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.object(smoke, "version", side_effect=PackageNotFoundError("homeassistant")),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(smoke.main(), 2)
        self.assertIn(f"this run needs {smoke.EXPECTED_CORE}", out.getvalue())


if __name__ == "__main__":
    unittest.main()
