"""Tipo de credencial da aba PsExec: identidade, UI e lançamento sem rede."""

from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit

from remoteops.core.auth_identity import (
    AuthKind,
    compose_psexec_identity,
    normalize_domain_label,
)
from remoteops.core.builder import CommandBuilder
from remoteops.core.models import CommandSpec
from remoteops.services.ops import (
    CommandExecutionService,
    CredentialContext,
    build_psexec_argv,
    materialize_password_in_argv,
)
from remoteops.utils.domain import HostUserDomain, local_logon_domain
from remoteops.utils.redaction import REDACTED, redact_command_text
from remoteops.winget.psexec_args import build_psexec_args


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


SECRET = 'p@ss w0rd!"\' &'


class LocalLogonDomainTests(unittest.TestCase):
    def test_domain_account_is_detected(self) -> None:
        with patch.dict(
            "os.environ",
            {"USERDOMAIN": "EMPRESA", "COMPUTERNAME": "PC-ADMIN", "USERDNSDOMAIN": "empresa.local"},
            clear=False,
        ):
            info = local_logon_domain()
        self.assertEqual(info.name, "EMPRESA")
        self.assertFalse(info.is_workgroup)
        self.assertEqual(info.source, "session")

    def test_local_account_is_not_a_domain(self) -> None:
        with patch.dict(
            "os.environ",
            {"USERDOMAIN": "PC-ADMIN", "COMPUTERNAME": "PC-ADMIN", "USERDNSDOMAIN": ""},
            clear=False,
        ):
            info = local_logon_domain()
        self.assertEqual(info.name, "")


class AuthIdentityTests(unittest.TestCase):
    def test_windows_omits_identity(self) -> None:
        self.assertEqual(compose_psexec_identity(AuthKind.WINDOWS, "joao", "CONTOSO"), "")
        self.assertEqual(compose_psexec_identity("windows", r".\joao", "PC01"), "")

    def test_domain_identity(self) -> None:
        self.assertEqual(
            compose_psexec_identity(AuthKind.DOMAIN, "joao", "CONTOSO"),
            r"CONTOSO\joao",
        )
        self.assertEqual(compose_psexec_identity(AuthKind.DOMAIN, "joao", ""), "joao")
        self.assertEqual(compose_psexec_identity(AuthKind.DOMAIN, "  ", "CONTOSO"), "")
        self.assertEqual(
            compose_psexec_identity(AuthKind.DOMAIN, "ana@contoso.com", "CONTOSO"),
            "ana@contoso.com",
        )

    def test_local_identity_is_dot_prefix_not_the_computer(self) -> None:
        self.assertEqual(
            compose_psexec_identity(AuthKind.LOCAL, "joao", "CONTOSO"),
            r".\joao",
        )
        self.assertEqual(
            compose_psexec_identity(AuthKind.LOCAL, r"CONTOSO\joao", "PC01"),
            r".\joao",
        )
        self.assertEqual(
            compose_psexec_identity(AuthKind.LOCAL, r".\Joao Silva", ""),
            r".\Joao Silva",
        )
        self.assertEqual(compose_psexec_identity(AuthKind.LOCAL, "", "PC01"), "")

    def test_domain_label_drops_account_and_slashes(self) -> None:
        self.assertEqual(normalize_domain_label(r" CONTOSO\joao "), "CONTOSO")
        self.assertEqual(normalize_domain_label("\\\\"), "")


class PsExecAuthUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.ui.tabs.psexec import PsExecTab

        self._domain_patch = patch(
            "remoteops.ui.tabs.psexec.lookup_host_userdomain",
            return_value=HostUserDomain(),
        )
        self._session_patch = patch(
            "remoteops.ui.tabs.psexec.local_logon_domain",
            return_value=HostUserDomain(),
        )
        self._probe_patch = patch(
            "remoteops.ui.tabs.psexec.probe_host_reachability",
            side_effect=AssertionError("probe de rede não deveria rodar"),
        )
        self._domain_patch.start()
        self._session_patch.start()
        self._probe_patch.start()
        self.tab = PsExecTab()
        self.tab.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.tab.auth_card.set_collapsed(False)
        self.tab.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.tab.shutdown(wait_ms=200)
        self.tab.close()
        self._probe_patch.stop()
        self._session_patch.stop()
        self._domain_patch.stop()
        self.tab.deleteLater()
        QApplication.processEvents()

    def _stop_host_timer(self) -> None:
        self.tab._host_status_timer.stop()

    def _params(self) -> dict:
        return self.tab.collect_builder_params(
            host="PC01",
            psexec_path="",
            remote_cmd="hostname",
        )

    def _spec(self) -> CommandSpec:
        builder = CommandBuilder()
        builder.set_psexec_params(self._params())
        return builder.build_psexec_spec()

    def test_starts_on_windows_and_toggles_fields(self) -> None:
        self.assertEqual(self.tab.auth_kind, AuthKind.WINDOWS)
        self.assertTrue(self.kind_checked(AuthKind.WINDOWS))
        self.assertIs(self.tab.auth_stack.currentWidget(), self.tab.auth_notice)
        self.assertTrue(self.tab.auth_notice.isVisible())
        self.assertFalse(self.tab.auth_fields.isVisible())
        texts = [label.text() for label in self.tab.auth_notice.findChildren(QLabel)]
        self.assertTrue(any("Credenciais da sessão Windows" in text for text in texts))
        self.assertTrue(any("contexto de segurança atual" in text for text in texts))
        self.assertEqual(self.tab.auth_username(), "")
        self.assertEqual(self.tab.auth_password(), "")

        self.tab.kind_domain.setChecked(True)
        self.assertEqual(self.tab.auth_kind, AuthKind.DOMAIN)
        self.assertTrue(self.tab.auth_fields.isVisible())
        self.assertFalse(self.tab.auth_notice.isVisible())
        self.assertTrue(self.tab.domain_edit.isVisible())
        self.assertFalse(self.tab._local_prefix.isVisible())
        self.assertFalse(self.tab.pass_edit.isEnabled())

        self.tab.kind_local.setChecked(True)
        self.assertEqual(self.tab.auth_kind, AuthKind.LOCAL)
        self.assertTrue(self.tab._local_prefix.isVisible())
        self.assertEqual(self.tab._local_prefix.text(), ".\\")
        self.assertFalse(self.tab.domain_edit.isVisible())
        self.assertIn("Conta local do computador remoto", self.tab._user_caption.text())

        self.tab.kind_windows.setChecked(True)
        self.assertFalse(self.tab.auth_fields.isVisible())

    def kind_checked(self, kind: AuthKind) -> bool:
        return self.tab.auth_kind_bar.button(kind).isChecked()

    def test_domain_mode_fills_session_domain_then_remote_domain(self) -> None:
        self._session_patch.stop()
        session = HostUserDomain(name="EMPRESA", dns="empresa.local", source="session")
        self._session_patch = patch(
            "remoteops.ui.tabs.psexec.local_logon_domain",
            return_value=session,
        )
        self._session_patch.start()
        self.tab.kind_domain.setChecked(True)
        self.assertEqual(self.tab.domain_edit.text(), "EMPRESA")
        self.assertFalse(self.tab._domain_manual)
        self.assertIn("automaticamente", self.tab._user_caption.text())
        self.tab.user_edit.setText("joao")
        self.assertEqual(self.tab.auth_username(), r"EMPRESA\joao")

        self.tab.host_edit.setText("PC01")
        self._stop_host_timer()
        self.tab._domain_wanted = "PC01"
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="CONTOSO", is_workgroup=False, source="netapi"),
        )
        self.assertEqual(self.tab.domain_edit.text(), "CONTOSO")
        self.assertEqual(self.tab.auth_username(), r"CONTOSO\joao")
        self.assertEqual(self.tab.auth_kind, AuthKind.DOMAIN)

        self.tab.domain_edit.setText("FABRICA")
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="CONTOSO", is_workgroup=False, source="netapi"),
        )
        self.assertEqual(self.tab.domain_edit.text(), "FABRICA")

    def test_domain_composition_discovery_and_manual_prefix(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.host_edit.setText("PC01")
        self._stop_host_timer()
        self.tab._domain_wanted = "PC01"
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="CONTOSO", is_workgroup=False, source="netapi"),
        )
        self.assertEqual(self.tab.auth_kind, AuthKind.DOMAIN)
        self.assertEqual(self.tab.domain_edit.text(), "CONTOSO")
        self.assertIn("automaticamente", self.tab._user_caption.text())
        self.assertEqual(self.tab.auth_username(), "")

        self.tab.user_edit.setText("joao")
        self.assertEqual(self.tab.auth_username(), r"CONTOSO\joao")
        self.assertTrue(self.tab.pass_edit.isEnabled())

        self.tab.domain_edit.setText("FABRICA")
        self.assertIn("manualmente", self.tab._user_caption.text())
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="CONTOSO", is_workgroup=False, source="netapi"),
        )
        self.assertEqual(self.tab.domain_edit.text(), "FABRICA")
        self.assertEqual(self.tab.auth_username(), r"FABRICA\joao")
        self.assertEqual(self.tab.auth_kind, AuthKind.DOMAIN)

    def test_stale_discovery_does_not_change_mode_or_manual_domain(self) -> None:
        self.tab.kind_windows.setChecked(True)
        self.tab.host_edit.setText("PC02")
        self._stop_host_timer()
        self.tab._domain_wanted = "PC02"
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="EVIL", is_workgroup=False),
        )
        self.assertEqual(self.tab.auth_kind, AuthKind.WINDOWS)
        self.assertEqual(self.tab.domain_edit.text(), "")
        self.assertEqual(self.tab.auth_username(), "")

    def test_discovery_while_windows_does_not_switch_mode(self) -> None:
        self.tab.host_edit.setText("PC01")
        self._stop_host_timer()
        self.tab._domain_wanted = "PC01"
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="CONTOSO", is_workgroup=False),
        )
        self.assertEqual(self.tab.auth_kind, AuthKind.WINDOWS)
        self.assertEqual(self.tab.auth_username(), "")
        self.assertFalse(self.tab.auth_fields.isVisible())
        self.tab.kind_domain.setChecked(True)
        self.assertEqual(self.tab.domain_edit.text(), "CONTOSO")
        self.assertEqual(self.tab.user_edit.text(), "")
        self.assertEqual(self.tab.auth_password(), "")

    def test_hostname_hint_and_workgroup_are_not_a_computer_account(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.host_edit.setText("pc.contoso.local")
        self._stop_host_timer()
        self.assertEqual(self.tab.domain_edit.text(), "CONTOSO")
        self.assertFalse(self.tab._domain_manual)

        self.tab.host_edit.setText("PC01")
        self._stop_host_timer()
        self.assertEqual(self.tab.domain_edit.text(), "")
        self.tab._domain_wanted = "PC01"
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="PC01", computer="PC01", is_workgroup=True, source="netapi"),
        )
        self.tab.user_edit.setText("joao")
        self.assertEqual(self.tab.auth_username(), "joao")
        self.assertNotIn("PC01", self.tab.auth_username())

    def test_manual_domain_survives_host_change(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.domain_edit.setText("FABRICA")
        self.tab.host_edit.setText("outro.contoso.local")
        self._stop_host_timer()
        self.assertEqual(self.tab.domain_edit.text(), "FABRICA")
        self.assertTrue(self.tab._domain_manual)

    def test_local_account_ignores_discovered_domain(self) -> None:
        self.tab.kind_local.setChecked(True)
        self.tab.user_edit.setText("joao")
        self.tab.host_edit.setText("PC01")
        self._stop_host_timer()
        self.tab._domain_wanted = "PC01"
        self.tab._on_host_domain_result(
            "PC01",
            HostUserDomain(name="CONTOSO", is_workgroup=False),
        )
        self.assertEqual(self.tab.auth_kind, AuthKind.LOCAL)
        self.assertEqual(self.tab.auth_username(), r".\joao")
        self.assertNotIn("CONTOSO", self.tab.auth_username())

    def test_paste_switches_kind_without_dropping_the_account(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.pass_edit.setText("segredo")
        self.tab.user_edit.setText(r".\ana")
        self.assertEqual(self.tab.auth_kind, AuthKind.LOCAL)
        self.assertEqual(self.tab.user_edit.text(), "ana")
        self.assertEqual(self.tab.auth_username(), r".\ana")
        self.assertEqual(self.tab.auth_password(), "")

        self.tab.user_edit.setText(r"OUTRO\bia")
        self.assertEqual(self.tab.auth_kind, AuthKind.DOMAIN)
        self.assertEqual(self.tab.domain_edit.text(), "OUTRO")
        self.assertEqual(self.tab.user_edit.text(), "bia")
        self.assertEqual(self.tab.auth_username(), r"OUTRO\bia")
        self.assertTrue(self.tab._domain_manual)

    def test_mode_switch_and_reset_clear_credentials(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.domain_edit.setText("CONTOSO")
        self.tab.user_edit.setText("joao")
        self.tab.pass_edit.setText(SECRET)
        self.tab.pass_reveal.click()
        self.assertEqual(self.tab.pass_edit.echoMode(), QLineEdit.EchoMode.Normal)

        self.tab.kind_local.setChecked(True)
        self.assertEqual(self.tab.user_edit.text(), "")
        self.assertEqual(self.tab.pass_edit.text(), "")
        self.assertEqual(self.tab.auth_username(), "")
        self.assertEqual(self.tab.auth_password(), "")
        self.assertEqual(self.tab.pass_edit.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(self.tab.domain_edit.text(), "CONTOSO")

        self.tab.kind_domain.setChecked(True)
        self.tab.user_edit.setText("joao")
        self.tab.pass_edit.setText(SECRET)
        self.tab._reset_card_autenticacao()
        self.assertEqual(self.tab.auth_kind, AuthKind.WINDOWS)
        self.assertEqual(self.tab.user_edit.text(), "")
        self.assertEqual(self.tab.pass_edit.text(), "")
        self.assertFalse(self.tab._domain_manual)
        self.assertEqual(self.tab.auth_password(), "")

    def test_password_mask_special_characters_and_preview(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.domain_edit.setText("CONTOSO")
        self.assertFalse(self.tab.pass_edit.isEnabled())
        self.tab.user_edit.setText("joao")
        self.tab.pass_edit.setText(SECRET)
        self.assertEqual(self.tab.pass_edit.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(self.tab.auth_password(), SECRET)
        self.assertNotIn(SECRET, self.tab.pass_edit.toolTip())
        self.assertNotIn(SECRET, self.tab.pass_reveal.toolTip())

        self.tab.pass_reveal.click()
        self.assertEqual(self.tab.pass_edit.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(self.tab.pass_reveal.toolTip(), "Ocultar senha")
        self.assertNotIn(SECRET, self.tab.pass_reveal.toolTip())
        self.assertNotIn(SECRET, self.tab.pass_reveal.accessibleName())
        self.tab.pass_reveal.click()
        self.assertEqual(self.tab.pass_edit.echoMode(), QLineEdit.EchoMode.Password)

        params = self._params()
        self.assertEqual(params["user"], r"CONTOSO\joao")
        self.assertTrue(params["has_password"])
        self.assertNotIn("password", params)
        self.assertNotIn(SECRET, json.dumps(params))

        spec = self._spec()
        self.assertIn(r"CONTOSO\joao", spec.argv)
        self.assertIn("-u", spec.argv)
        self.assertIn("-p", spec.argv)
        self.assertNotIn(SECRET, spec.argv)
        self.assertEqual(spec.argv[spec.argv.index("-p") + 1], REDACTED)
        self.assertNotIn(SECRET, spec.display_command)
        self.assertNotIn(SECRET, spec.sanitized_display([SECRET]))
        self.assertIn(REDACTED, spec.display_command)

        self.tab.pass_edit.clear()
        interactive = self._spec()
        self.assertIn("-u", interactive.argv)
        self.assertNotIn("-p", interactive.argv)

        spaced = "  seg redo  "
        self.tab.pass_edit.setText(spaced)
        materialized = materialize_password_in_argv(self._spec().argv, self.tab.auth_password())
        self.assertEqual(materialized[materialized.index("-p") + 1], spaced)

    def test_windows_preview_has_no_credentials_even_if_the_field_is_dirty(self) -> None:
        self.tab.pass_edit.setText(SECRET)
        self.tab.user_edit.setText("joao")
        self.assertEqual(self.tab.auth_username(), "")
        self.assertEqual(self.tab.auth_password(), "")
        spec = self._spec()
        self.assertNotIn("-u", spec.argv)
        self.assertNotIn("-p", spec.argv)
        self.assertNotIn(SECRET, spec.display_command)
        self.assertNotIn(SECRET, json.dumps(self._params()))

    def test_arrow_keys_move_kind_and_preview_signal(self) -> None:
        seen: list[int] = []
        self.tab.authChanged.connect(lambda: seen.append(1))
        self.tab.auth_kind_bar.move_kind(1)
        self.assertEqual(self.tab.auth_kind, AuthKind.DOMAIN)
        self.assertTrue(seen)
        seen.clear()
        self.tab.domain_edit.setText("CONTOSO")
        self.assertTrue(seen)
        self.tab.auth_kind_bar.move_kind(-1)
        self.assertEqual(self.tab.auth_kind, AuthKind.WINDOWS)

    def test_launch_paths_keep_one_argv_and_redact_logs(self) -> None:
        self.tab.kind_local.setChecked(True)
        self.tab.user_edit.setText("joao")
        self.tab.pass_edit.setText(SECRET)
        spec = self._spec()
        self.assertEqual(spec.argv[spec.argv.index("-u") + 1], r".\joao")

        class _Exec:
            def __init__(self) -> None:
                self.calls = []

            def run(self, command, passwords=None, use_conpty=False):
                self.calls.append(
                    {
                        "argv": list(command.argv),
                        "display": command.display_command,
                        "passwords": list(passwords or []),
                        "use_conpty": use_conpty,
                    }
                )

        logs: list[str] = []
        executor = _Exec()
        service = CommandExecutionService(executor, log_fn=logs.append)
        launched = service._launch_psexec_conpty(spec, password=SECRET)
        self.assertTrue(launched.ok)
        self.assertNotIn(SECRET, launched.message)
        self.assertNotIn(SECRET, launched.display_command)
        call = executor.calls[0]
        self.assertTrue(call["use_conpty"])
        self.assertEqual(call["argv"][call["argv"].index("-p") + 1], SECRET)
        self.assertEqual(call["argv"].count(SECRET), 1)
        self.assertNotIn(SECRET, call["display"])
        self.assertNotIn(SECRET, "\n".join(logs))

        captured: dict[str, list[str]] = {}

        def _open(argv, **_kwargs):
            captured["argv"] = list(argv)
            return object()

        with patch("remoteops.services.ops.open_external_console_argv_keep_open", side_effect=_open):
            external = service._launch_psexec_external(spec, password=SECRET)
        self.assertTrue(external.ok)
        self.assertNotIn(SECRET, external.message)
        self.assertNotIn(SECRET, external.display_command)
        self.assertEqual(captured["argv"][captured["argv"].index("-p") + 1], SECRET)
        self.assertEqual(captured["argv"], call["argv"])

        class _Boom:
            def run(self, *_args, **_kwargs):
                raise RuntimeError(f"falha {SECRET}")

        failed = CommandExecutionService(_Boom(), log_fn=logs.append)._launch_psexec_conpty(
            spec, password=SECRET
        )
        self.assertFalse(failed.ok)
        self.assertNotIn(SECRET, failed.message)
        self.assertIn(REDACTED, failed.message)
        redacted = redact_command_text(f"eco {SECRET}", passwords=[self.tab.auth_password()])
        self.assertNotIn(SECRET, redacted)

    def test_shared_credential_helpers(self) -> None:
        self.tab.kind_domain.setChecked(True)
        self.tab.domain_edit.setText("CONTOSO")
        self.tab.user_edit.setText("joao")
        self.tab.pass_edit.setText(SECRET)
        creds = CredentialContext(
            user=self.tab.auth_username(),
            password=self.tab.auth_password(),
        )
        argv = build_psexec_argv(
            psexec_exe="PsExec.exe",
            host="PC01",
            remote_argv=["hostname"],
            creds=creds,
        )
        self.assertEqual(argv[argv.index("-u") + 1], r"CONTOSO\joao")
        self.assertEqual(argv[argv.index("-p") + 1], SECRET)

        self.tab.kind_windows.setChecked(True)
        empty = CredentialContext(
            user=self.tab.auth_username(),
            password=self.tab.auth_password(),
        )
        plain = build_psexec_argv(
            psexec_exe="PsExec.exe",
            host="PC01",
            remote_argv=["hostname"],
            creds=empty,
        )
        self.assertNotIn("-u", plain)
        self.assertNotIn("-p", plain)
        self.assertNotIn(SECRET, plain)

        winget_local = build_psexec_args(
            psexec_path="PsExec.exe",
            host="PC01",
            username=r".\joao",
            password=SECRET,
            ps_command="hostname",
        )
        self.assertEqual(winget_local[winget_local.index("-u") + 1], r".\joao")
        self.assertEqual(winget_local[winget_local.index("-p") + 1], SECRET)
        winget_windows = build_psexec_args(
            psexec_path="PsExec.exe",
            host="PC01",
            username="",
            password=SECRET,
            ps_command="hostname",
        )
        self.assertNotIn("-u", winget_windows)
        self.assertNotIn("-p", winget_windows)
