"""Testes da matriz ICMP/TCP e descarte de resultado atrasado — sem rede real."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from remoteops.utils.host_reachability import (
    CAPTION_ICMP_BLOCKED,
    CAPTION_OFFLINE,
    CAPTION_PSEXEC_READY,
    CAPTION_TCP_UNAVAILABLE,
    CAPTION_UNRESOLVED,
    PSPING_MISSING_CAPTION,
    HostAccessKind,
    is_stale_host_result,
    probe_host_reachability,
    psexec_tcp_blocked_message,
)
from remoteops.utils.network_scan import probe_windows_host
from remoteops.utils.ping import IcmpPingKind, IcmpPingStatus
from remoteops.utils.psping import (
    IpFamily,
    PsPingMode,
    PsPingResult,
    PsPingState,
    invalidate_psping_cache,
)


def _icmp(state: PsPingState, host: str = "pc01") -> PsPingResult:
    return PsPingResult(host=host, mode=PsPingMode.ICMP, state=state, attempts=1)


def _tcp(state: PsPingState, host: str = "pc01") -> PsPingResult:
    return PsPingResult(
        host=host, mode=PsPingMode.TCP, state=state, port=445, attempts=1
    )


class TestStaleHostResult(unittest.TestCase):
    def test_discards_late_result(self) -> None:
        self.assertTrue(is_stale_host_result("pc-old", "pc-new", "pc-new"))
        self.assertTrue(is_stale_host_result("pc01", "pc01", "pc02"))
        self.assertFalse(is_stale_host_result("pc01", "pc01", "pc01"))
        self.assertTrue(is_stale_host_result("", "pc01", "pc01"))


class TestHostReachabilityMatrix(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_psping_cache()

    def tearDown(self) -> None:
        invalidate_psping_cache()

    def _probe(self, icmp: PsPingResult, tcp: PsPingResult) -> object:
        with patch(
            "remoteops.utils.host_reachability.run_psping",
            side_effect=[icmp, tcp],
        ):
            return probe_host_reachability(
                "pc01",
                psping_present=True,
                use_cache=False,
                log=False,
            )

    def test_both_success(self) -> None:
        reach = self._probe(_icmp(PsPingState.SUCCESS), _tcp(PsPingState.SUCCESS))
        self.assertEqual(reach.kind, HostAccessKind.PSEXEC_READY)
        self.assertTrue(reach.psexec_enabled)
        self.assertEqual(reach.caption, CAPTION_PSEXEC_READY)
        self.assertEqual(reach.status_state, "online")

    def test_icmp_blocked_tcp_open(self) -> None:
        reach = self._probe(_icmp(PsPingState.TIMEOUT), _tcp(PsPingState.SUCCESS))
        self.assertEqual(reach.kind, HostAccessKind.ICMP_BLOCKED)
        self.assertTrue(reach.psexec_enabled)
        self.assertEqual(reach.caption, CAPTION_ICMP_BLOCKED)
        self.assertEqual(reach.status_state, "warn")

    def test_icmp_ok_tcp_closed(self) -> None:
        reach = self._probe(
            _icmp(PsPingState.SUCCESS), _tcp(PsPingState.CONNECTION_REFUSED)
        )
        self.assertEqual(reach.kind, HostAccessKind.TCP_UNAVAILABLE)
        self.assertFalse(reach.psexec_enabled)
        self.assertEqual(reach.caption, CAPTION_TCP_UNAVAILABLE)

    def test_both_fail(self) -> None:
        reach = self._probe(_icmp(PsPingState.TIMEOUT), _tcp(PsPingState.TIMEOUT))
        self.assertEqual(reach.kind, HostAccessKind.OFFLINE)
        self.assertFalse(reach.psexec_enabled)
        self.assertEqual(reach.caption, CAPTION_OFFLINE)

    def test_unresolved(self) -> None:
        with patch(
            "remoteops.utils.host_reachability.run_psping",
            return_value=_icmp(PsPingState.NAME_UNRESOLVED),
        ):
            reach = probe_host_reachability(
                "nosuchhost",
                psping_present=True,
                use_cache=False,
                log=False,
            )
        self.assertEqual(reach.kind, HostAccessKind.UNRESOLVED)
        self.assertFalse(reach.psexec_enabled)
        self.assertEqual(reach.caption, CAPTION_UNRESOLVED)

    def test_invalid_host(self) -> None:
        reach = probe_host_reachability("bad host", psping_present=True, log=False)
        self.assertEqual(reach.kind, HostAccessKind.INVALID_HOST)
        self.assertFalse(reach.psexec_enabled)

    def test_cancelled(self) -> None:
        with patch(
            "remoteops.utils.host_reachability.run_psping",
            return_value=_icmp(PsPingState.CANCELLED),
        ):
            reach = probe_host_reachability(
                "pc01",
                psping_present=True,
                use_cache=False,
                log=False,
            )
        self.assertEqual(reach.kind, HostAccessKind.CANCELLED)
        self.assertTrue(reach.cancelled)
        self.assertFalse(reach.psexec_enabled)

    def test_fallback_icmp_blocked_tcp_open(self) -> None:
        reach = probe_host_reachability(
            "pc01",
            psping_present=False,
            icmp_fallback=lambda _h: IcmpPingStatus(False, IcmpPingKind.TIMEOUT),
            tcp_fallback=lambda _h, _p: "open",
            use_cache=False,
            log=False,
        )
        self.assertTrue(reach.limited)
        self.assertEqual(reach.kind, HostAccessKind.ICMP_BLOCKED)
        self.assertTrue(reach.psexec_enabled)
        self.assertIn(PSPING_MISSING_CAPTION, reach.tooltip)

    def test_fallback_both_fail(self) -> None:
        reach = probe_host_reachability(
            "pc01",
            psping_present=False,
            icmp_fallback=lambda _h: IcmpPingStatus(False, IcmpPingKind.TIMEOUT),
            tcp_fallback=lambda _h, _p: "timeout",
            use_cache=False,
            log=False,
        )
        self.assertEqual(reach.kind, HostAccessKind.OFFLINE)
        self.assertFalse(reach.psexec_enabled)
        self.assertTrue(reach.limited)

    def test_precheck_message_adapts_to_icmp(self) -> None:
        blocked = psexec_tcp_blocked_message(icmp_ok=True)
        self.assertIn("responde à rede", blocked)
        self.assertIn("TCP 445", blocked)
        both = psexec_tcp_blocked_message(icmp_ok=False)
        self.assertIn("não responde ao ICMP", both)


class TestNetworkScanIgnoresIcmpGate(unittest.TestCase):
    def test_tcp_port_identifies_host_without_icmp(self) -> None:
        with patch(
            "remoteops.utils.network_scan.has_windows_port", return_value=True
        ), patch(
            "remoteops.utils.network_scan.resolve_windows_hostname",
            return_value="PC01",
        ):
            self.assertEqual(probe_windows_host("10.0.0.8"), "PC01")

    def test_no_windows_port_is_skipped(self) -> None:
        with patch(
            "remoteops.utils.network_scan.has_windows_port", return_value=False
        ):
            self.assertIsNone(probe_windows_host("10.0.0.8"))


class TestIpvFamilyPassedToPsPing(unittest.TestCase):
    def test_default_family_is_ipv4(self) -> None:
        seen = {}

        def fake_run(host, **kwargs):
            seen["family"] = kwargs.get("family")
            seen["mode"] = kwargs.get("mode")
            if kwargs.get("mode") == PsPingMode.ICMP:
                return _icmp(PsPingState.SUCCESS, host)
            return _tcp(PsPingState.SUCCESS, host)

        with patch("remoteops.utils.host_reachability.run_psping", side_effect=fake_run):
            probe_host_reachability("pc01", psping_present=True, use_cache=False, log=False)
        self.assertEqual(seen["family"], IpFamily.IPV4)


if __name__ == "__main__":
    unittest.main()
