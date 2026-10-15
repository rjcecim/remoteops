"""Plano congelado, fases de Executar e proteção do pré-teste TCP."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from remoteops.core.models import CommandSpec
from remoteops.core.run_attempt import (
    RunPhase,
    freeze_plan,
    new_execution_blocked,
    phase_is_busy,
    plan_blocking_errors,
)
from remoteops.utils.domain import HostUserDomain
from remoteops.utils.host_reachability import HostAccessKind, HostReachability
from remoteops.utils.redaction import REDACTED


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _quiet_probe(host, **_kwargs):
    return HostReachability(
        host=host,
        kind=HostAccessKind.CANCELLED,
        caption="",
        status_state="idle",
        psexec_enabled=False,
        cancelled=True,
    )


class RunAttemptUnitTests(unittest.TestCase):
    def test_freeze_plan_copies_args_and_metadata(self) -> None:
        spec = CommandSpec.from_argv(
            ["PsExec.exe", r"\\PC01", "hostname"],
            metadata={"kind": "psexec", "psexec_detached": True},
        )
        frozen = freeze_plan([spec])
        self.assertEqual(len(frozen), 1)
        frozen[0].args.append("extra")
        frozen[0].metadata["kind"] = "changed"
        self.assertNotIn("extra", spec.args)
        self.assertEqual(spec.metadata["kind"], "psexec")

    def test_plan_blocking_errors_from_metadata(self) -> None:
        spec = CommandSpec(
            executable="PsExec.exe",
            display_command="# Erro PsExec: conflito",
            metadata={"kind": "psexec", "psexec_errors": ["-f requer -c"]},
        )
        self.assertEqual(plan_blocking_errors([spec]), ["-f requer -c"])

    def test_phase_busy_and_guard(self) -> None:
        self.assertTrue(phase_is_busy(RunPhase.PRECHECKING))
        self.assertFalse(phase_is_busy(RunPhase.IDLE))
        self.assertTrue(
            new_execution_blocked(
                phase_busy=True,
                executor_busy=False,
                awaiting_followup=False,
            )
        )


class RunAttemptWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def _open_window(self):
        from remoteops.ui.main_window import MainWindow

        patches = [
            patch(
                "remoteops.ui.tabs.psexec.lookup_host_userdomain",
                return_value=HostUserDomain(),
            ),
            patch(
                "remoteops.ui.tabs.psexec.local_logon_domain",
                return_value=HostUserDomain(),
            ),
            patch(
                "remoteops.ui.tabs.psexec.probe_host_reachability",
                side_effect=_quiet_probe,
            ),
            patch(
                "remoteops.ui.tabs.psexec.query_remote_cpu_topology",
                return_value=(None, "Falha ao consultar os processadores."),
            ),
        ]
        for item in patches:
            item.start()
        window = MainWindow()
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        QApplication.processEvents()
        tab = window.psexec_tab
        tab._host_status_timer.stop()
        tab._cpu_refresh_timer.stop()
        tab._session_refresh_timer.stop()
        return window, patches

    def tearDown(self) -> None:
        patches = getattr(self, "_patches", None)
        window = getattr(self, "_window", None)
        if window is not None:
            window.psexec_tab.shutdown(wait_ms=200)
            window.close()
            QApplication.processEvents()
        if patches:
            for item in reversed(patches):
                item.stop()

    def test_external_checkbox_label(self) -> None:
        self._window, self._patches = self._open_window()
        label = self._window.command_preview.external_cmd_check.text()
        self.assertEqual(label, "Abrir em console externo")

    def test_frozen_plan_survives_param_change_during_precheck(self) -> None:
        self._window, self._patches = self._open_window()
        window = self._window
        tab = window.psexec_tab
        tab.host_edit.setText("PC01")
        tab._host_online = True
        tab.remote_cmd_edit.setText("hostname")
        tab.flag_accepteula.setChecked(True)
        launched: list = []

        def _capture(plan, **kwargs):
            launched.append((list(plan), kwargs.get("use_external_console")))
            result = MagicMock()
            result.ok = True
            result.robocopy_started = False
            result.remote_monitored = False
            result.message = "ok"
            return result

        with (
            patch.object(tab, "has_fresh_tcp445", return_value=False),
            patch.object(tab, "start_tcp_precheck") as start_precheck,
            patch.object(window._execution_service, "launch_plan", side_effect=_capture),
        ):
            window.on_run()
            start_precheck.assert_called_once()
            self.assertEqual(window._run_phase, RunPhase.PRECHECKING)
            attempt = window._active_attempt
            self.assertIsNotNone(attempt)
            assert attempt is not None
            original_display = attempt.display_command
            tab.remote_cmd_edit.setText("ipconfig")
            window.update_command()
            window._on_tcp_precheck_finished(True, "")
        self.assertEqual(len(launched), 1)
        argv = launched[0][0][0].argv
        self.assertIn("hostname", argv)
        self.assertNotIn("ipconfig", argv)
        self.assertIn("hostname", original_display)

    def test_host_change_during_precheck_cancels_launch(self) -> None:
        self._window, self._patches = self._open_window()
        window = self._window
        tab = window.psexec_tab
        tab.host_edit.setText("PC01")
        tab._host_online = True
        tab.remote_cmd_edit.setText("hostname")
        with (
            patch.object(tab, "has_fresh_tcp445", return_value=False),
            patch.object(tab, "start_tcp_precheck"),
            patch.object(window._execution_service, "launch_plan") as launch,
        ):
            window.on_run()
            tab.host_edit.setText("PC02")
            window._on_tcp_precheck_finished(True, "")
            launch.assert_not_called()
        self.assertEqual(window._run_phase, RunPhase.IDLE)
        self.assertIsNone(window._active_attempt)

    def test_double_run_is_blocked_while_prechecking(self) -> None:
        self._window, self._patches = self._open_window()
        window = self._window
        tab = window.psexec_tab
        tab.host_edit.setText("PC01")
        tab._host_online = True
        tab.remote_cmd_edit.setText("hostname")
        with (
            patch.object(tab, "has_fresh_tcp445", return_value=False),
            patch.object(tab, "start_tcp_precheck") as start_precheck,
        ):
            window.on_run()
            window.on_run()
            self.assertEqual(start_precheck.call_count, 1)

    def test_cancel_precheck_blocks_late_callback(self) -> None:
        self._window, self._patches = self._open_window()
        window = self._window
        tab = window.psexec_tab
        tab.host_edit.setText("PC01")
        tab._host_online = True
        tab.remote_cmd_edit.setText("hostname")
        with (
            patch.object(tab, "has_fresh_tcp445", return_value=False),
            patch.object(tab, "start_tcp_precheck"),
            patch.object(tab, "cancel_tcp_precheck") as cancel,
            patch.object(window._execution_service, "launch_plan") as launch,
        ):
            window.on_run()
            window.on_stop()
            cancel.assert_called_once()
            window._on_tcp_precheck_finished(True, "")
            launch.assert_not_called()
        self.assertEqual(window._run_phase, RunPhase.IDLE)

    def test_preview_stays_redacted_for_password(self) -> None:
        from remoteops.core.auth_identity import AuthKind

        self._window, self._patches = self._open_window()
        window = self._window
        tab = window.psexec_tab
        tab.host_edit.setText("PC01")
        tab._host_online = True
        tab._select_kind(AuthKind.DOMAIN, clear_account=False)
        tab.domain_edit.setText("EMPRESA")
        tab.user_edit.setText("ana")
        tab.pass_edit.setText("SegredoX")
        tab.remote_cmd_edit.setText("hostname")
        window.update_command()
        preview = window.command_preview.get_command()
        self.assertIn(REDACTED, preview)
        self.assertNotIn("SegredoX", preview)

    def test_realtime_still_before_precheck(self) -> None:
        self._window, self._patches = self._open_window()
        window = self._window
        tab = window.psexec_tab
        tab.host_edit.setText("PC01")
        tab._host_online = True
        tab.remote_cmd_edit.setText("hostname")
        tab.priority_combo.setCurrentIndex(5)
        with (
            patch.object(tab, "has_fresh_tcp445", return_value=False),
            patch.object(tab, "start_tcp_precheck") as precheck,
            patch(
                "remoteops.ui.tabs.psexec.QMessageBox.question",
                return_value=QMessageBox.StandardButton.No,
            ),
        ):
            window.on_run()
            precheck.assert_not_called()
        self.assertIsNone(window._active_attempt)


if __name__ == "__main__":
    unittest.main()
