"""Flags, timeout, argumentos extras e modo -d do PsExec."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from remoteops.core.builder import CommandBuilder
from remoteops.core.models import ExecutionResult, OperationStatus
from remoteops.core.psexec_options import (
    TIMEOUT_MAX_SECONDS,
    PsExecOptions,
    build_psexec_option_argv,
    compute_psexec_option_state,
    options_from_params,
    sanitize_psexec_options,
    validate_psexec_options,
)
from remoteops.core.win_cmdline import split_extra_args, split_windows_command_line
from remoteops.utils.domain import HostUserDomain
from remoteops.utils.host_reachability import HostAccessKind, HostReachability
from remoteops.utils.redaction import REDACTED, format_argv_for_display, redact_command_text


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


class WindowsArgSplitTests(unittest.TestCase):
    def test_shared_splitter_matches_command_line_to_argv(self) -> None:
        sample = r'uno "dos tres" /quiet'
        self.assertEqual(split_extra_args(sample), split_windows_command_line(sample))

    def test_quoted_path_with_spaces(self) -> None:
        self.assertEqual(
            split_extra_args(r'"C:\Program Files\app.exe" /S'),
            [r"C:\Program Files\app.exe", "/S"],
        )

    def test_empty_quoted_argument_is_kept(self) -> None:
        self.assertEqual(split_extra_args(r'a "" b'), ["a", "", "b"])

    def test_unicode_and_slash_flags(self) -> None:
        self.assertEqual(split_extra_args("café /quiet"), ["café", "/quiet"])

    def test_installer_style_property(self) -> None:
        self.assertEqual(
            split_extra_args(r'/qn PROPERTY="valor com espaço"'),
            ["/qn", "PROPERTY=valor com espaço"],
        )

    def test_builder_appends_extra_args_with_windows_rules(self) -> None:
        builder = CommandBuilder()
        builder.set_psexec_params(
            {
                "host": "PC01",
                "remote_cmd": "setup.exe",
                "extra_args": r'"C:\Program Files\payload" /S',
                "-accepteula": True,
                "-nobanner": True,
            }
        )
        spec = builder.build_psexec_spec()
        self.assertIn(r"C:\Program Files\payload", spec.argv)
        self.assertIn("/S", spec.argv)
        self.assertTrue(spec.argv.index("setup.exe") < spec.argv.index("/S"))


class FlagCompatibilityTests(unittest.TestCase):
    def test_each_isolated_flag_emits(self) -> None:
        cases = {
            "flag_d": "-d",
            "flag_e": "-e",
            "flag_c": "-c",
            "flag_arm": "-arm",
            "flag_accepteula": "-accepteula",
            "flag_nobanner": "-nobanner",
        }
        for attr, token in cases.items():
            with self.subTest(flag=token):
                argv = build_psexec_option_argv(PsExecOptions(**{attr: True}))
                self.assertIn(token, argv)

    def test_copy_force_and_version_require_c(self) -> None:
        self.assertTrue(validate_psexec_options(PsExecOptions(flag_f=True)))
        self.assertTrue(validate_psexec_options(PsExecOptions(flag_v=True)))
        ok = build_psexec_option_argv(PsExecOptions(flag_c=True, flag_f=True))
        self.assertEqual(ok.count("-c"), 1)
        self.assertIn("-f", ok)
        both = validate_psexec_options(PsExecOptions(flag_c=True, flag_f=True, flag_v=True))
        self.assertTrue(any("mutuamente" in error for error in both))
        cleaned = sanitize_psexec_options(
            PsExecOptions(flag_c=True, flag_f=True, flag_v=True),
            trigger="-f",
        )
        self.assertTrue(cleaned.flag_f)
        self.assertFalse(cleaned.flag_v)

    def test_system_and_e_conflict(self) -> None:
        errors = validate_psexec_options(PsExecOptions(flag_s=True, flag_e=True))
        self.assertTrue(any("-s" in error and "-e" in error for error in errors))
        state = compute_psexec_option_state(
            PsExecOptions(flag_s=True, flag_e=True),
            trigger="-s",
        )
        self.assertTrue(state.options.flag_s)
        self.assertFalse(state.options.flag_e)
        self.assertFalse(state.widgets["-e"].enabled)

    def test_robocopy_blocks_copy_flags(self) -> None:
        builder = CommandBuilder()
        builder.set_robocopy_params({"dest": "Temp", "switches": "/R:1"})
        builder.set_psexec_params(
            {
                "host": "PC01",
                "remote_cmd": "hostname",
                "-c": True,
                "-f": True,
            }
        )
        errors = builder.validate_psexec_params()
        self.assertTrue(any("Robocopy" in error for error in errors))
        state = compute_psexec_option_state(
            PsExecOptions(flag_c=True, flag_f=True, copy_allowed=False)
        )
        self.assertFalse(state.options.flag_c)
        self.assertFalse(state.widgets["-c"].enabled)


class TimeoutValidationTests(unittest.TestCase):
    def test_zero_omits_n(self) -> None:
        self.assertNotIn("-n", build_psexec_option_argv(PsExecOptions(timeout=0)))

    def test_positive_emits_n(self) -> None:
        argv = build_psexec_option_argv(PsExecOptions(timeout=30))
        self.assertEqual(argv[argv.index("-n") + 1], "30")

    def test_negative_and_over_limit_are_rejected(self) -> None:
        self.assertTrue(
            any("negativo" in error for error in validate_psexec_options(PsExecOptions(timeout=-1)))
        )
        self.assertTrue(
            any(
                str(TIMEOUT_MAX_SECONDS) in error
                for error in validate_psexec_options(
                    PsExecOptions(timeout=TIMEOUT_MAX_SECONDS + 1)
                )
            )
        )

    def test_max_value_is_accepted(self) -> None:
        self.assertEqual(
            validate_psexec_options(PsExecOptions(timeout=TIMEOUT_MAX_SECONDS)),
            [],
        )
        argv = build_psexec_option_argv(PsExecOptions(timeout=TIMEOUT_MAX_SECONDS))
        self.assertEqual(argv[argv.index("-n") + 1], str(TIMEOUT_MAX_SECONDS))

    def test_non_numeric_becomes_zero_in_params(self) -> None:
        opts = options_from_params({"timeout": "abc"})
        self.assertEqual(opts.timeout, 0)
        self.assertEqual(validate_psexec_options(opts), [])


class DetachedLaunchTests(unittest.TestCase):
    def test_detached_pid_is_not_treated_as_remote_failure(self) -> None:
        result = ExecutionResult(
            return_code=4242,
            metadata={"psexec_detached": True},
        ).finalize()
        self.assertTrue(result.success)
        self.assertEqual(result.status, OperationStatus.COMPLETED)
        self.assertTrue(result.metadata.get("detached_launch"))

    def test_detached_negative_code_is_failure(self) -> None:
        result = ExecutionResult(
            return_code=-1,
            metadata={"psexec_detached": True},
        ).finalize()
        self.assertFalse(result.success)
        self.assertEqual(result.status, OperationStatus.FAILED)

    def test_builder_marks_detached_metadata(self) -> None:
        builder = CommandBuilder()
        builder.set_psexec_params(
            {
                "host": "PC01",
                "remote_cmd": "hostname",
                "-d": True,
                "-accepteula": True,
            }
        )
        spec = builder.build_psexec_spec()
        self.assertIn("-d", spec.argv)
        self.assertTrue(spec.metadata.get("psexec_detached"))


class PreviewAndRedactionTests(unittest.TestCase):
    def test_preview_matches_execution_argv_shape(self) -> None:
        builder = CommandBuilder()
        builder.set_psexec_params(
            {
                "host": "PC01",
                "user": r"EMPRESA\ana",
                "has_password": True,
                "remote_cmd": "cmd.exe",
                "extra_args": "/c echo ok",
                "-d": True,
                "-accepteula": True,
                "-nobanner": True,
                "timeout": 15,
            }
        )
        spec = builder.build_psexec_spec()
        display = spec.display_command
        self.assertIn("-d", spec.argv)
        self.assertIn("-n", spec.argv)
        self.assertIn("15", spec.argv)
        self.assertIn("/c", spec.argv)
        self.assertIn("echo", spec.argv)
        self.assertIn(REDACTED, display)
        self.assertNotIn("Segredo", display)
        joined = format_argv_for_display(spec.argv)
        self.assertIn(REDACTED, joined)

    def test_exception_text_is_redacted(self) -> None:
        secret = "SenhaSecreta99"
        safe = redact_command_text(f"PsExec -p {secret} failed", passwords=[secret])
        self.assertNotIn(secret, safe)
        self.assertIn(REDACTED, safe)


class FlagsCardUiTests(unittest.TestCase):
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
            side_effect=_quiet_probe,
        )
        self._cpu_patch = patch(
            "remoteops.ui.tabs.psexec.query_remote_cpu_topology",
            return_value=(None, "Falha ao consultar os processadores."),
        )
        for item in (
            self._domain_patch,
            self._session_patch,
            self._probe_patch,
            self._cpu_patch,
        ):
            item.start()
        self.tab = PsExecTab()
        self.tab.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.tab.show()
        QApplication.processEvents()
        self.tab._host_status_timer.stop()
        self.tab._cpu_refresh_timer.stop()
        self.tab._session_refresh_timer.stop()

    def tearDown(self) -> None:
        self.tab.shutdown(wait_ms=200)
        self.tab.close()
        for item in (
            self._cpu_patch,
            self._probe_patch,
            self._session_patch,
            self._domain_patch,
        ):
            item.stop()
        self.tab.deleteLater()
        QApplication.processEvents()

    def test_card_keeps_three_rows_and_eight_flags(self) -> None:
        card = self.tab._form_cards[4]
        self.assertEqual(card._title_label.text(), "Flags e Argumentos")
        flags = (
            self.tab.flag_d,
            self.tab.flag_e,
            self.tab.flag_c,
            self.tab.flag_f,
            self.tab.flag_v,
            self.tab.flag_arm,
            self.tab.flag_accepteula,
            self.tab.flag_nobanner,
        )
        self.assertEqual(len(flags), 8)
        self.assertEqual(self.tab.timeout_spin.minimum(), 0)
        self.assertEqual(self.tab.timeout_spin.maximum(), TIMEOUT_MAX_SECONDS)
        self.assertIn("conexão", self.tab.timeout_spin.toolTip().casefold())

    def test_f_and_v_disable_without_c(self) -> None:
        self.tab.flag_c.setChecked(False)
        self.tab.update_psexec_option_state("-c")
        QApplication.processEvents()
        self.assertFalse(self.tab.flag_f.isEnabled())
        self.assertFalse(self.tab.flag_v.isEnabled())
        self.assertIn("-c", self.tab.flag_f.toolTip())
        self.tab.flag_c.setChecked(True)
        self.tab.update_psexec_option_state("-c")
        QApplication.processEvents()
        self.assertTrue(self.tab.flag_f.isEnabled())
        self.tab.flag_f.setChecked(True)
        self.tab.update_psexec_option_state("-f")
        QApplication.processEvents()
        self.assertFalse(self.tab.flag_v.isEnabled())

    def test_reset_restores_flags_and_timeout(self) -> None:
        self.tab.timeout_spin.setValue(42)
        self.tab.flag_d.setChecked(True)
        self.tab.flag_c.setChecked(True)
        self.tab.flag_f.setChecked(True)
        self.tab.extra_args.setText("/S")
        self.tab._reset_card_flags()
        QApplication.processEvents()
        self.assertEqual(self.tab.timeout_spin.value(), 0)
        self.assertFalse(self.tab.flag_d.isChecked())
        self.assertFalse(self.tab.flag_c.isChecked())
        self.assertFalse(self.tab.flag_f.isChecked())
        self.assertEqual(self.tab.extra_args.text(), "")

    def test_preview_updates_with_timeout_and_d(self) -> None:
        self.tab.host_edit.setText("PC01")
        self.tab.remote_cmd_edit.setText("hostname")
        self.tab.timeout_spin.setValue(12)
        self.tab.flag_d.setChecked(True)
        self.tab.flag_accepteula.setChecked(True)
        self.tab.update_psexec_option_state("-d")
        builder = CommandBuilder()
        builder.set_psexec_params(
            self.tab.collect_builder_params(
                host="PC01",
                psexec_path="",
                remote_cmd="hostname",
            )
        )
        display = builder.build_psexec()
        self.assertIn("-d", display)
        self.assertIn("-n", display)
        self.assertIn("12", display)


if __name__ == "__main__":
    unittest.main()
