"""Testes da filtragem de conectividade da Instalação em Lote — sem rede real."""

from __future__ import annotations

import unittest

from remoteops.services.batch_install import REASON_OFFLINE, decide_host_action
from remoteops.utils.host_reachability import (
    HostAccessKind,
    HostReachability,
    decide_batch_connectivity,
)
from remoteops.utils.product_identity import ProductIdentity
from remoteops.utils.psping import PsPingMode, PsPingResult, PsPingState


def _reach(
    kind: HostAccessKind,
    *,
    host: str = "pc01",
    psexec_enabled: bool = False,
    icmp_ok: bool = False,
    tcp_ok: bool = False,
) -> HostReachability:
    icmp_state = PsPingState.SUCCESS if icmp_ok else PsPingState.TIMEOUT
    tcp_state = PsPingState.SUCCESS if tcp_ok else PsPingState.TIMEOUT
    captions = {
        HostAccessKind.PSEXEC_READY: "Online — PsExec acessível",
        HostAccessKind.ICMP_BLOCKED: "Acessível — ICMP bloqueado",
        HostAccessKind.TCP_UNAVAILABLE: "Online — TCP 445 indisponível",
        HostAccessKind.OFFLINE: "Offline ou inacessível",
        HostAccessKind.UNRESOLVED: "Nome não resolvido",
        HostAccessKind.INVALID_HOST: "Host inválido",
        HostAccessKind.CANCELLED: "Verificando…",
    }
    return HostReachability(
        host=host,
        kind=kind,
        caption=captions[kind],
        status_state="online" if psexec_enabled else "offline",
        psexec_enabled=psexec_enabled,
        icmp=PsPingResult(host=host, mode=PsPingMode.ICMP, state=icmp_state),
        tcp=PsPingResult(host=host, mode=PsPingMode.TCP, state=tcp_state, port=445),
    )


_IDENTITY = ProductIdentity(label="App", installer_version="1.0")


class TestBatchConnectivityFilter(unittest.TestCase):
    def test_icmp_blocked_tcp_open_continues(self) -> None:
        reach = _reach(
            HostAccessKind.ICMP_BLOCKED,
            psexec_enabled=True,
            icmp_ok=False,
            tcp_ok=True,
        )
        decision = decide_batch_connectivity(reach)
        self.assertTrue(decision.continue_inventory)
        self.assertTrue(decision.online)
        self.assertTrue(decision.icmp_blocked)
        self.assertIn("ICMP bloqueado", decision.log_message)

        row = decide_host_action(
            host="pc01",
            desired_version="1.0",
            online=decision.online,
            inventory=None,
            identity=_IDENTITY,
        )
        self.assertTrue(row.online)
        self.assertNotEqual(row.reason, REASON_OFFLINE)

    def test_both_success_continues(self) -> None:
        reach = _reach(
            HostAccessKind.PSEXEC_READY,
            psexec_enabled=True,
            icmp_ok=True,
            tcp_ok=True,
        )
        decision = decide_batch_connectivity(reach)
        self.assertTrue(decision.continue_inventory)
        self.assertFalse(decision.icmp_blocked)
        self.assertEqual(decision.log_message, "")

    def test_icmp_ok_tcp_closed_is_offline(self) -> None:
        reach = _reach(
            HostAccessKind.TCP_UNAVAILABLE,
            psexec_enabled=False,
            icmp_ok=True,
            tcp_ok=False,
        )
        decision = decide_batch_connectivity(reach)
        self.assertFalse(decision.continue_inventory)
        self.assertFalse(decision.online)
        row = decide_host_action(
            host="pc01",
            desired_version="1.0",
            online=decision.online,
            inventory=None,
            identity=_IDENTITY,
        )
        self.assertFalse(row.online)
        self.assertEqual(row.reason, REASON_OFFLINE)

    def test_both_fail_offline(self) -> None:
        reach = _reach(HostAccessKind.OFFLINE, psexec_enabled=False)
        decision = decide_batch_connectivity(reach)
        self.assertFalse(decision.continue_inventory)
        self.assertIn("offline ou inacessível", decision.log_message)

    def test_unresolved_skipped(self) -> None:
        reach = _reach(HostAccessKind.UNRESOLVED, host="ghost")
        decision = decide_batch_connectivity(reach)
        self.assertFalse(decision.continue_inventory)
        self.assertIn("nome não resolvido", decision.log_message)

    def test_invalid_host_skipped(self) -> None:
        reach = _reach(HostAccessKind.INVALID_HOST, host="bad host")
        decision = decide_batch_connectivity(reach)
        self.assertFalse(decision.continue_inventory)
        self.assertIn("host inválido", decision.log_message)


if __name__ == "__main__":
    unittest.main()
