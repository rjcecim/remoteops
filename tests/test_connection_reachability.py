"""Conectividade da seção Conexão: validação, classificação, cache e PsExec."""

from __future__ import annotations

import unittest
from unittest import mock

from remoteops.core.builder import CommandBuilder
from remoteops.core.psexec_options import build_psexec_prefix_argv
from remoteops.ui.main_window import new_execution_blocked
from remoteops.utils.host_reachability import (
    HostAccessKind,
    decide_batch_connectivity,
    has_fresh_tcp445,
    is_stale_host_result,
    probe_host_reachability,
)
from remoteops.utils.hosts import normalize_hosts_list
from remoteops.utils.ping import (
    IcmpPingKind,
    IcmpPingStatus,
    format_unc_host,
    is_valid_host,
    normalize_host,
)
from remoteops.utils.psping import (
    CACHE_TTL_S,
    IpFamily,
    PsPingMode,
    PsPingResult,
    PsPingState,
    get_cached_psping,
    invalidate_psping_cache,
    run_psping,
    store_cached_psping,
)


def _icmp(kind: IcmpPingKind, *, online: bool | None = None) -> IcmpPingStatus:
    if online is None:
        online = kind == IcmpPingKind.OK
    return IcmpPingStatus(online, kind, "")


def _probe(icmp_kind: IcmpPingKind, tcp: str):
    return probe_host_reachability(
        "PC01",
        psping_present=False,
        icmp_fallback=lambda _host: _icmp(icmp_kind),
        tcp_fallback=lambda _host, _port: tcp,
        log=False,
        use_cache=False,
    )


class HostValidationTests(unittest.TestCase):
    def test_normalize_strips_unc_and_slashes(self) -> None:
        self.assertEqual(normalize_host(r"  \\PC01\  "), "PC01")
        self.assertEqual(normalize_host("//pc01.corp.local/"), "pc01.corp.local")

    def test_empty_and_invalid_hosts(self) -> None:
        self.assertFalse(is_valid_host(""))
        self.assertFalse(is_valid_host("   "))
        self.assertFalse(is_valid_host("PC 01"))
        self.assertFalse(is_valid_host(r"PC01\ADMIN$"))
        self.assertFalse(is_valid_host("PC01/share"))
        self.assertFalse(is_valid_host("PC01:445"))
        self.assertFalse(is_valid_host("PC01 & whoami"))
        self.assertFalse(is_valid_host("999.999.999.999"))
        self.assertFalse(is_valid_host(":::"))
        self.assertFalse(is_valid_host("a..b"))

    def test_accepted_hostname_fqdn_and_ip(self) -> None:
        self.assertTrue(is_valid_host("PC01"))
        self.assertTrue(is_valid_host("pc-01.corp.local"))
        self.assertTrue(is_valid_host("pc-01.corp.local."))
        self.assertTrue(is_valid_host("10.1.2.3"))
        self.assertTrue(is_valid_host("2001:db8::1"))
        self.assertTrue(is_valid_host("[2001:db8::1]"))

    def test_hosts_file_uses_the_same_rules(self) -> None:
        kept = normalize_hosts_list([r"\\PC01", "pc-01.corp.local", "bad host", r"PC01\c$"])
        self.assertEqual(kept, ["PC01", "pc-01.corp.local"])

    def test_unc_token_brackets_only_ipv6(self) -> None:
        self.assertEqual(format_unc_host("PC01"), "PC01")
        self.assertEqual(format_unc_host("10.0.0.8"), "10.0.0.8")
        self.assertEqual(format_unc_host("2001:db8::1"), "[2001:db8::1]")
        self.assertEqual(format_unc_host("[2001:db8::1]"), "[2001:db8::1]")


class ReachabilityClassificationTests(unittest.TestCase):
    def test_empty_host_does_not_probe(self) -> None:
        called = {"n": 0}

        def _boom(*_args, **_kwargs):
            called["n"] += 1
            raise AssertionError("não deveria verificar")

        result = probe_host_reachability("", runner=_boom, log=False)
        self.assertEqual(result.kind, HostAccessKind.INVALID_HOST)
        self.assertFalse(result.psexec_enabled)
        self.assertEqual(called["n"], 0)

    def test_invalid_host_is_validation_error(self) -> None:
        result = probe_host_reachability("PC 01", log=False, psping_present=True)
        self.assertEqual(result.kind, HostAccessKind.INVALID_HOST)
        self.assertEqual(result.caption, "Host inválido")

    def test_dns_unresolved(self) -> None:
        result = _probe(IcmpPingKind.UNRESOLVED, "unresolved")
        self.assertEqual(result.kind, HostAccessKind.UNRESOLVED)
        self.assertEqual(result.caption, "Nome não resolvido")
        self.assertFalse(result.psexec_enabled)

    def test_icmp_ok_tcp_ok_is_network_not_psexec_auth(self) -> None:
        result = _probe(IcmpPingKind.OK, "open")
        self.assertEqual(result.kind, HostAccessKind.PSEXEC_READY)
        self.assertEqual(result.caption, "Rede acessível")
        self.assertNotIn("PsExec acessível", result.caption)
        self.assertNotIn("PsExec acessível", result.tooltip)
        self.assertIn("ICMP:", result.tooltip)
        self.assertIn("TCP 445:", result.tooltip)
        self.assertIn("não confirma autenticação", result.tooltip.casefold())
        self.assertTrue(result.psexec_enabled)
        self.assertEqual(result.status_state, "online")

    def test_icmp_fail_tcp_ok_stays_reachable(self) -> None:
        result = _probe(IcmpPingKind.TIMEOUT, "open")
        self.assertEqual(result.kind, HostAccessKind.ICMP_BLOCKED)
        self.assertIn("Rede acessível", result.caption)
        self.assertIn("ICMP indisponível", result.caption)
        self.assertNotIn("Offline", result.caption)
        self.assertTrue(result.psexec_enabled)
        self.assertEqual(result.status_state, "warn")

    def test_icmp_ok_tcp_fail(self) -> None:
        result = _probe(IcmpPingKind.OK, "timeout")
        self.assertEqual(result.kind, HostAccessKind.TCP_UNAVAILABLE)
        self.assertEqual(result.caption, "TCP 445 indisponível")
        self.assertFalse(result.psexec_enabled)

    def test_both_timeouts_are_unreachable(self) -> None:
        result = _probe(IcmpPingKind.TIMEOUT, "timeout")
        self.assertEqual(result.kind, HostAccessKind.OFFLINE)
        self.assertEqual(result.caption, "Host inacessível")
        self.assertFalse(result.psexec_enabled)

    def test_tcp_refused_is_port_closed_not_ambiguous(self) -> None:
        result = _probe(IcmpPingKind.TIMEOUT, "refused")
        self.assertEqual(result.kind, HostAccessKind.TCP_UNAVAILABLE)

    def test_probe_errors_are_not_reported_as_offline(self) -> None:
        result = _probe(IcmpPingKind.ERROR, "error")
        self.assertEqual(result.kind, HostAccessKind.CHECK_FAILED)
        self.assertEqual(result.caption, "Falha na verificação")
        self.assertNotEqual(result.status_state, "offline")
        self.assertFalse(result.psexec_enabled)

    def test_exception_is_check_failure_not_offline(self) -> None:
        def _raise(_host):
            raise RuntimeError("falha simulada")

        result = probe_host_reachability(
            "PC01",
            psping_present=False,
            icmp_fallback=_raise,
            log=False,
        )
        self.assertEqual(result.kind, HostAccessKind.CHECK_FAILED)

    def test_psping_missing_uses_fallback(self) -> None:
        result = _probe(IcmpPingKind.OK, "open")
        self.assertTrue(result.limited)
        self.assertIn("PsPing", result.tooltip)
        self.assertIn("ping.exe", result.tooltip)
        self.assertNotIn("PsExec acessível", result.caption)

    def test_batch_does_not_call_icmp_blocked_offline(self) -> None:
        reach = _probe(IcmpPingKind.TIMEOUT, "open")
        decision = decide_batch_connectivity(reach)
        self.assertTrue(decision.continue_inventory)
        self.assertIn("ICMP indisponível", decision.log_message)
        failed = decide_batch_connectivity(_probe(IcmpPingKind.ERROR, "error"))
        self.assertFalse(failed.continue_inventory)
        self.assertIn("falha na verificação", failed.log_message)


class CacheAndStaleTests(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_psping_cache()

    def tearDown(self) -> None:
        invalidate_psping_cache()

    def test_stale_result_does_not_match_current_host(self) -> None:
        self.assertTrue(is_stale_host_result("PC01", "PC02", "PC02"))
        self.assertTrue(is_stale_host_result("PC01", "PC01", "PC03"))
        self.assertFalse(is_stale_host_result("PC01", "pc01", "PC01"))
        self.assertTrue(is_stale_host_result("", "PC01", "PC01"))

    def test_expired_cache_forces_a_new_lookup(self) -> None:
        clock = {"t": 1_000.0}
        result = PsPingResult(
            host="PC01",
            mode=PsPingMode.TCP,
            state=PsPingState.SUCCESS,
            port=445,
            message="TCP acessível.",
            ip_family=IpFamily.IPV4,
        )
        with mock.patch("remoteops.utils.psping.time.monotonic", lambda: clock["t"]):
            store_cached_psping(result)
            self.assertTrue(has_fresh_tcp445("PC01"))
            cached = run_psping("PC01", mode=PsPingMode.TCP, port=445, log=False)
            self.assertTrue(cached.ok)
            clock["t"] += CACHE_TTL_S + 0.1
            self.assertIsNone(get_cached_psping("PC01", PsPingMode.TCP, 445, IpFamily.IPV4))
            self.assertFalse(has_fresh_tcp445("PC01"))
            with mock.patch(
                "remoteops.utils.psping.resolve_psping_exe", return_value=""
            ) as resolve:
                again = run_psping("PC01", mode=PsPingMode.TCP, port=445, log=False)
        self.assertFalse(again.ok)
        self.assertEqual(again.state, PsPingState.TOOL_MISSING)
        resolve.assert_called()

    def test_changed_host_does_not_reuse_other_cache_entry(self) -> None:
        store_cached_psping(
            PsPingResult(
                host="PC01",
                mode=PsPingMode.TCP,
                state=PsPingState.SUCCESS,
                port=445,
                ip_family=IpFamily.IPV4,
            )
        )
        self.assertTrue(has_fresh_tcp445("PC01"))
        self.assertFalse(has_fresh_tcp445("PC02"))


class PsExecHostTests(unittest.TestCase):
    def test_prefix_keeps_hostname_and_brackets_ipv6(self) -> None:
        host = build_psexec_prefix_argv(executable="PsExec.exe", host=r"\\PC01")
        ipv6 = build_psexec_prefix_argv(executable="PsExec.exe", host="2001:db8::1")
        self.assertEqual(host[1], r"\\PC01")
        self.assertEqual(ipv6[1], r"\\[2001:db8::1]")

    def test_invalid_host_is_not_built_into_the_command(self) -> None:
        builder = CommandBuilder()
        builder.set_psexec_params({"host": "PC01 & calc", "remote_cmd": "hostname"})
        self.assertIn("inválido", builder.build_psexec())

    def test_valid_manual_command_is_preserved(self) -> None:
        builder = CommandBuilder()
        builder.set_psexec_params({"host": "PC01", "remote_cmd": "hostname"})
        command = builder.build_psexec()
        self.assertIn(r"\\PC01", command)
        self.assertIn("hostname", command)
        self.assertNotIn("inválido", command)


class ExecutionGuardTests(unittest.TestCase):
    def test_blocks_while_running_or_precheck(self) -> None:
        self.assertFalse(
            new_execution_blocked(
                stop_enabled=False,
                executor_busy=False,
                awaiting_followup=False,
                precheck_pending=False,
            )
        )
        self.assertTrue(
            new_execution_blocked(
                stop_enabled=True,
                executor_busy=False,
                awaiting_followup=False,
                precheck_pending=False,
            )
        )
        self.assertTrue(
            new_execution_blocked(
                stop_enabled=False,
                executor_busy=True,
                awaiting_followup=False,
                precheck_pending=False,
            )
        )
        self.assertTrue(
            new_execution_blocked(
                stop_enabled=False,
                executor_busy=False,
                awaiting_followup=True,
                precheck_pending=False,
            )
        )
        self.assertTrue(
            new_execution_blocked(
                stop_enabled=False,
                executor_busy=False,
                awaiting_followup=False,
                precheck_pending=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
