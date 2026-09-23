"""Instalação MSI em lote: comando, abas existentes, status e compatibilidade EXE."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from remoteops.core.builder import CommandBuilder
from remoteops.services.batch_install import (
    ACTION_SKIP,
    INSTALLER_SUCCESS_CODES,
    RESULT_ERROR,
    RESULT_INSTALLED,
    RESULT_SKIPPED,
    RESULT_UPDATED,
    BatchHostRow,
    RemoteInstallOutcome,
    build_batch_install_spec,
    decide_host_action,
)
from remoteops.services.batch_msi import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CONNECTION,
    STATUS_COPY_ERROR,
    STATUS_INSTALLER,
    STATUS_PERMISSION,
    STATUS_REBOOT_REQUIRED,
    STATUS_REBOOT_STARTED,
    STATUS_WAITING,
    MsiHostRow,
    apply_msi_execution_to_batch,
    apply_msi_install_outcome,
    build_batch_msi_plan,
    classify_msi_exit,
    classify_transport_error,
    count_tab_titles,
    preview_msiexec_command,
    run_msi_host,
    selected_online_rows,
)
from remoteops.services.msi_validate import (
    MSI_CFB_MAGIC,
    validate_msi_file,
    validate_msi_params,
    validate_msi_property_tokens,
)
from remoteops.utils.product_identity import ProductIdentity


def _write_fake_msi(path: str, payload: bytes = b"\x00" * 32) -> str:
    with open(path, "wb") as handle:
        handle.write(MSI_CFB_MAGIC + payload)
    return path


def _msi_params(**overrides) -> dict:
    params = {
        "enable": True,
        "action": "/i",
        "interface": "/qn",
        "restart": "/norestart",
        "log": False,
        "log_file": "",
        "repair": "",
        "update": "",
    }
    params.update(overrides)
    return params


def _ok_copy(*_args, **_kwargs) -> RemoteInstallOutcome:
    return RemoteInstallOutcome(ok=True, return_code=1, message="")


def _ok_install(code: int = 0) -> RemoteInstallOutcome:
    return RemoteInstallOutcome(
        ok=True,
        return_code=code,
        psexec_ok=True,
        installer_ok=code in INSTALLER_SUCCESS_CODES,
    )


class MsiValidationTests(unittest.TestCase):
    def test_rejects_non_msi_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "setup.exe")
            Path(path).write_bytes(MSI_CFB_MAGIC + b"x")
            errors = validate_msi_file(path)
        self.assertTrue(any("Extensão" in err for err in errors))

    def test_rejects_missing_magic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "setup.msi")
            Path(path).write_bytes(b"not-an-msi")
            errors = validate_msi_file(path)
        self.assertTrue(any("assinatura" in err.lower() for err in errors))

    def test_accepts_cfb_msi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "setup.msi"))
            self.assertEqual(validate_msi_file(path), [])

    def test_rejects_property_injection(self) -> None:
        valid, errors = validate_msi_property_tokens("ALLUSERS=1 & calc.exe")
        self.assertEqual(valid, ["ALLUSERS=1"])
        self.assertTrue(errors)

    def test_accepts_property_pairs(self) -> None:
        valid, errors = validate_msi_property_tokens(
            'ALLUSERS=1 INSTALLDIR="C:\\Program Files\\App"'
        )
        self.assertEqual(errors, [])
        self.assertIn("ALLUSERS=1", valid)
        self.assertTrue(any(token.startswith("INSTALLDIR=") for token in valid))

    def test_rejects_unknown_action(self) -> None:
        errors = validate_msi_params(_msi_params(action="/evil"))
        self.assertTrue(errors)


class MsiexecCommandTests(unittest.TestCase):
    def test_preview_uses_ui_qn_norestart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "App Setup.msi"))
            preview = preview_msiexec_command(
                msi_path=path,
                msi_params=_msi_params(),
                robocopy_params={"dest": "temp"},
            )
        self.assertIn("msiexec", preview.lower())
        self.assertIn("/i", preview)
        self.assertIn("/qn", preview)
        self.assertIn("/norestart", preview)
        self.assertIn("App Setup.msi", preview)

    def test_qb_and_qf_follow_user_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            qb = preview_msiexec_command(
                msi_path=path,
                msi_params=_msi_params(interface="/qb"),
                robocopy_params={"dest": "temp"},
            )
            qf = preview_msiexec_command(
                msi_path=path,
                msi_params=_msi_params(interface="/qf", restart="/forcerestart"),
                robocopy_params={"dest": "temp"},
            )
        self.assertIn("/qb", qb)
        self.assertNotIn("/qn", qb)
        self.assertIn("/qf", qf)
        self.assertIn("/forcerestart", qf)

    def test_spaces_in_path_stay_single_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Pasta com espaço")
            os.makedirs(folder, exist_ok=True)
            path = _write_fake_msi(os.path.join(folder, "App Setup.msi"))
            plan = build_batch_msi_plan(
                host="HOST A",
                msi_path=path,
                msi_params=_msi_params(),
                robocopy_params={"dest": "temp\\lote msi"},
                psexec_params={"user": "dom\\admin", "has_password": True},
                pstools_path="",
                has_password=True,
            )
        self.assertFalse(plan.errors)
        self.assertIn("App Setup.msi", plan.robocopy.argv)
        self.assertTrue(any("App Setup.msi" in arg for arg in plan.psexec.argv))
        self.assertTrue(any("lote msi" in arg for arg in plan.robocopy.argv))
        self.assertNotIn("calc.exe", " ".join(plan.psexec.argv))

    def test_properties_appear_in_msiexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            preview = preview_msiexec_command(
                msi_path=path,
                msi_params=_msi_params(update="ALLUSERS=1 REBOOT=ReallySuppress"),
                robocopy_params={"dest": "temp"},
            )
        self.assertIn("ALLUSERS=1", preview)
        self.assertIn("REBOOT=ReallySuppress", preview)

    def test_individual_msi_plan_still_uses_robocopy_then_psexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            builder = CommandBuilder()
            builder.set_file_selection({"mode": "file", "file": path, "folder": None})
            builder.set_msi_params(_msi_params())
            builder.set_robocopy_params({"dest": "temp", "switches": "/NFL"})
            builder.set_psexec_params({"host": "PC1", "psexec_path": "", "remote_cmd": ""})
            plan = builder.build_execution_plan()
        kinds = [(spec.metadata or {}).get("kind") for spec in plan]
        self.assertEqual(kinds[0], "robocopy")
        self.assertIn("psexec", kinds[1] or "psexec")
        self.assertTrue(any("msiexec" in arg.lower() for arg in plan[1].argv))

    def test_injection_is_stripped_from_builder(self) -> None:
        builder = CommandBuilder()
        builder.set_file_selection({"mode": "file", "file": r"C:\temp\app.msi", "folder": None})
        builder.set_msi_params(
            _msi_params(update="ALLUSERS=1 & notepad.exe")
        )
        builder.set_robocopy_params({"dest": "temp"})
        builder.set_psexec_params({"host": "PC1", "psexec_path": "", "remote_cmd": ""})
        argv = builder._build_msiexec_argv()
        self.assertIn("ALLUSERS=1", argv)
        self.assertFalse(any("notepad" in str(part).lower() for part in argv))


class MsiExitCodeTests(unittest.TestCase):
    def test_success_codes(self) -> None:
        self.assertEqual(classify_msi_exit(0), STATUS_COMPLETED)
        self.assertEqual(classify_msi_exit(3010), STATUS_REBOOT_REQUIRED)
        self.assertEqual(classify_msi_exit(1641), STATUS_REBOOT_STARTED)
        self.assertEqual(classify_msi_exit(1603), STATUS_INSTALLER)
        self.assertIn(0, INSTALLER_SUCCESS_CODES)
        self.assertIn(3010, INSTALLER_SUCCESS_CODES)
        self.assertIn(1641, INSTALLER_SUCCESS_CODES)

    def test_apply_reboot_required_is_not_failure(self) -> None:
        row = MsiHostRow(host="PC1")
        apply_msi_install_outcome(row, _ok_install(3010))
        self.assertEqual(row.status, STATUS_REBOOT_REQUIRED)
        self.assertNotEqual(row.status, STATUS_INSTALLER)

    def test_apply_reboot_started_is_not_failure(self) -> None:
        row = MsiHostRow(host="PC1")
        apply_msi_install_outcome(row, _ok_install(1641))
        self.assertEqual(row.status, STATUS_REBOOT_STARTED)


class MsiHostFlowTests(unittest.TestCase):
    def _plan(self, path: str) -> object:
        return build_batch_msi_plan(
            host="PC1",
            msi_path=path,
            msi_params=_msi_params(),
            robocopy_params={"dest": "temp"},
            psexec_params={},
            pstools_path="",
            has_password=False,
        )

    def test_install_phase_starts_only_after_copy(self) -> None:
        phases: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            run_msi_host(
                MsiHostRow(host="PC1"),
                plan,
                copy_runner=_ok_copy,
                install_runner=lambda *_a, **_k: _ok_install(0),
                cleanup_runner=lambda *_a, **_k: None,
                on_phase=lambda phase, _display: phases.append(phase),
            )
        self.assertEqual(phases, ["copy", "install"])

    def test_copy_failure_stops_before_psexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            installed = []
            phases: list[str] = []
            row = run_msi_host(
                MsiHostRow(host="PC1"),
                plan,
                copy_runner=lambda *_a, **_k: RemoteInstallOutcome(
                    ok=False, return_code=8, message="Robocopy retornou código 8"
                ),
                install_runner=lambda *_a, **_k: installed.append(True)
                or _ok_install(0),
                cleanup_runner=lambda *_a, **_k: None,
                on_phase=lambda phase, _display: phases.append(phase),
            )
        self.assertEqual(row.status, STATUS_COPY_ERROR)
        self.assertEqual(installed, [])
        self.assertEqual(phases, ["copy"])

    def test_connection_and_permission_errors(self) -> None:
        self.assertEqual(
            classify_transport_error("Couldn't access PC1", 53),
            STATUS_CONNECTION,
        )
        self.assertEqual(
            classify_transport_error("Access is denied", 5),
            STATUS_PERMISSION,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            row = run_msi_host(
                MsiHostRow(host="PC1"),
                plan,
                copy_runner=lambda *_a, **_k: RemoteInstallOutcome(
                    ok=False, message="couldn't access", return_code=53
                ),
                install_runner=lambda *_a, **_k: _ok_install(0),
                cleanup_runner=lambda *_a, **_k: None,
            )
        self.assertEqual(row.status, STATUS_CONNECTION)

    def test_install_failure_keeps_temp_msi(self) -> None:
        cleaned = []
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            row = run_msi_host(
                MsiHostRow(host="PC1"),
                plan,
                copy_runner=_ok_copy,
                install_runner=lambda *_a, **_k: RemoteInstallOutcome(
                    ok=False,
                    return_code=1603,
                    psexec_ok=True,
                    installer_ok=False,
                    message="Instalador retornou código 1603",
                ),
                cleanup_runner=lambda *_a, **_k: cleaned.append(True),
            )
        self.assertEqual(row.status, STATUS_INSTALLER)
        self.assertEqual(cleaned, [])

    def test_success_cleans_temp_and_keeps_logs(self) -> None:
        cleaned = []
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            row = run_msi_host(
                MsiHostRow(host="PC1"),
                plan,
                copy_runner=_ok_copy,
                install_runner=lambda *_a, **_k: RemoteInstallOutcome(
                    ok=True,
                    return_code=0,
                    psexec_ok=True,
                    installer_ok=True,
                    stdout="=== verbose msi log ===",
                ),
                cleanup_runner=lambda *_a, **_k: cleaned.append(True),
            )
        self.assertEqual(row.status, STATUS_COMPLETED)
        self.assertEqual(cleaned, [True])
        self.assertIn("verbose msi log", row.logs)

    def test_cancel_marks_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            row = run_msi_host(
                MsiHostRow(host="PC1"),
                plan,
                should_cancel=lambda: True,
                copy_runner=_ok_copy,
                install_runner=lambda *_a, **_k: _ok_install(0),
                cleanup_runner=lambda *_a, **_k: None,
            )
        self.assertEqual(row.status, STATUS_CANCELLED)

    def test_parallel_hosts_respect_limit(self) -> None:
        current = 0
        peak = 0
        lock = threading.Lock()

        def install(*_args, **_kwargs):
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.05)
            with lock:
                current -= 1
            return _ok_install(0)

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_fake_msi(os.path.join(tmp, "app.msi"))
            plan = self._plan(path)
            rows = [MsiHostRow(host=f"PC{i}") for i in range(4)]

            def work(row: MsiHostRow) -> None:
                run_msi_host(
                    row,
                    plan,
                    copy_runner=_ok_copy,
                    install_runner=install,
                    cleanup_runner=lambda *_a, **_k: None,
                )

            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(work, rows))
        self.assertLessEqual(peak, 2)
        self.assertTrue(all(row.status == STATUS_COMPLETED for row in rows))

    def test_selected_online_rows(self) -> None:
        rows = {
            "a": MsiHostRow(host="A", selected=True, online=True, status=STATUS_WAITING),
            "b": MsiHostRow(host="B", selected=False, online=True, status=STATUS_WAITING),
            "c": MsiHostRow(host="C", selected=True, online=False, status=STATUS_CONNECTION),
        }
        pending = selected_online_rows(rows)
        self.assertEqual([row.host for row in pending], ["A"])


class ExeBatchCompatibilityTests(unittest.TestCase):
    def test_exe_spec_does_not_use_robocopy(self) -> None:
        spec = build_batch_install_spec(
            host="PC1",
            exe_path=r"C:\temp\setup.exe",
            psexec_params={"-c": True, "user": "u"},
            pstools_path="",
            has_password=False,
        )
        joined = " ".join(spec.argv).lower()
        self.assertNotIn("robocopy", joined)
        self.assertNotIn("msiexec", joined)

    def test_exe_decision_still_skips_current_version(self) -> None:
        identity = ProductIdentity(
            label="App",
            needles=("app",),
            installer_version="1.0.0",
        )
        row = decide_host_action(
            host="PC1",
            desired_version="1.0.0",
            online=False,
            inventory=None,
            identity=identity,
        )
        self.assertEqual(row.reason, "Offline ou inacessível")
        self.assertFalse(row.needs_install)

    def test_msi_results_row_matches_exe_columns(self) -> None:
        from remoteops.utils.psinfo import HostInventoryStatus, InstalledApp

        identity = ProductIdentity(
            label="RustDesk",
            needles=("RustDesk",),
            installer_version="1.4.5",
        )
        inventory = HostInventoryStatus(
            host="ETSECEX-3CCG39",
            ok=True,
            apps=[
                InstalledApp(
                    display_name="RustDesk",
                    version="1.4.5",
                    publisher="",
                    display_line="RustDesk",
                    product_code="",
                    uninstall_string="",
                    quiet_uninstall_string="",
                    is_msi=True,
                    arch="64",
                )
            ],
        )
        row = decide_host_action(
            host="ETSECEX-3CCG39",
            desired_version="1.4.5",
            online=True,
            inventory=inventory,
            identity=identity,
        )
        self.assertEqual(
            row.as_tuple(),
            (
                "ETSECEX-3CCG39",
                "RustDesk (64-bit)",
                "1.4.5",
                "1.4.5",
                ACTION_SKIP,
                RESULT_SKIPPED,
                "Versão já atual",
            ),
        )
        self.assertFalse(row.needs_install)

    def test_msi_success_maps_to_exe_result_columns(self) -> None:
        batch = BatchHostRow(
            host="PC1",
            app_found="—",
            action="Instalar",
            needs_install=True,
        )
        msi = MsiHostRow(host="PC1", status=STATUS_COMPLETED, exit_code=0)
        apply_msi_execution_to_batch(batch, msi)
        self.assertEqual(batch.result, RESULT_INSTALLED)
        self.assertFalse(batch.needs_install)

        updated = BatchHostRow(
            host="PC2",
            is_update=True,
            action="Atualizar",
            needs_install=True,
        )
        apply_msi_execution_to_batch(
            updated,
            MsiHostRow(host="PC2", status=STATUS_REBOOT_REQUIRED, exit_code=3010),
        )
        self.assertEqual(updated.result, RESULT_UPDATED)

        failed = BatchHostRow(host="PC3", needs_install=True)
        apply_msi_execution_to_batch(
            failed,
            MsiHostRow(host="PC3", status=STATUS_COPY_ERROR, message="Falha na cópia"),
        )
        self.assertEqual(failed.result, RESULT_ERROR)
        self.assertEqual(failed.reason, "Falha na cópia")


class MsiTabUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_msi_tab_keeps_params_without_batch_controls(self) -> None:
        from remoteops.ui.tabs.msi import MsiTab

        tab = MsiTab()
        self.assertTrue(hasattr(tab, "action_combo"))
        self.assertFalse(hasattr(tab, "scan_btn"))
        self.assertFalse(hasattr(tab, "start_btn"))
        self.assertEqual(count_tab_titles([tab.tr("MSI")])["MSI"], 1)
        self.assertEqual(tab.action_combo.currentText(), "/i")
        self.assertEqual(tab.interface_combo.currentText(), "/qn")
        self.assertEqual(tab.restart_combo.currentText(), "/norestart")
        tab.set_msi_path(r"C:\temp\App Setup.msi")
        self.assertIn("App Setup.msi", tab.file_lbl.text())
        self.assertIn("/qn", tab.current_preview_command())
        tab.deleteLater()

    def test_batch_msi_tab_is_customized_not_exe_copy(self) -> None:
        from remoteops.ui.tabs.batchmsi import BatchMsiTab

        tab = BatchMsiTab()
        self.assertTrue(hasattr(tab, "scan_btn"))
        self.assertTrue(hasattr(tab, "start_btn"))
        self.assertTrue(hasattr(tab, "table"))
        self.assertTrue(hasattr(tab, "version_edit"))
        self.assertFalse(hasattr(tab, "action_combo"))
        self.assertEqual(tab.table.columnCount(), 7)
        self.assertEqual(
            [tab.table.horizontalHeaderItem(i).text() for i in range(7)],
            [
                "Computador/IP",
                "Aplicativo",
                "Versão",
                "Versão desejada",
                "Ação",
                "Resultado",
                "Motivo",
            ],
        )
        self.assertEqual(tab._output_tabs.count(), 2)
        self.assertEqual(tab._output_tabs.tabText(0), "Mensagens")
        self.assertEqual(tab._output_tabs.tabText(1), "Console de Saída")
        tab.version_edit.setText("4.10.0")
        self.assertEqual(tab.desired_version(), "4.10.0")
        tab.set_msi_path(r"C:\temp\App Setup.msi")
        self.assertIn("App Setup.msi", tab.file_lbl.text())
        tab.deleteLater()

    def test_msi_lote_installs_one_host_at_a_time(self) -> None:
        from remoteops.services.batch_install import RESULT_INSTALLED, BatchHostRow
        from remoteops.ui.tabs.batchmsi import _BatchMsiWorker

        identity = ProductIdentity(label="App", needles=("App",))
        rows = [
            BatchHostRow(host="B", order=2, needs_install=True),
            BatchHostRow(host="A", order=1, needs_install=True),
        ]
        worker = _BatchMsiWorker(
            [],
            msi_path=r"C:\temp\app.msi",
            msi_params={},
            robocopy_params={},
            psexec_params={},
            user="",
            password="",
            identity=identity,
            pending_rows=rows,
            max_workers=8,
        )
        current = 0
        peak = 0
        seen: list[str] = []

        def fake(row: BatchHostRow) -> None:
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            time.sleep(0.04)
            seen.append(row.host)
            current -= 1
            row.result = RESULT_INSTALLED
            row.needs_install = False

        worker._install_one = fake  # type: ignore[method-assign]
        worker._run_installs(list(rows))
        self.assertEqual(peak, 1)
        self.assertEqual(seen, ["A", "B"])
        worker.deleteLater()

    def test_main_window_opens_lote_tab_for_msi_and_exe(self) -> None:
        from remoteops.ui.main_window import MainWindow

        win = MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            msi = _write_fake_msi(os.path.join(tmp, "setup.msi"))
            exe = os.path.join(tmp, "setup.exe")
            Path(exe).write_bytes(b"MZ")
            win.file_selector.set_file(msi)
            win.on_file_selected({"mode": "file", "file": msi, "folder": None})
            titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
            widgets = [win.tabs.widget(i) for i in range(win.tabs.count())]
            self.assertEqual(titles.count("MSI"), 1)
            self.assertEqual(titles.count("Instalação em Lote"), 1)
            self.assertEqual(titles.count("Robocopy"), 1)
            self.assertEqual(titles.count("PsExec"), 1)
            self.assertIs(win.tabs.widget(titles.index("MSI")), win.msi_tab)
            self.assertIs(
                win.tabs.widget(titles.index("Instalação em Lote")),
                win.batchmsi_tab,
            )
            self.assertNotIn(win.batchinstall_tab, widgets)

            win.file_selector.set_file(exe)
            win.on_file_selected({"mode": "file", "file": exe, "folder": None})
            exe_titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
            exe_widgets = [win.tabs.widget(i) for i in range(win.tabs.count())]
            self.assertEqual(exe_titles.count("Instalação em Lote"), 1)
            self.assertEqual(exe_titles.count("MSI"), 0)
            self.assertIs(
                win.tabs.widget(exe_titles.index("Instalação em Lote")),
                win.batchinstall_tab,
            )
            self.assertNotIn(win.batchmsi_tab, exe_widgets)
        win.close()
        win.deleteLater()


if __name__ == "__main__":
    unittest.main()
