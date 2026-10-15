"""Prioridade, grupo e afinidade do PsExec com topologia do computador remoto."""

from __future__ import annotations

import base64
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtGui import QValidator
from PyQt6.QtWidgets import QApplication, QMessageBox

from remoteops.core.auth_identity import AuthKind
from remoteops.core.builder import CommandBuilder
from remoteops.core.psexec_options import (
    PRIORITIES,
    PsExecOptions,
    build_psexec_option_argv,
    options_from_params,
    options_to_params,
    sanitize_psexec_options,
    validate_psexec_options,
)
from remoteops.ui.tabs.psexec import _CpuTopologyWorker
from remoteops.utils.cpu_topology import (
    CpuTopology,
    ProcessorGroup,
    classify_affinity_input,
    classify_cpu_query,
    describe_cpu_status,
    parse_cpu_topology_output,
    query_remote_cpu_topology,
)
from remoteops.utils.domain import HostUserDomain
from remoteops.utils.host_reachability import HostAccessKind, HostReachability
from remoteops.utils.validator import AffinityValidator

SECRET = "Segredo 1"


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


def _topology(host: str, groups: list[tuple[int, int]], computer: str = "") -> CpuTopology:
    return CpuTopology(
        host=host,
        groups=tuple(ProcessorGroup(group_id, count) for group_id, count in groups),
        computer=computer,
        queried_at=datetime.now(timezone.utc),
    )


class PriorityAndTopologyTests(unittest.TestCase):
    def test_default_priority_emits_no_flag(self) -> None:
        argv = build_psexec_option_argv(PsExecOptions())
        self.assertFalse(set(argv) & set(PRIORITIES))

    def test_each_priority_maps_to_its_flag(self) -> None:
        expected = {
            "-low": "-low",
            "-belownormal": "-belownormal",
            "-abovenormal": "-abovenormal",
            "-high": "-high",
            "-realtime": "-realtime",
            "-background": "-background",
        }
        for priority, flag in expected.items():
            argv = build_psexec_option_argv(PsExecOptions(priority=priority))
            self.assertIn(flag, argv)
            others = [item for item in PRIORITIES if item != flag]
            self.assertFalse(set(argv) & set(others))

    def test_background_is_not_low_or_detached(self) -> None:
        argv = build_psexec_option_argv(
            PsExecOptions(priority="-background", flag_d=True)
        )
        self.assertEqual(argv, ["-d", "-background"])

    def test_invalid_priority_is_rejected(self) -> None:
        errors = validate_psexec_options(PsExecOptions(priority="-d"))
        self.assertTrue(any("Prioridade inválida" in error for error in errors))
        self.assertNotIn("-d", build_psexec_option_argv(PsExecOptions(priority="-d")))

    def test_parser_keeps_group_sizes_separate(self) -> None:
        text = "\n".join(
            [
                "Connecting to PC01...",
                "GROUPS 2",
                "GROUP 1 8",
                "COMPUTER SRV01",
                "GROUP 0 16",
            ]
        )
        groups, computer, error = parse_cpu_topology_output(text)
        self.assertEqual(error, "")
        self.assertEqual(computer, "SRV01")
        self.assertEqual([(group.group_id, group.logical_count) for group in groups], [(0, 16), (1, 8)])
        self.assertEqual(sum(group.logical_count for group in groups), 24)

    def test_parser_rejects_incomplete_or_impossible_groups(self) -> None:
        for text in (
            "",
            "GROUPS 2\nGROUP 0 16\n",
            "GROUPS 1\nGROUP 0 0\n",
            "GROUPS 1\nGROUP 0 65\n",
            "CPU_TOPOLOGY_ERROR\n",
            "NumberOfLogicalProcessors 16\n",
        ):
            groups, _computer, error = parse_cpu_topology_output(text)
            self.assertEqual(groups, ())
            self.assertTrue(error)

    def test_affinity_states_and_fixup_keep_the_typed_value(self) -> None:
        validator = AffinityValidator(16)
        acceptable, _, _ = validator.validate("1,2,3", 0)
        intermediate, _, _ = validator.validate("1,2,", 0)
        invalid, _, _ = validator.validate("1,2,2", 0)
        out_of_range, _, _ = validator.validate("1,99", 0)
        letters, _, _ = validator.validate("1,a", 0)
        self.assertEqual(acceptable, QValidator.State.Acceptable)
        self.assertEqual(intermediate, QValidator.State.Intermediate)
        self.assertEqual(invalid, QValidator.State.Invalid)
        self.assertEqual(out_of_range, QValidator.State.Invalid)
        self.assertEqual(letters, QValidator.State.Invalid)
        self.assertEqual(validator.fixup("1,2,"), "1,2")
        self.assertEqual(validator.fixup("1,99"), "1,99")
        self.assertEqual(validator.fixup("1,2,2"), "1,2,2")
        unknown = AffinityValidator(None)
        state, _, _ = unknown.validate("1,99", 0)
        self.assertEqual(state, QValidator.State.Acceptable)
        self.assertEqual(classify_affinity_input("", max_cpu=16), ("acceptable", ""))

    def test_known_topology_checks_the_selected_group_not_the_sum(self) -> None:
        groups = ((0, 4), (1, 20))
        automatic = PsExecOptions(
            affinity="5",
            cpu_topology_verified=True,
            cpu_topology_groups=groups,
        )
        self.assertTrue(validate_psexec_options(automatic))
        on_second = PsExecOptions(
            cpu_group=1,
            affinity="5",
            cpu_topology_verified=True,
            cpu_topology_groups=groups,
        )
        self.assertEqual(validate_psexec_options(on_second), [])
        too_far = PsExecOptions(
            cpu_group=1,
            affinity="21",
            cpu_topology_verified=True,
            cpu_topology_groups=groups,
        )
        self.assertTrue(any("fora" in error for error in validate_psexec_options(too_far)))

    def test_unknown_topology_warns_and_keeps_the_requested_cpus(self) -> None:
        opts = PsExecOptions(cpu_group=1, affinity="1, 2, 40")
        self.assertEqual(validate_psexec_options(opts), [])
        text, _tip = describe_cpu_status(
            state="not_queried",
            topology=None,
            affinity=opts.affinity,
            cpu_group=1,
        )
        self.assertIn("Não consultado", text)
        self.assertIn("não foram conferidos", text)
        self.assertNotIn("Dados atualizados", text)
        argv = build_psexec_option_argv(opts)
        self.assertEqual(argv, ["-g", "1", "-a", "1,2,40"])
        kept = sanitize_psexec_options(PsExecOptions(affinity="1,2,99", cpu_group=3))
        self.assertEqual(kept.affinity, "1,2,99")
        self.assertEqual(kept.cpu_group, 3)

    def test_invalid_affinity_is_not_rewritten_into_a_smaller_mask(self) -> None:
        opts = PsExecOptions(
            affinity="1,2,2",
            cpu_topology_verified=True,
            cpu_topology_groups=((0, 16),),
        )
        self.assertTrue(validate_psexec_options(opts))
        argv = build_psexec_option_argv(opts)
        self.assertIn("1,2,2", argv)
        self.assertNotIn("1,2", [item for item in argv if item != "1,2,2"])
        out_of_range = PsExecOptions(
            affinity="1,2,99",
            cpu_topology_verified=True,
            cpu_topology_groups=((0, 16),),
        )
        self.assertTrue(any("99" in error for error in validate_psexec_options(out_of_range)))
        self.assertIn("1,2,99", build_psexec_option_argv(out_of_range))

    def test_missing_group_blocks_without_falling_back_to_automatic(self) -> None:
        opts = PsExecOptions(
            cpu_group=5,
            cpu_topology_verified=True,
            cpu_topology_groups=((0, 8),),
            cpu_group_unavailable=True,
            cpu_group_block_reason="Grupo CPU 5 não existe neste computador.",
        )
        errors = validate_psexec_options(opts)
        self.assertIn("Grupo CPU 5 não existe neste computador.", errors)
        self.assertNotIn("-g", build_psexec_option_argv(opts))

    def test_topology_round_trip_drops_unusable_limits(self) -> None:
        opts = PsExecOptions(
            cpu_group=1,
            affinity="2",
            cpu_topology_verified=True,
            cpu_topology_groups=((0, 4), (1, 8)),
        )
        again = options_from_params(options_to_params(opts))
        self.assertTrue(again.cpu_topology_verified)
        self.assertEqual(again.cpu_topology_groups, ((0, 4), (1, 8)))
        broken = options_from_params(
            {"cpu_topology_verified": True, "cpu_topology_groups": [(0, 0)]}
        )
        self.assertFalse(broken.cpu_topology_verified)
        self.assertEqual(broken.cpu_topology_groups, ())

    def test_query_uses_remote_group_api_and_hides_password(self) -> None:
        captured: list[list[str]] = []

        def _run(argv, timeout):
            captured.append(list(argv))
            self.assertEqual(timeout, 20.0)
            return subprocess.CompletedProcess(
                argv,
                0,
                "GROUPS 1\nGROUP 0 4\nCOMPUTER HOST01\n",
                "",
            )

        topology, error = query_remote_cpu_topology(
            "PC01",
            user="EMPRESA\\ana",
            password=SECRET,
            run=_run,
        )
        self.assertEqual(error, "")
        self.assertIsNotNone(topology)
        assert topology is not None
        self.assertEqual(topology.logical_processors, 4)
        self.assertEqual(topology.computer, "HOST01")
        argv = captured[0]
        self.assertNotIn("-s", argv)
        self.assertNotIn("-h", argv)
        self.assertIn("-accepteula", argv)
        self.assertIn(SECRET, argv)
        script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16le")
        self.assertIn("GetActiveProcessorGroupCount", script)
        self.assertIn("GetActiveProcessorCount", script)
        self.assertNotIn("NumberOfLogicalProcessors", script)
        captured.clear()
        topology, error = query_remote_cpu_topology("PC01", run=_run)
        self.assertEqual(error, "")
        self.assertIsNotNone(topology)
        plain = captured[0]
        self.assertNotIn("-u", plain)
        self.assertNotIn("-p", plain)
        self.assertNotIn("-s", plain)

    def test_empty_psexec_pipe_uses_the_remote_file(self) -> None:
        def _run(argv, timeout):
            self.assertIn("-EncodedCommand", argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                "",
                "powershell.exe exited with error code 0.",
            )

        def _read(path):
            self.assertIn("RemoteOpsCpu_", path)
            return "GROUPS 1\r\nGROUP 0 8\r\nCOMPUTER SRV01\r\n", ""

        topology, error = query_remote_cpu_topology(
            "PC01",
            run=_run,
            read_result=_read,
        )
        self.assertEqual(error, "")
        self.assertIsNotNone(topology)
        assert topology is not None
        self.assertEqual(topology.logical_processors, 8)
        self.assertEqual(topology.computer, "SRV01")

    def test_query_failures_do_not_leak_the_password(self) -> None:
        def _timeout(_argv, timeout):
            raise subprocess.TimeoutExpired("PsExec.exe", timeout)

        def _boom(_argv, _timeout):
            raise RuntimeError(f"falha {SECRET}")

        _topology, timeout_error = query_remote_cpu_topology(
            "PC01", password=SECRET, run=_timeout
        )
        self.assertEqual(timeout_error, "Tempo esgotado ao consultar os processadores.")
        self.assertNotIn(SECRET, timeout_error)
        _topology, boom_error = query_remote_cpu_topology(
            "PC01", password=SECRET, run=_boom
        )
        self.assertEqual(boom_error, "Falha ao consultar os processadores.")
        self.assertNotIn(SECRET, boom_error)
        denied, denied_error = query_remote_cpu_topology(
            "PC01",
            password=SECRET,
            run=lambda argv, timeout: subprocess.CompletedProcess(
                argv, 1, "", "Access is denied."
            ),
        )
        self.assertIsNone(denied)
        self.assertEqual(denied_error, "Acesso negado.")
        offline, offline_error = query_remote_cpu_topology(
            "PC01",
            run=lambda argv, timeout: subprocess.CompletedProcess(
                argv, 1, "", "The network path was not found."
            ),
        )
        self.assertIsNone(offline)
        self.assertEqual(offline_error, "Computador indisponível.")
        self.assertEqual(classify_cpu_query("WinError 5"), ("denied", "Acesso negado."))
        self.assertEqual(
            classify_cpu_query("WinError 53"),
            ("unavailable", "Computador indisponível."),
        )


class PerformanceCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.ui.tabs.psexec import PsExecTab

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
        self._cpu_patch = patch(
            "remoteops.ui.tabs.psexec.query_remote_cpu_topology",
            return_value=(None, "Falha ao consultar os processadores."),
        )
        self._local_groups = patch(
            "remoteops.utils.api.get_processor_groups",
            return_value=[7, 8, 9],
        )
        self._local_count = patch(
            "remoteops.utils.api.get_processor_count",
            return_value=99,
        )
        for item in (
            self._domain_patch,
            self._session_domain_patch,
            self._probe_patch,
            self._cpu_patch,
            self._local_groups,
            self._local_count,
        ):
            item.start()
        self.tab = PsExecTab()
        self.tab.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.tab.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.tab._host_status_timer.stop()
        self.tab._cpu_refresh_timer.stop()
        self.tab._session_refresh_timer.stop()
        self.tab.shutdown(wait_ms=2000)
        self.tab.close()
        for item in (
            self._local_count,
            self._local_groups,
            self._cpu_patch,
            self._probe_patch,
            self._session_domain_patch,
            self._domain_patch,
        ):
            item.stop()
        self.tab.deleteLater()
        QApplication.processEvents()

    def _online(self, host: str = "PC01") -> None:
        self.tab._host_status_timer.stop()
        self.tab.host_edit.setText(host)
        self.tab._host_status_timer.stop()
        self.tab._cpu_refresh_timer.stop()
        self.tab._host_online = True
        self.tab._update_cpu_query_enabled()
        QApplication.processEvents()

    def _stop_cpu_timer(self) -> None:
        self.tab._cpu_refresh_timer.stop()
        self.tab._host_status_timer.stop()

    def _deliver(
        self,
        groups: list[tuple[int, int]],
        error: str = "",
        *,
        generation: int | None = None,
        host: str = "",
        computer: str = "",
    ) -> None:
        current = host or self.tab.host_edit.text()
        topology = None if error else _topology(current, groups, computer)
        gen = self.tab._cpu_generation if generation is None else generation
        self.tab._on_cpu_result(gen, current, topology, error)
        QApplication.processEvents()

    def _select_group(self, group_id: int) -> None:
        combo = self.tab.group_combo
        for index in range(combo.count()):
            if combo.itemData(index) == group_id:
                combo.setCurrentIndex(index)
                return
        self.fail(f"grupo {group_id} não apareceu: {self._texts()}")

    def _texts(self) -> list[str]:
        combo = self.tab.group_combo
        return [combo.itemText(index) for index in range(combo.count())]

    def _spec(self, host: str | None = None):
        builder = CommandBuilder()
        builder.set_psexec_params(
            self.tab.collect_builder_params(
                host=host or self.tab.host_edit.text() or "PC01",
                psexec_path="",
                remote_cmd="hostname",
            )
        )
        return builder.build_psexec_spec()

    def test_card_starts_without_local_processors(self) -> None:
        card = self.tab._form_cards[3]
        self.assertEqual(card._title_label.text(), "Desempenho")
        self.assertTrue(card.is_collapsed)
        self.assertEqual(
            [self.tab.priority_combo.itemData(i) for i in range(self.tab.priority_combo.count())],
            ["", "-low", "-belownormal", "-abovenormal", "-high", "-realtime", "-background"],
        )
        self.assertEqual(self.tab.priority_combo.itemText(0), "Padrão")
        self.assertEqual(self.tab.priority_combo.itemText(5), "Tempo real — crítico")
        self.assertEqual(
            self.tab.priority_combo.itemText(6),
            "Segundo plano — E/S e memória",
        )
        self.assertEqual(self._texts(), ["Automático (sem -g)"])
        self.assertEqual(self.tab.affinity_edit.placeholderText(), "1,2,3")
        self.assertEqual(self.tab.cpu_status.text(), "Não consultado.")
        self.assertFalse(self.tab.cpu_query_btn.isEnabled())
        self.assertEqual(self.tab.cpu_query_btn.focusPolicy(), Qt.FocusPolicy.TabFocus)
        self.assertEqual(self.tab.cpu_query_btn.text(), self.tab.session_refresh_btn.text())
        self.assertEqual(self.tab.cpu_query_btn.size(), self.tab.session_refresh_btn.size())
        self.assertEqual(self.tab.cpu_query_btn.accessibleName(), "Atualizar")
        self.assertIsNone(self.tab.affinity_validator.max_cpu)
        self.assertFalse(self.tab.realtime_warning.isVisible())

    def test_realtime_warns_and_requires_confirmation(self) -> None:
        self.tab._form_cards[3]._apply_collapsed_state(False)
        self.tab.priority_combo.setCurrentIndex(5)
        QApplication.processEvents()
        self.assertTrue(self.tab.realtime_warning.isVisible())
        with patch(
            "remoteops.ui.tabs.psexec.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as question:
            self.assertFalse(self.tab.confirm_realtime_priority())
        self.assertEqual(question.call_args.args[-1], QMessageBox.StandardButton.No)
        with patch(
            "remoteops.ui.tabs.psexec.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.assertTrue(self.tab.confirm_realtime_priority())
        self.tab.priority_combo.setCurrentIndex(0)
        with patch("remoteops.ui.tabs.psexec.QMessageBox.question") as question:
            self.assertTrue(self.tab.confirm_realtime_priority())
        question.assert_not_called()
        self.assertFalse(self.tab.realtime_warning.isVisible())

    def test_preview_matches_priority_group_and_affinity(self) -> None:
        self._online()
        self._deliver([(0, 16), (1, 8)], computer="SRV01")
        self.tab.priority_combo.setCurrentIndex(6)
        self._select_group(1)
        self.tab.affinity_edit.setText("1, 2")
        display = self._spec().display_command
        self.assertNotIn("# Erro", display)
        self.assertIn("-background", display)
        self.assertNotIn("-low", display)
        self.assertNotIn(" -d", f" {display}")
        self.assertIn("-g 1", display)
        self.assertIn("-a 1,2", display)
        self.assertIn("Dados atualizados", self.tab.cpu_status.text())
        self.assertIn("SRV01", self.tab.cpu_status.toolTip())

    def test_group_change_revalidates_without_discarding_affinity(self) -> None:
        self._online()
        self._deliver([(0, 16), (1, 4)])
        self.tab.affinity_edit.setText("10")
        self._select_group(1)
        self.assertEqual(self.tab.affinity_edit.text(), "10")
        self.assertIn("fora", self.tab.cpu_status.text())
        self.assertTrue(self._spec().display_command.startswith("# Erro PsExec:"))
        self._select_group(0)
        self.assertEqual(self.tab.affinity_edit.text(), "10")
        self.assertIn("Dados atualizados", self.tab.cpu_status.text())
        self.assertNotIn("# Erro", self._spec().display_command)

    def test_trailing_comma_duplicate_and_empty_affinity(self) -> None:
        self._online()
        self.tab.affinity_edit.setText("1,2,")
        self.assertEqual(self.tab.affinity_edit.text(), "1,2,")
        state, _, _ = self.tab.affinity_validator.validate("1,2,", 0)
        self.assertEqual(state, QValidator.State.Intermediate)
        self.assertIn("incompleta", self.tab.cpu_status.text())
        self.tab.affinity_edit.setText("1,2,2")
        self.assertEqual(self.tab.affinity_edit.text(), "1,2,2")
        self.assertIn("repete", self.tab.cpu_status.text())
        self.tab.affinity_edit.clear()
        self.assertEqual(self.tab.cpu_status.text(), "Não consultado.")
        self.assertNotIn("# Erro", self._spec().display_command)
        self._deliver([(0, 4)])
        self.tab._form_cards[3]._apply_collapsed_state(False)
        self.tab.affinity_edit.setFocus()
        self.tab.affinity_edit.setText("1,9")
        self.tab.affinity_edit.clearFocus()
        QApplication.processEvents()
        self.assertEqual(self.tab.affinity_edit.text(), "1,9")
        self.assertIn("fora", self.tab.cpu_status.text())

    def test_query_failure_timeout_and_offline_states(self) -> None:
        self._online()
        self._deliver([], error="Acesso negado.")
        self.assertEqual(self.tab._cpu_state, "failed")
        self.assertIn("Acesso negado.", self.tab.cpu_status.text())
        self.assertNotIn("processador", self.tab.cpu_status.text())
        self._deliver([], error="Tempo esgotado ao consultar os processadores.")
        self.assertIn("Tempo esgotado", self.tab.cpu_status.text())
        self.tab._set_host_status("offline", psexec_enabled=False)
        self._stop_cpu_timer()
        self.assertEqual(self.tab.cpu_status.text(), "Indisponível.")
        self.tab.host_edit.setText("PC 01")
        self._stop_cpu_timer()
        self.assertEqual(self.tab.cpu_status.text(), "Indisponível.")
        self.tab.host_edit.clear()
        self._stop_cpu_timer()
        self.assertEqual(self.tab.cpu_status.text(), "Não consultado.")

    def test_host_change_drops_the_previous_topology(self) -> None:
        self._online("PC01")
        self._deliver([(0, 16), (1, 8)])
        self._select_group(1)
        self.tab.affinity_edit.setText("1,2")
        self.tab.host_edit.setText("PC02")
        self._stop_cpu_timer()
        self.tab._host_online = True
        self.tab._on_cpu_result(
            self.tab._cpu_generation,
            "PC01",
            _topology("PC01", [(0, 16), (1, 8)]),
            "",
        )
        self.assertEqual(self.tab.affinity_edit.text(), "1,2")
        self.assertEqual(self._texts(), ["Automático (sem -g)"])
        self.assertNotIn("16", self.tab.cpu_status.text())
        self.assertNotIn("Dados atualizados", self.tab.cpu_status.text())
        display = self._spec("PC02").display_command
        self.assertNotIn("-g", display)
        self.assertIn("-a 1,2", display)
        self.assertIn("não foi conferida", self.tab.cpu_status.text())

    def test_credential_change_invalidates_limits_and_old_results(self) -> None:
        self._online()
        self.tab._select_kind(AuthKind.DOMAIN, clear_account=False)
        self.tab.domain_edit.setText("EMPRESA")
        self.tab.user_edit.setText("ana")
        self._stop_cpu_timer()
        self._deliver([(0, 8), (1, 4)])
        self._select_group(1)
        self.tab.pass_edit.setText(SECRET)
        self._stop_cpu_timer()
        self.assertEqual(self.tab._cpu_state, "stale")
        self.assertNotIn("8", " ".join(self._texts()))
        self.assertIn("não verificado", " ".join(self._texts()))
        self.assertIn("Desatualizado", self.tab.cpu_status.text())
        stale = _topology("PC01", [(0, 64)])
        self.tab._on_cpu_result(self.tab._cpu_generation - 1, "PC01", stale, "")
        self.assertNotIn("64", self.tab.cpu_status.text())
        self.assertNotIn(SECRET, self.tab.cpu_status.text())
        self.assertNotIn(SECRET, self._spec().display_command)

    def test_reset_restores_choices_and_keeps_the_current_topology(self) -> None:
        self._online()
        self._deliver([(0, 4)])
        self.tab.priority_combo.setCurrentIndex(4)
        self._select_group(0)
        self.tab.affinity_edit.setText("1,2")
        self.tab._reset_card_desempenho()
        self.assertEqual(self.tab.priority_combo.currentData(), "")
        self.assertIsNone(self.tab.group_combo.currentData())
        self.assertEqual(self.tab.affinity_edit.text(), "")
        self.assertIn("4 processadores lógicos", self.tab.cpu_status.text())
        self.assertIn("1 grupo CPU", self.tab.cpu_status.text())
        self.assertNotIn("-high", self._spec().display_command)
        self.assertNotIn("-g", self._spec().display_command)
        self.assertNotIn("-a", self._spec().display_command)

    def test_stale_result_does_not_replace_a_newer_topology(self) -> None:
        self._online()
        self._deliver([(0, 4)])
        previous = self.tab._cpu_generation
        self.tab._cpu_generation += 1
        self._deliver([(0, 32)], generation=previous)
        self.assertIn("4 processadores lógicos", self.tab.cpu_status.text())
        self.assertNotIn("32", self.tab.cpu_status.text())

    def test_manual_query_does_not_start_a_second_worker(self) -> None:
        calls: list[int] = []
        release = threading.Event()

        def _hang(_host, user="", password="", should_cancel=None, **_kwargs):
            calls.append(1)
            while not release.is_set():
                if should_cancel and should_cancel():
                    return None, ""
                time.sleep(0.01)
            return None, "Acesso negado."

        self._cpu_patch.stop()
        try:
            with patch(
                "remoteops.ui.tabs.psexec.query_remote_cpu_topology",
                side_effect=_hang,
            ):
                self._online()
                self._deliver([(0, 16)])
                self.tab._refresh_cpu_topology(manual=True)
                self.assertTrue(_wait_until(lambda: len(calls) == 1))
                self.assertEqual(self.tab.cpu_status.text(), "Consultando processadores…")
                self.assertNotIn("16", " ".join(self._texts()))
                self.assertFalse(self.tab.cpu_query_btn.isEnabled())
                self.tab._refresh_cpu_topology(manual=True)
                self.assertEqual(calls, [1])
                release.set()
                self.assertTrue(
                    _wait_until(lambda: self.tab._cpu_state == "failed")
                )
        finally:
            release.set()
            self._cpu_patch.start()

    def test_shutdown_clears_password_and_drops_the_worker(self) -> None:
        seen: list[str] = []
        release = threading.Event()

        def _hang(_host, user="", password="", should_cancel=None, **_kwargs):
            seen.append(password)
            while not (should_cancel and should_cancel()):
                time.sleep(0.01)
            return None, ""

        self._cpu_patch.stop()
        worker = None
        try:
            with patch(
                "remoteops.ui.tabs.psexec.query_remote_cpu_topology",
                side_effect=_hang,
            ):
                self._online()
                self.tab._select_kind(AuthKind.LOCAL, clear_account=False)
                self.tab.user_edit.setText("ana")
                self.tab.pass_edit.setText(SECRET)
                self._stop_cpu_timer()
                self.tab._refresh_cpu_topology(manual=True)
                self.assertTrue(_wait_until(lambda: seen == [SECRET]))
                worker = self.tab._cpu_worker
                self.tab.shutdown(wait_ms=2000)
                QApplication.processEvents()
        finally:
            release.set()
            self._cpu_patch.start()
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertEqual(worker._password, "")
        self.assertNotIn(SECRET, "".join(seen[1:]))
        self.assertIsNone(self.tab._cpu_worker)

    def test_worker_does_not_keep_the_password_after_run(self) -> None:
        seen: list[str] = []

        def _fake(_host, user="", password="", should_cancel=None, **_kwargs):
            seen.append(password)
            return None, "Computador indisponível."

        with patch(
            "remoteops.ui.tabs.psexec.query_remote_cpu_topology",
            side_effect=_fake,
        ):
            worker = _CpuTopologyWorker(1, "PC01", user="ana", password=SECRET)
            worker.start()
            self.assertTrue(worker.wait(2000))
        QApplication.processEvents()
        self.assertEqual(seen, [SECRET])
        self.assertEqual(worker._password, "")
        worker.deleteLater()
        QApplication.processEvents()


class RealtimeRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def test_cancel_happens_before_the_network_precheck(self) -> None:
        from remoteops.ui.main_window import MainWindow

        with (
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
        ):
            window = MainWindow()
            try:
                tab = window.psexec_tab
                tab._host_status_timer.stop()
                tab.host_edit.setText("PC01")
                tab._host_status_timer.stop()
                tab._cpu_refresh_timer.stop()
                tab._host_online = True
                tab.remote_cmd_edit.setText("hostname")
                tab.priority_combo.setCurrentIndex(5)
                with (
                    patch.object(tab, "has_fresh_tcp445", return_value=False),
                    patch.object(tab, "start_tcp_precheck") as precheck,
                    patch(
                        "remoteops.ui.tabs.psexec.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No,
                    ) as question,
                ):
                    window.on_run()
                question.assert_called_once()
                precheck.assert_not_called()
                with (
                    patch.object(tab, "has_fresh_tcp445", return_value=False),
                    patch.object(tab, "start_tcp_precheck") as precheck,
                    patch(
                        "remoteops.ui.tabs.psexec.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes,
                    ),
                ):
                    window.on_run()
                precheck.assert_called_once()
            finally:
                window.close()
                QApplication.processEvents()
