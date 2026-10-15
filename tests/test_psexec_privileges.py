"""Privilégio exclusivo e descoberta de sessões da aba PsExec, sem rede real."""

from __future__ import annotations

import sys
import threading
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from remoteops.core.auth_identity import AuthKind
from remoteops.core.builder import CommandBuilder
from remoteops.core.psexec_options import (
    PsExecOptions,
    build_psexec_option_argv,
    sanitize_psexec_options,
    validate_psexec_options,
)
from remoteops.ui.tabs.psexec import _SessionListWorker
from remoteops.utils.domain import HostUserDomain
from remoteops.utils.host_reachability import HostAccessKind, HostReachability
from remoteops.utils.sessions import RemoteSession

SECRET = "segredo-um"


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


class PrivilegeOptionTests(unittest.TestCase):
    def test_flags_stay_exclusive_and_match_argv(self) -> None:
        default = build_psexec_option_argv(PsExecOptions())
        self.assertNotIn("-h", default)
        self.assertNotIn("-s", default)
        self.assertNotIn("-l", default)

        for flag, attr in (("-h", "flag_h"), ("-s", "flag_s"), ("-l", "flag_l")):
            opts = PsExecOptions(**{attr: True})
            argv = build_psexec_option_argv(opts)
            self.assertIn(flag, argv)
            for other in ("-h", "-s", "-l"):
                if other != flag:
                    self.assertNotIn(other, argv)

        mixed = PsExecOptions(flag_h=True, flag_s=True, flag_l=True, flag_e=True)
        clean = sanitize_psexec_options(mixed)
        self.assertTrue(clean.flag_s)
        self.assertFalse(clean.flag_h)
        self.assertFalse(clean.flag_l)
        self.assertFalse(clean.flag_e)
        argv = build_psexec_option_argv(mixed)
        self.assertIn("-s", argv)
        self.assertNotIn("-h", argv)
        self.assertNotIn("-l", argv)
        self.assertNotIn("-e", argv)

        dropped = sanitize_psexec_options(
            PsExecOptions(flag_s=True, flag_e=True),
            trigger="-e",
        )
        self.assertTrue(dropped.flag_e)
        self.assertFalse(dropped.flag_s)

    def test_interactive_argv_and_blocked_session(self) -> None:
        off = build_psexec_option_argv(PsExecOptions(session_id=4))
        self.assertNotIn("-i", off)

        plain = build_psexec_option_argv(PsExecOptions(session_interactive=True))
        self.assertEqual(plain[plain.index("-i") + 1 : plain.index("-i") + 2], [])
        self.assertIn("-i", plain)

        chosen = build_psexec_option_argv(
            PsExecOptions(session_interactive=True, session_id=4)
        )
        self.assertEqual(chosen[chosen.index("-i") + 1], "4")

        blocked = PsExecOptions(
            session_interactive=True,
            session_id=4,
            session_unavailable=True,
            session_block_reason="A sessão selecionada não está mais disponível.",
        )
        self.assertTrue(validate_psexec_options(blocked))
        self.assertNotIn("-i", build_psexec_option_argv(blocked))


class PsExecPrivilegeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.ui.tabs.psexec import PsExecTab

        self.log_lines: list[str] = []

        class _Log:
            def append_log(inner, text: str) -> None:  # noqa: N805
                self.log_lines.append(text)

        self._domain_patch = patch(
            "remoteops.ui.tabs.psexec.lookup_host_userdomain",
            return_value=HostUserDomain(),
        )
        self._session_domain_patch = patch(
            "remoteops.ui.tabs.psexec.local_logon_domain",
            return_value=HostUserDomain(),
        )
        self._probe_patch = patch(
            "remoteops.ui.tabs.psexec.probe_host_reachability",
            side_effect=_quiet_probe,
        )
        self._list_patch = patch(
            "remoteops.ui.tabs.psexec.list_remote_sessions",
            return_value=([], ""),
        )
        self._domain_patch.start()
        self._session_domain_patch.start()
        self._probe_patch.start()
        self.list_sessions = self._list_patch.start()
        self.tab = PsExecTab(log_output=_Log())
        self.tab.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.tab.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.tab._host_status_timer.stop()
        self.tab._session_refresh_timer.stop()
        self.tab.shutdown(wait_ms=2000)
        self.tab.close()
        self._list_patch.stop()
        self._probe_patch.stop()
        self._session_domain_patch.stop()
        self._domain_patch.stop()
        self.tab.deleteLater()
        QApplication.processEvents()

    def _spec(self):
        builder = CommandBuilder()
        builder.set_psexec_params(
            self.tab.collect_builder_params(
                host="PC01",
                psexec_path="",
                remote_cmd="hostname",
            )
        )
        return builder.build_psexec_spec()

    def _texts(self) -> list[str]:
        combo = self.tab.session_combo
        return [combo.itemText(i) for i in range(combo.count())]

    def _arm(self, host: str = "PC01") -> None:
        self.tab._host_status_timer.stop()
        self.tab.host_edit.setText(host)
        self.tab._host_status_timer.stop()
        self.tab._host_online = True
        if not self.tab.session_interactive.isChecked():
            self.tab.session_interactive.setChecked(True)
        self.tab._session_refresh_timer.stop()
        self.tab._update_session_refresh_enabled()
        QApplication.processEvents()

    def _deliver(self, sessions, error: str = "", *, generation: int | None = None, host: str = "") -> None:
        current = host or self.tab.host_edit.text()
        gen = self.tab._session_generation if generation is None else generation
        self.tab._on_sessions_result(gen, current, sessions, error)
        QApplication.processEvents()

    def _select(self, session_id: int, username: str) -> None:
        combo = self.tab.session_combo
        for index in range(combo.count()):
            data = combo.itemData(index)
            if (
                isinstance(data, tuple)
                and data
                and data[0] == "session"
                and int(data[1]) == session_id
                and data[2] == username
            ):
                combo.setCurrentIndex(index)
                return
        self.fail(f"sessão {session_id} {username} não apareceu")

    def _wait_idle(self, seconds: float = 5.0) -> None:
        loops = int(seconds / 0.02)
        for _ in range(loops):
            QApplication.processEvents()
            if self.tab._session_worker is None and not self.tab._session_querying:
                if not self.tab._session_pending:
                    return
            QTest.qWait(20)
        self.fail("a consulta de sessões não terminou")

    def test_segment_matches_preview_and_is_exclusive(self) -> None:
        self.assertFalse(self.tab.flag_h.isVisible())
        self.assertFalse(self.tab.flag_s.isVisible())
        self.assertFalse(self.tab.flag_l.isVisible())
        self.assertEqual(self.tab.privilege_bar.level(), "")
        self.assertEqual(
            self.tab.privilege_caption.text(),
            "Executar sem flags adicionais de privilégio.",
        )
        argv = self._spec().argv
        self.assertNotIn("-h", argv)
        self.assertNotIn("-s", argv)
        self.assertNotIn("-l", argv)

        expected = {
            "-h": "Solicita o token elevado da conta, quando disponível.",
            "-s": "Executar como NT AUTHORITY\\SYSTEM no computador remoto.",
            "-l": "Executar com privilégios reduzidos.",
        }
        for level, caption in expected.items():
            self.tab.privilege_bar.button(level).click()
            QApplication.processEvents()
            self.assertEqual(self.tab.privilege_bar.level(), level)
            self.assertEqual(self.tab.privilege_caption.text(), caption)
            argv = self._spec().argv
            self.assertIn(level, argv)
            for other in expected:
                if other != level:
                    self.assertNotIn(other, argv)
            self.assertEqual(
                sum(1 for flag in ("flag_h", "flag_s", "flag_l") if getattr(self.tab, flag).isChecked()),
                1,
            )

        self.tab.privilege_bar.button("").click()
        QApplication.processEvents()
        argv = self._spec().argv
        self.assertNotIn("-h", argv)
        self.assertNotIn("-s", argv)
        self.assertNotIn("-l", argv)

    def test_system_turns_off_profile_flag_and_the_reverse(self) -> None:
        self.tab.flag_e.setChecked(True)
        self.tab.privilege_bar.button("-s").click()
        QApplication.processEvents()
        self.assertTrue(self.tab.flag_s.isChecked())
        self.assertFalse(self.tab.flag_e.isChecked())
        self.assertIn("-s", self._spec().argv)
        self.assertNotIn("-e", self._spec().argv)

        self.tab.flag_e.setChecked(True)
        QApplication.processEvents()
        self.assertTrue(self.tab.flag_e.isChecked())
        self.assertFalse(self.tab.flag_s.isChecked())
        self.assertEqual(self.tab.privilege_bar.level(), "")
        self.assertNotIn("-s", self._spec().argv)

    def test_shell_and_exe_preselect_system_once(self) -> None:
        self.tab.apply_shell_command_defaults("cmd")
        self.assertEqual(self.tab.privilege_bar.level(), "-s")
        self.assertIn("-s", self._spec().argv)
        self.tab.privilege_bar.button("").click()
        QApplication.processEvents()
        self.tab.apply_shell_command_defaults("powershell.exe")
        self.assertEqual(self.tab.privilege_bar.level(), "")

        self.tab.preselect_exe_options()
        self.assertEqual(self.tab.privilege_bar.level(), "-s")
        self.assertIn("-s", self._spec().argv)

    def test_reset_restores_default_privilege_and_console(self) -> None:
        self._arm()
        self.tab.privilege_bar.button("-l").click()
        QApplication.processEvents()
        self.tab._reset_card_privilegios()
        QApplication.processEvents()
        self.assertEqual(self.tab.privilege_bar.level(), "")
        self.assertFalse(self.tab.session_interactive.isChecked())
        self.assertIsNone(self.tab.session_combo.currentData())
        self.assertEqual(self.tab.session_combo.itemText(0), "Console padrão (sem ID)")
        argv = self._spec().argv
        self.assertNotIn("-l", argv)
        self.assertNotIn("-i", argv)

    def test_interactive_without_id_and_with_id(self) -> None:
        argv = self._spec().argv
        self.assertNotIn("-i", argv)
        self.assertFalse(self.tab.session_combo.isEnabled())
        self.assertFalse(self.tab.session_refresh_btn.isEnabled())

        self._arm()
        self.assertTrue(self.tab.session_combo.isEnabled())
        self.assertTrue(self.tab.session_refresh_btn.isEnabled())
        argv = self._spec().argv
        index = argv.index("-i")
        self.assertFalse(argv[index + 1].isdigit())

        session = RemoteSession(1, "console", "usuario01", "Ativa")
        self._deliver([session, RemoteSession(2, "rdp", "usuario02", "Desconectada")])
        self.assertEqual(self.tab.session_combo.currentIndex(), 0)
        self.assertIn("1 — usuario01 · Ativa", self._texts())
        self.assertIn("2 — usuario02 · Desconectada", self._texts())
        self._select(1, "usuario01")
        argv = self._spec().argv
        self.assertEqual(argv[argv.index("-i") + 1], "1")

    def test_query_messages_and_manual_refresh(self) -> None:
        self._arm()
        self.assertTrue(self.tab.session_refresh_btn.isEnabled())
        self.list_sessions.reset_mock()
        self.tab.session_refresh_btn.click()
        self._wait_idle()
        self.assertEqual(self.list_sessions.call_count, 1)
        self.assertEqual(self.tab.session_status.text(), "Nenhuma sessão encontrada.")

        self._deliver([], "Access is denied")
        self.assertEqual(self.tab.session_status.text(), "Acesso negado.")
        self.assertNotIn("Falha ao consultar", " ".join(self._texts()))

        self._deliver([], "Command timed out after 8 seconds")
        self.assertIn("Tempo esgotado", self.tab.session_status.text())

        self._deliver([], "WinError 53")
        self.assertEqual(self.tab.session_status.text(), "Computador indisponível.")

        self._deliver([], "erro secret-token inesperado")
        self.assertEqual(self.tab.session_status.text(), "Falha inesperada ao consultar as sessões.")
        self.assertNotIn("secret-token", self.tab.session_status.text())
        self.assertTrue(all("secret-token" not in line for line in self.log_lines))

        self.tab.session_interactive.setChecked(False)
        self.tab._session_refresh_timer.stop()
        self.assertFalse(self.tab.session_refresh_btn.isEnabled())
        self.tab.host_edit.clear()
        self.tab._host_status_timer.stop()
        self.tab.session_interactive.setChecked(True)
        self.tab._session_refresh_timer.stop()
        self.assertFalse(self.tab.session_refresh_btn.isEnabled())

    def test_preserves_same_session_and_invalidates_a_different_one(self) -> None:
        self._arm()
        self._deliver(
            [
                RemoteSession(1, "console", "usuario01", "Ativa"),
                RemoteSession(2, "rdp", "usuario02", "Desconectada"),
            ]
        )
        self._select(2, "usuario02")
        self._deliver(
            [
                RemoteSession(2, "rdp", "usuario02", "Ativa"),
                RemoteSession(3, "rdp", "usuario03", "Ativa"),
            ]
        )
        data = self.tab.session_combo.currentData()
        self.assertEqual(data, ("session", 2, "usuario02"))
        self.assertEqual(self._spec().argv[self._spec().argv.index("-i") + 1], "2")

        self._deliver([RemoteSession(2, "rdp", "outra", "Ativa")])
        self.assertEqual(self.tab.session_combo.currentData()[0], "missing")
        self.assertIn("não está mais disponível", self.tab.session_status.text())
        spec = self._spec()
        self.assertTrue(spec.display_command.startswith("# Erro PsExec:"))
        self.assertIn("não está mais disponível", spec.display_command)
        self.assertNotIn("-i", spec.args)

        self._deliver([RemoteSession(1, "console", "usuario01", "Ativa")])
        self._select(1, "usuario01")
        self._deliver([])
        self.assertEqual(self.tab.session_combo.currentData()[0], "missing")
        self.assertNotIn("-i", self._spec().args)

    def test_stale_results_do_not_replace_the_current_list(self) -> None:
        self._arm("PC01")
        current = self.tab._session_generation
        self._deliver([RemoteSession(4, "console", "novo", "Ativa")], generation=current)
        self._deliver([RemoteSession(9, "console", "velho", "Ativa")], generation=current - 1)
        self.assertTrue(any("novo" in text for text in self._texts()))
        self.assertFalse(any("velho" in text for text in self._texts()))

        gen = self.tab._session_generation
        self.tab.host_edit.setText("PC02")
        self.tab._host_status_timer.stop()
        self._deliver(
            [RemoteSession(4, "console", "novo", "Ativa")],
            generation=gen,
            host="PC01",
        )
        self.assertFalse(any("novo" in text for text in self._texts()))
        self.assertIsNone(self.tab.session_combo.currentData())

    def test_host_change_during_query_drops_the_old_result(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def listing(host, user="", password=""):
            started.set()
            release.wait(5)
            return [RemoteSession(9, "console", "antigo", "Ativa")], ""

        self.list_sessions.side_effect = listing
        self._arm("PC01")
        self.tab._refresh_remote_sessions(manual=True)
        self.assertTrue(started.wait(2))
        self.assertIn("Consultando", self.tab.session_status.text())
        self.assertFalse(self.tab.session_refresh_btn.isEnabled())
        self.tab.host_edit.setText("PC02")
        self.tab._host_status_timer.stop()
        release.set()
        self._wait_idle()
        self.assertFalse(any("antigo" in text for text in self._texts()))
        self.assertIsNone(self.tab._session_worker)

    def test_credential_change_during_query_uses_only_the_new_result(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[tuple[str, str]] = []

        def listing(host, user="", password=""):
            calls.append((user, password))
            if len(calls) == 1:
                started.set()
                release.wait(5)
                return [RemoteSession(1, "console", "primeiro", "Ativa")], ""
            return [RemoteSession(2, "console", "segundo", "Ativa")], ""

        self.list_sessions.side_effect = listing
        self.tab._select_kind(AuthKind.DOMAIN, clear_account=False)
        self.tab.domain_edit.setText("EMPRESA")
        self.tab.user_edit.setText("ana")
        self._arm("PC01")
        self.tab._session_refresh_timer.stop()
        self.tab._refresh_remote_sessions(manual=True)
        self.assertTrue(started.wait(2))
        self.tab.user_edit.setText("bia")
        self.tab._session_refresh_timer.stop()
        release.set()
        self._wait_idle()
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0][0], r"EMPRESA\ana")
        self.assertEqual(calls[-1][0], r"EMPRESA\bia")
        joined = " ".join(self._texts())
        self.assertNotIn("primeiro", joined)
        self.assertIn("segundo", joined)
        self.assertEqual(self.tab.session_combo.currentIndex(), 0)

    def test_password_change_blocks_the_old_session_and_does_not_leak(self) -> None:
        self.tab._select_kind(AuthKind.DOMAIN, clear_account=False)
        self.tab.domain_edit.setText("EMPRESA")
        self.tab.user_edit.setText("ana")
        self._arm()
        self._deliver([RemoteSession(3, "console", "ana", "Ativa")])
        self._select(3, "ana")
        self.tab.pass_edit.setText(SECRET)
        self.tab._session_refresh_timer.stop()
        spec = self._spec()
        self.assertTrue(spec.display_command.startswith("# Erro PsExec:"))
        self.assertNotIn("-i", spec.args)
        self.assertNotIn(SECRET, spec.display_command)
        context = " ".join(str(part) for part in (self.tab._session_context or ()))
        self.assertNotIn(SECRET, context)
        self.assertNotIn(SECRET, self.tab.session_status.text())

        worker = _SessionListWorker(1, "PC01", user="ana", password=SECRET)
        worker.run()
        self.assertEqual(worker._password, "")
        self.assertEqual(worker._user, "")

    def test_shutdown_during_query_ignores_a_late_result(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def listing(host, user="", password=""):
            started.set()
            release.wait(5)
            return [RemoteSession(8, "console", "tardio", "Ativa")], ""

        self.list_sessions.side_effect = listing
        self._arm()
        generation = self.tab._session_generation
        self.tab._refresh_remote_sessions(manual=True)
        self.assertTrue(started.wait(2))
        release.set()
        self.tab.shutdown(wait_ms=3000)
        QApplication.processEvents()
        self.tab._on_sessions_result(
            generation,
            "PC01",
            [RemoteSession(8, "console", "tardio", "Ativa")],
            "",
        )
        self.assertFalse(any("tardio" in text for text in self._texts()))
        self.assertIsNone(self.tab._session_worker)
