"""Testes do PsPing — sem rede real e sem o executável instalado."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.core.process_runner import CapturedProcess
from remoteops.utils.psping import (
    IpFamily,
    PsPingMode,
    PsPingState,
    build_psping_argv,
    get_cached_psping,
    interpret_psping,
    invalidate_psping_cache,
    looks_like_ipv6,
    psping_available,
    resolve_psping_exe,
    run_psping,
    snap_attempts,
    store_cached_psping,
    tcp_destination,
    validate_port,
)


class TestPsPingResolve(unittest.TestCase):
    def test_prefers_64bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsPing64.exe"), "wb").close()
            open(os.path.join(folder, "PsPing.exe"), "wb").close()
            resolved = resolve_psping_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsPing64.exe")
            self.assertTrue(psping_available(folder))

    def test_fallback_to_32bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsPing.exe"), "wb").close()
            resolved = resolve_psping_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsPing.exe")
            self.assertTrue(psping_available(folder))

    def test_missing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            resolved = resolve_psping_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsPing64.exe")
            self.assertFalse(psping_available(folder))
            result = run_psping(
                "pc01",
                pstools_dir=folder,
                use_cache=False,
                log=False,
            )
            self.assertEqual(result.state, PsPingState.TOOL_MISSING)


class TestPsPingArgv(unittest.TestCase):
    def test_icmp_ipv4(self) -> None:
        argv = build_psping_argv(
            exe=r"C:\PSTools\PsPing64.exe",
            host="pc01",
            mode=PsPingMode.ICMP,
            family=IpFamily.IPV4,
            attempts=1,
        )
        self.assertEqual(
            argv,
            [r"C:\PSTools\PsPing64.exe", "-4", "-n", "1", "-w", "0", "-q", "pc01"],
        )

    def test_tcp_ipv4(self) -> None:
        argv = build_psping_argv(
            exe=r"C:\PSTools\PsPing64.exe",
            host="pc01",
            mode=PsPingMode.TCP,
            port=445,
            family=IpFamily.IPV4,
            attempts=1,
        )
        self.assertEqual(
            argv,
            [r"C:\PSTools\PsPing64.exe", "-4", "-n", "1", "-w", "0", "-q", "pc01:445"],
        )

    def test_icmp_ipv6(self) -> None:
        argv = build_psping_argv(
            exe=r"C:\PSTools\PsPing64.exe",
            host="pc01",
            mode=PsPingMode.ICMP,
            family=IpFamily.IPV6,
            attempts=3,
        )
        self.assertEqual(argv[1], "-6")
        self.assertEqual(argv[3], "3")

    def test_tcp_ipv6_literal(self) -> None:
        self.assertTrue(looks_like_ipv6("fe80::1"))
        dest = tcp_destination("fe80::1", 445)
        self.assertEqual(dest, "[fe80::1]:445")
        argv = build_psping_argv(
            exe=r"C:\PSTools\PsPing64.exe",
            host="fe80::1",
            mode=PsPingMode.TCP,
            port=445,
            family=IpFamily.IPV6,
            attempts=1,
        )
        self.assertEqual(argv[-1], "[fe80::1]:445")
        self.assertEqual(argv[1], "-6")

    def test_port_min_max(self) -> None:
        ok, n, err = validate_port(1)
        self.assertTrue(ok)
        self.assertEqual(n, 1)
        self.assertEqual(err, "")
        ok, n, err = validate_port(65535)
        self.assertTrue(ok)
        self.assertEqual(n, 65535)
        build_psping_argv(
            exe="PsPing64.exe",
            host="pc01",
            mode=PsPingMode.TCP,
            port=1,
        )
        build_psping_argv(
            exe="PsPing64.exe",
            host="pc01",
            mode=PsPingMode.TCP,
            port=65535,
        )

    def test_invalid_port(self) -> None:
        self.assertFalse(validate_port(0)[0])
        self.assertFalse(validate_port(65536)[0])
        self.assertFalse(validate_port("abc")[0])
        with self.assertRaises(ValueError):
            build_psping_argv(
                exe="PsPing64.exe",
                host="pc01",
                mode=PsPingMode.TCP,
                port=0,
            )

    def test_invalid_host(self) -> None:
        with self.assertRaises(ValueError):
            build_psping_argv(
                exe="PsPing64.exe",
                host="bad&host",
                mode=PsPingMode.ICMP,
            )
        with self.assertRaises(ValueError):
            build_psping_argv(
                exe="PsPing64.exe",
                host="",
                mode=PsPingMode.ICMP,
            )

    def test_snap_attempts(self) -> None:
        self.assertEqual(snap_attempts(1), 1)
        self.assertEqual(snap_attempts(3), 3)
        self.assertEqual(snap_attempts(5), 5)
        self.assertEqual(snap_attempts(2), 3)
        self.assertEqual(snap_attempts(99), 5)


def _captured(
    *,
    returncode: int = 1,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    cancelled: bool = False,
    spawn_error: str = "",
) -> CapturedProcess:
    return CapturedProcess(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cancelled=cancelled,
        spawn_error=spawn_error,
    )


class TestPsPingInterpret(unittest.TestCase):
    def test_icmp_success(self) -> None:
        captured = _captured(
            returncode=0,
            stdout=b"Ping statistics for 10.0.0.1:\n  Sent = 1, Received = 1, Lost = 0 (0% loss),\n",
        )
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.SUCCESS)
        self.assertTrue(result.ok)

    def test_icmp_failure(self) -> None:
        captured = _captured(
            returncode=1,
            stdout=b"Ping statistics for 10.0.0.1:\n  Sent = 1, Received = 0, Lost = 1 (100% loss),\n",
        )
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.TIMEOUT)
        self.assertFalse(result.ok)

    def test_tcp_success(self) -> None:
        captured = _captured(
            returncode=0,
            stdout=b"TCP connect statistics for 10.0.0.1:445:\n  Sent = 1, Received = 1, Lost = 0 (0% loss),\n",
        )
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.TCP,
            port=445,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.SUCCESS)
        self.assertEqual(result.port, 445)

    def test_connection_refused(self) -> None:
        captured = _captured(
            returncode=1,
            stderr=b"The remote computer refused the network connection.\n",
        )
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.TCP,
            port=445,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.CONNECTION_REFUSED)

    def test_timeout(self) -> None:
        captured = _captured(returncode=1, timed_out=True)
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.TIMEOUT)

    def test_cancelled_not_network_failure(self) -> None:
        captured = _captured(returncode=1, cancelled=True, stdout=b"100% loss")
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.CANCELLED)
        self.assertNotEqual(result.state, PsPingState.CONNECTIVITY_FAILURE)

    def test_name_unresolved(self) -> None:
        captured = _captured(
            returncode=1,
            stderr=b"Error resolving nosuchhost: The requested name is valid, but no data of the requested type was found.\n",
        )
        result = interpret_psping(
            captured,
            host="nosuchhost",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.NAME_UNRESOLVED)

    def test_tool_missing_from_spawn(self) -> None:
        captured = _captured(spawn_error="Executavel nao encontrado: C:\\PSTools\\PsPing64.exe")
        result = interpret_psping(
            captured,
            host="pc01",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        self.assertEqual(result.state, PsPingState.TOOL_MISSING)


class TestPsPingRunnerInjection(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_psping_cache()

    def tearDown(self) -> None:
        invalidate_psping_cache()

    def test_run_with_injected_runner(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            exe = os.path.join(folder, "PsPing64.exe")
            open(exe, "wb").close()

            def runner(argv, **_kwargs):
                self.assertEqual(argv[0], exe)
                self.assertIn("-q", argv)
                self.assertNotIn("-s", argv)
                self.assertNotIn("-b", argv)
                self.assertNotIn("-h", argv)
                return _captured(
                    returncode=0,
                    stdout=b"Lost = 0 (0% loss),\n",
                )

            result = run_psping(
                "pc01",
                pstools_dir=folder,
                runner=runner,
                use_cache=False,
                log=False,
            )
            self.assertTrue(result.ok)

    def test_cache_reused_then_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsPing64.exe"), "wb").close()
            calls = {"n": 0}

            def runner(_argv, **_kwargs):
                calls["n"] += 1
                return _captured(returncode=0, stdout=b"Lost = 0 (0% loss),\n")

            first = run_psping(
                "pc01",
                pstools_dir=folder,
                runner=runner,
                use_cache=True,
                log=False,
            )
            second = run_psping(
                "pc01",
                pstools_dir=folder,
                runner=runner,
                use_cache=True,
                log=False,
            )
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            self.assertEqual(calls["n"], 1)
            cached = get_cached_psping("pc01", PsPingMode.ICMP)
            self.assertIsNotNone(cached)
            invalidate_psping_cache()
            self.assertIsNone(get_cached_psping("pc01", PsPingMode.ICMP))

    def test_diagnostic_skips_cache(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsPing64.exe"), "wb").close()
            calls = {"n": 0}

            def runner(_argv, **_kwargs):
                calls["n"] += 1
                proc = _captured(returncode=0, stdout=b"Lost = 0 (0% loss),\n")
                return proc

            run_psping("pc01", pstools_dir=folder, runner=runner, use_cache=True, log=False)
            run_psping("pc01", pstools_dir=folder, runner=runner, use_cache=False, log=False)
            self.assertEqual(calls["n"], 2)

    def test_cancelled_not_cached(self) -> None:
        result = interpret_psping(
            _captured(cancelled=True),
            host="pc01",
            mode=PsPingMode.ICMP,
            port=None,
            attempts=1,
            family=IpFamily.IPV4,
        )
        store_cached_psping(result)
        self.assertIsNone(get_cached_psping("pc01", PsPingMode.ICMP))


if __name__ == "__main__":
    unittest.main()
