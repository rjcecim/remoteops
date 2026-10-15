"""Ciclo de vida da verificação de host na aba PsExec, sem rede real."""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from remoteops.utils.host_reachability import HostAccessKind, HostReachability
from remoteops.utils.ping import IcmpPingKind, IcmpPingStatus
from remoteops.utils.psping import PsPingMode, PsPingResult, PsPingState


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _ready(host: str) -> HostReachability:
    from remoteops.utils.host_reachability import probe_host_reachability

    return probe_host_reachability(
        host,
        psping_present=False,
        icmp_fallback=lambda _host: IcmpPingStatus(True, IcmpPingKind.OK, ""),
        tcp_fallback=lambda _host, _port: "open",
        log=False,
        use_cache=False,
    )


def _cancelled(host: str) -> HostReachability:
    return HostReachability(
        host=host,
        kind=HostAccessKind.CANCELLED,
        caption="Verificando…",
        status_state="checking",
        psexec_enabled=False,
        cancelled=True,
    )


def _wait_until(predicate, timeout_s: float = 2.0) -> bool:
    loop = QEventLoop()
    state = {"done": False}

    def _poll() -> None:
        if predicate():
            state["done"] = True
            loop.quit()
            return
        QTimer.singleShot(20, _poll)

    QTimer.singleShot(20, _poll)
    QTimer.singleShot(int(timeout_s * 1000), loop.quit)
    loop.exec()
    return state["done"]


class PsExecConnectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.ui.tabs.psexec import PsExecTab
        from remoteops.utils.domain import HostUserDomain

        self._domain_patch = patch(
            "remoteops.ui.tabs.psexec.lookup_host_userdomain",
            return_value=HostUserDomain(),
        )
        self._domain_patch.start()
        self.tab = PsExecTab()

    def tearDown(self) -> None:
        self.tab.shutdown(wait_ms=2000)
        self._domain_patch.stop()
        self.tab.deleteLater()
        QApplication.processEvents()

    def test_widgets_and_idle_state(self) -> None:
        self.assertEqual(self.tab.host_status_label.text(), "Aguardando host")
        self.assertFalse(self.tab.recheck_btn.isEnabled())
        self.assertTrue(self.tab.diagnose_btn.isEnabled())
        self.assertIn("cmd.exe", self.tab.remote_cmd_edit.placeholderText())
        self.assertNotIn("PsExec acessível", self.tab.host_status_label.text())

    def test_empty_and_invalid_do_not_probe(self) -> None:
        calls: list[str] = []

        def _probe(host, **_kwargs):
            calls.append(host)
            return _ready(host)

        with patch("remoteops.ui.tabs.psexec.probe_host_reachability", side_effect=_probe):
            self.tab.host_edit.setText("PC 01")
            self.assertEqual(self.tab.host_status_label.text(), "Host inválido")
            self.assertFalse(self.tab._host_status_timer.isActive())
            self.tab.host_edit.clear()
            self.assertEqual(self.tab.host_status_label.text(), "Aguardando host")
            self.assertFalse(
                _wait_until(lambda: bool(calls), timeout_s=0.8),
                calls,
            )
        self.assertEqual(calls, [])

    def test_valid_host_starts_check_and_keeps_typed_command(self) -> None:
        self.tab.remote_cmd_edit.setText("hostname")
        with patch(
            "remoteops.ui.tabs.psexec.probe_host_reachability",
            side_effect=lambda host, **_kwargs: _ready(host),
        ):
            self.tab.host_edit.setText("PC01")
            self.assertEqual(self.tab.host_status_label.text(), "Verificando…")
            self.assertFalse(self.tab.is_host_online)
            self.assertTrue(
                _wait_until(
                    lambda: self.tab.host_status_label.text() == "Rede acessível"
                )
            )
            self.assertTrue(self.tab.is_host_online)
            self.assertIn("ICMP:", self.tab.host_status_label.toolTip())
            self.assertIn("TCP 445:", self.tab.host_status_label.toolTip())
            self.assertEqual(self.tab.remote_cmd_edit.text(), "hostname")
            self.tab.host_edit.setText("PC02")
            self.assertEqual(self.tab.remote_cmd_edit.text(), "hostname")
            self.assertFalse(self.tab.is_host_online)
            self.assertIsNone(self.tab._last_reachability)
            self.tab._host_status_timer.stop()
            self.tab._supersede_host_check()

    def test_stale_generation_does_not_update_ui(self) -> None:
        self.tab.host_edit.blockSignals(True)
        self.tab.host_edit.setText("PC02")
        self.tab.host_edit.blockSignals(False)
        self.tab._host_status_wanted = "PC02"
        self.tab._host_status_generation = 2
        self.tab._on_host_status_result(1, "PC01", _ready("PC01"))
        self.assertFalse(self.tab.is_host_online)
        self.assertIsNone(self.tab._last_reachability)
        self.tab._on_host_status_result(2, "PC02", _ready("PC02"))
        self.assertTrue(self.tab.is_host_online)
        self.assertEqual(self.tab._last_reachability.host, "PC02")

    def test_rapid_host_switch_keeps_only_the_latest(self) -> None:
        calls: list[str] = []

        def _probe(host, **kwargs):
            calls.append(host)
            cancel = kwargs.get("should_cancel")
            while not (cancel and cancel()):
                time.sleep(0.02)
            return _cancelled(host)

        with patch("remoteops.ui.tabs.psexec.probe_host_reachability", side_effect=_probe):
            self.tab._host_status_generation = 1
            self.tab._host_status_wanted = "PC01"
            self.tab.host_edit.blockSignals(True)
            self.tab.host_edit.setText("PC01")
            self.tab.host_edit.blockSignals(False)
            self.tab._start_host_status_worker("PC01", 1, use_cache=True)
            self.assertTrue(_wait_until(lambda: calls == ["PC01"]))
            self.tab.host_edit.blockSignals(True)
            self.tab.host_edit.setText("PC03")
            self.tab.host_edit.blockSignals(False)
            self.tab._check_host_status()
            self.assertEqual(self.tab._host_status_pending[0], "PC03")
            self.assertTrue(
                _wait_until(lambda: calls[:2] == ["PC01", "PC03"] or calls == ["PC01", "PC03"])
            )
            self.assertEqual(calls[0], "PC01")
            self.assertEqual(calls[-1], "PC03")
            self.assertFalse(self.tab.is_host_online)

    def test_shutdown_during_check_does_not_leave_a_running_worker(self) -> None:
        def _probe(host, **kwargs):
            cancel = kwargs.get("should_cancel")
            while not (cancel and cancel()):
                time.sleep(0.02)
            return _cancelled(host)

        with patch("remoteops.ui.tabs.psexec.probe_host_reachability", side_effect=_probe):
            self.tab._start_host_status_worker("PC01", 1, use_cache=False)
            self.assertTrue(_wait_until(lambda: self.tab._host_status_worker is not None and self.tab._host_status_worker.isRunning()))
            self.tab.shutdown(wait_ms=2000)
            running = [
                worker
                for worker in self.tab._live_workers
                if worker.isRunning()
            ]
        self.assertEqual(running, [])
        self.assertTrue(self.tab._closing)

    def test_replaced_tcp_worker_is_not_dropped(self) -> None:
        finished: list[str] = []

        def _probe(host, **kwargs):
            cancel = kwargs.get("should_cancel")
            while not (cancel and cancel()):
                time.sleep(0.02)
            finished.append(host)
            return PsPingResult(
                host=host,
                mode=PsPingMode.TCP,
                state=PsPingState.CANCELLED,
                port=445,
                cancelled=True,
                message="Teste cancelado.",
            )

        with patch("remoteops.ui.tabs.psexec.probe_tcp_445", side_effect=_probe):
            self.tab.host_edit.blockSignals(True)
            self.tab.host_edit.setText("PC01")
            self.tab.host_edit.blockSignals(False)
            self.tab.start_tcp_precheck()
            self.assertTrue(_wait_until(lambda: self.tab._tcp_precheck_worker is not None and self.tab._tcp_precheck_worker.isRunning()))
            self.tab.start_tcp_precheck()
            second = self.tab._tcp_precheck_worker
            self.assertTrue(_wait_until(lambda: len(finished) >= 1))
            QApplication.processEvents()
            self.assertIs(self.tab._tcp_precheck_worker, second)
            self.assertTrue(second.isRunning())

    def test_recheck_bypasses_cache(self) -> None:
        flags: list[bool] = []

        def _probe(host, **kwargs):
            flags.append(bool(kwargs.get("use_cache", True)))
            return _ready(host)

        with patch("remoteops.ui.tabs.psexec.probe_host_reachability", side_effect=_probe):
            self.tab.host_edit.setText("PC01")
            self.assertTrue(_wait_until(lambda: bool(flags)))
            self.assertTrue(
                _wait_until(lambda: self.tab.host_status_label.text() == "Rede acessível")
            )
            self.tab.recheck_btn.click()
            self.assertTrue(_wait_until(lambda: False in flags))
        self.assertIn(False, flags)


if __name__ == "__main__":
    unittest.main()
