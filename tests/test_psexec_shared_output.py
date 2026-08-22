"""Testes da política de visibilidade e do modo ConPTY vs console externo."""

from __future__ import annotations

# E402/I001: QT_QPA_PLATFORM precisa ser definido antes dos imports do PyQt.
# ruff: noqa: E402, I001

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

# Windows: o plugin offscreen do Qt pode abortar. Preferir o plugin nativo
# e janelas ocultas nos testes de UI.
if sys.platform == "win32":
    os.environ.pop("QT_QPA_PLATFORM", None)
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QSizePolicy

from remoteops.core.models import CommandSpec
from remoteops.services.ops import CommandExecutionService
from remoteops.ui.style import apply_ui_defaults
from remoteops.ui.widgets.log import LogOutputWidget


_QT_APP = None


def _app() -> QApplication:
    global _QT_APP
    existing = QApplication.instance()
    if existing is not None:
        _QT_APP = existing
        return existing
    _QT_APP = QApplication([])
    apply_ui_defaults(_QT_APP)
    return _QT_APP


class FakeFinished:
    def connect(self, _fn):
        return None

    def disconnect(self, _fn):
        return None


class FakeExecutor:
    def __init__(self):
        self.runs = []
        self.finished = FakeFinished()

    def run(self, spec, passwords=None, use_conpty=False):
        self.runs.append(
            {"spec": spec, "passwords": passwords, "use_conpty": bool(use_conpty)}
        )


class LaunchModeTests(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        self.logs: list[str] = []
        self.svc = CommandExecutionService(self.executor, log_fn=self.logs.append)
        self.spec = CommandSpec.from_argv(
            ["PsExec.exe", r"\\host", "cmd", "/c", "echo"],
            metadata={"kind": "psexec"},
        )

    def test_unchecked_uses_conpty(self):
        result = self.svc.launch_plan([self.spec], use_external_console=False)
        self.assertTrue(result.ok)
        self.assertTrue(result.remote_monitored)
        self.assertEqual(len(self.executor.runs), 1)
        self.assertTrue(self.executor.runs[0]["use_conpty"])

    def test_checked_opens_external_console_without_conpty(self):
        with patch(
            "remoteops.services.ops.open_external_console_argv_keep_open"
        ) as opener:
            result = self.svc.launch_plan([self.spec], use_external_console=True)
        self.assertTrue(result.ok)
        self.assertFalse(result.remote_monitored)
        self.assertEqual(self.executor.runs, [])
        opener.assert_called_once()
        argv = opener.call_args[0][0]
        self.assertEqual(argv[0], "PsExec.exe")


class IsolatedClearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def test_clear_does_not_broadcast(self):
        main = LogOutputWidget()
        winget = LogOutputWidget()
        apps = LogOutputWidget()
        main.append_log("main-line")
        winget.append_log("winget-line")
        apps.append_log("apps-line")
        main.clear_log()
        self.assertNotIn("main-line", main.text_edit.toPlainText())
        self.assertIn("winget-line", winget.text_edit.toPlainText())
        self.assertIn("apps-line", apps.text_edit.toPlainText())
        winget.clear_log()
        self.assertIn("apps-line", apps.text_edit.toPlainText())


class SharedOutputVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        from remoteops.ui.main_window import MainWindow

        self.win = MainWindow()
        self.win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        QApplication.processEvents()

    def _switch(self, widget):
        self.win.tabs.setCurrentWidget(widget)
        QApplication.processEvents()

    def test_psexec_shows_shared_widgets_and_single_checkbox(self):
        self.assertEqual(self.win._workspace_mode(), "psexec")
        self.assertTrue(self.win.command_preview.isVisible())
        self.assertTrue(self.win.log_output.isVisible())
        self.assertTrue(self.win.run_button.isVisible())
        self.assertTrue(self.win.stop_button.isVisible())
        self.assertEqual(
            self.win.tabs.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Maximum,
        )
        checks = [
            w
            for w in self.win.findChildren(QCheckBox)
            if w.text() == "Executar como comando externo"
        ]
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].isChecked())
        self.assertIs(
            checks[0],
            self.win.command_preview.external_cmd_check,
        )

    def test_form_tabs_hide_shared_widgets_without_recreating(self):
        preview_id = id(self.win.command_preview)
        log_id = id(self.win.log_output)
        check_id = id(self.win.command_preview.external_cmd_check)

        self.win.update_tab_visibility(True, False)
        QApplication.processEvents()
        self.assertGreaterEqual(self.win.tabs.indexOf(self.win.msi_tab), 0)

        self._switch(self.win.msi_tab)
        self.assertEqual(self.win._workspace_mode(), "form_only")
        self.assertFalse(self.win.command_preview.isVisible())
        self.assertFalse(self.win.log_output.isVisible())
        self.assertFalse(self.win.run_button.isVisible())
        self.assertFalse(self.win.stop_button.isVisible())
        self.assertFalse(self.win.command_preview.external_cmd_check.isVisible())
        self.assertEqual(
            self.win.tabs.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding,
        )
        for card in self.win.msi_tab._form_cards:
            self.assertTrue(card._is_collapsible)
            self.assertTrue(card._reset_btn.isVisibleTo(card))
            self.assertTrue(card._toggle_btn.isVisibleTo(card))

        self.win.psexec_tab.remote_cmd_edit.setText("powershell")
        QApplication.processEvents()
        self._switch(self.win.powershell_tab)
        self.assertEqual(self.win._workspace_mode(), "form_only")
        self.assertFalse(self.win.command_preview.isVisible())

        self.win.psexec_tab.remote_cmd_edit.setText("cmd")
        QApplication.processEvents()
        self._switch(self.win.cmd_tab)
        self.assertEqual(self.win._workspace_mode(), "form_only")
        self.assertFalse(self.win.log_output.isVisible())

        if self.win.tabs.indexOf(self.win.robocopy_tab) == -1:
            self.win.tabs.addTab(self.win.robocopy_tab, "Robocopy")
            QApplication.processEvents()
        self._switch(self.win.robocopy_tab)
        self.assertEqual(self.win._workspace_mode(), "form_only")
        self.assertFalse(self.win.command_preview.isVisible())
        for card in self.win.robocopy_tab._form_cards:
            self.assertTrue(card._is_collapsible)
            self.assertTrue(card._reset_btn.isVisibleTo(card))
            self.assertTrue(card._toggle_btn.isVisibleTo(card))

        self._switch(self.win.psexec_tab)
        self.assertEqual(self.win._workspace_mode(), "psexec")
        self.assertTrue(self.win.command_preview.isVisible())
        self.assertTrue(self.win.log_output.isVisible())
        self.assertEqual(
            self.win.tabs.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Maximum,
        )
        self.assertEqual(id(self.win.command_preview), preview_id)
        self.assertEqual(id(self.win.log_output), log_id)
        self.assertEqual(id(self.win.command_preview.external_cmd_check), check_id)

    def test_output_received_while_hidden_is_kept(self):
        self.win.update_tab_visibility(True, False)
        QApplication.processEvents()
        self.win.log_output.append_log("before-switch")
        self._switch(self.win.msi_tab)
        self.win.log_output.append_log("while-hidden")
        self._switch(self.win.psexec_tab)
        text = self.win.log_output.text_edit.toPlainText()
        self.assertIn("before-switch", text)
        self.assertIn("while-hidden", text)

    def test_tab_change_does_not_stop_executor(self):
        self.win.update_tab_visibility(True, False)
        QApplication.processEvents()
        with patch.object(self.win.executor, "stop") as stop:
            self._switch(self.win.msi_tab)
            self._switch(self.win.psexec_tab)
        stop.assert_not_called()

    def test_preview_updates_from_form_tabs(self):
        self.win.psexec_tab.host_edit.setText("testhost")
        self.win.psexec_tab.pass_edit.setText("secret-password")
        self.win.psexec_tab.remote_cmd_edit.setText("powershell")
        self.win.powershell_tab.command_edit.setPlainText("Get-Date")
        QApplication.processEvents()
        self.assertIn("testhost", self.win.command_preview.get_command())
        self.assertNotIn("secret-password", self.win.command_preview.get_command())
        self.assertIn("-NoLogo", self.win.command_preview.get_command())

        self._switch(self.win.powershell_tab)
        self.win.powershell_tab.nologo_checkbox.setChecked(False)
        QApplication.processEvents()
        hidden_preview = self.win.command_preview.get_command()
        self.assertNotIn("-NoLogo", hidden_preview)
        self._switch(self.win.psexec_tab)
        self.assertEqual(self.win.command_preview.get_command(), hidden_preview)
        self.assertNotIn("secret-password", self.win.command_preview.get_command())
        self.assertNotIn("-NoLogo", self.win.command_preview.get_command())

    def test_cmd_and_powershell_preselect_system_flag(self):
        self.assertFalse(self.win.psexec_tab.flag_s.isChecked())
        self.win.psexec_tab.remote_cmd_edit.setText("powershell")
        QApplication.processEvents()
        self.assertTrue(self.win.psexec_tab.flag_s.isChecked())
        self.win.psexec_tab.flag_s.setChecked(False)
        QApplication.processEvents()
        self.assertFalse(self.win.psexec_tab.flag_s.isChecked())
        self.win.psexec_tab.remote_cmd_edit.setText("powershell.exe")
        QApplication.processEvents()
        self.assertFalse(self.win.psexec_tab.flag_s.isChecked())
        self.win.psexec_tab.remote_cmd_edit.clear()
        QApplication.processEvents()
        self.win.psexec_tab.remote_cmd_edit.setText("cmd")
        QApplication.processEvents()
        self.assertTrue(self.win.psexec_tab.flag_s.isChecked())
        self.assertIn("-s", self.win.command_preview.get_command())

    def test_stop_prefers_exit_over_kill(self):
        with patch.object(self.win.executor, "send_input", return_value=True) as send:
            with patch.object(self.win.executor, "stop") as stop:
                self.win.on_stop()
        send.assert_called_once_with("exit")
        stop.assert_not_called()
        self.assertTrue(self.win._session_exit_requested)

        with patch.object(self.win.executor, "send_input", return_value=True):
            with patch.object(self.win.executor, "stop") as stop:
                self.win.on_stop()
        stop.assert_called_once()

    def test_stop_kills_when_there_is_no_session(self):
        with patch.object(self.win.executor, "send_input", return_value=False):
            with patch.object(self.win.executor, "stop") as stop:
                self.win.on_stop()
        stop.assert_called_once()

    def test_fullscreen_tabs_still_hide_shared_widgets(self):
        self.win.open_settings_tab()
        QApplication.processEvents()
        self.assertEqual(self.win._workspace_mode(), "fullscreen")
        self.assertFalse(self.win.command_preview.isVisible())
        self.assertFalse(self.win.log_output.isVisible())
        self.assertIs(self.win.tabs.currentWidget(), self.win.settings_tab)

        self._switch(self.win.psexec_tab)
        self.assertTrue(self.win.command_preview.isVisible())
        self.assertTrue(self.win.log_output.isVisible())


if __name__ == "__main__":
    unittest.main()
