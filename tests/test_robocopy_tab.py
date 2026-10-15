"""UI da aba Robocopy: destino, switches, /R /W e reset."""

from __future__ import annotations

import sys
import unittest

from PyQt6.QtWidgets import QApplication

from remoteops.core.robocopy_dest import DEFAULT_RETRIES, DEFAULT_WAIT_SECONDS


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app  # type: ignore[return-value]


class RobocopyTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.ui.tabs.robocopy import RobocopyTab

        self.tab = RobocopyTab()

    def tearDown(self) -> None:
        self.tab.close()
        self.tab.deleteLater()

    def test_default_dest_and_switches(self) -> None:
        params = self.tab.get_params()
        self.assertEqual(params["dest"], "C:\\temp")
        for flag in ("/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP"):
            self.assertIn(flag, params["switches"])
        self.assertEqual(params["retries"], DEFAULT_RETRIES)
        self.assertEqual(params["wait"], DEFAULT_WAIT_SECONDS)
        self.assertEqual(len(self.tab.switches), 7)
        self.assertTrue(all(cb.isChecked() for cb in self.tab.switches))

    def test_dest_validation_feedback(self) -> None:
        self.tab.dest_edit.setText(r"D:\temp")
        self.assertIn("C:", self.tab.dest_status.text())
        self.assertEqual(self.tab.dest_unc.text(), "")
        self.tab.dest_edit.setText(r"C:\temp\scripts")
        self.assertIn("compartilhamento C$", self.tab.dest_status.text())
        self.assertIn(r"C$\temp\scripts", self.tab.dest_unc.text())

    def test_reset_destino(self) -> None:
        self.tab.dest_edit.setText(r"C:\other")
        self.tab._reset_card_destino()
        self.assertEqual(self.tab.dest_edit.text(), "C:\\temp")

    def test_reset_parametros(self) -> None:
        for cb in self.tab.switches:
            cb.setChecked(False)
        self.tab.retries_combo.setCurrentIndex(0)
        self.tab.wait_combo.setCurrentIndex(0)
        self.tab._reset_card_parametros()
        self.assertTrue(all(cb.isChecked() for cb in self.tab.switches))
        self.assertEqual(self.tab.get_params()["retries"], DEFAULT_RETRIES)
        self.assertEqual(self.tab.get_params()["wait"], DEFAULT_WAIT_SECONDS)
        self.assertIn("7 de 7", self.tab.switches_count.text())

    def test_preview_host_updates_unc(self) -> None:
        self.tab.set_preview_host("PC01")
        self.tab.dest_edit.setText("C:\\temp")
        self.assertEqual(
            self.tab.dest_status.text(),
            "Destino formatado para o compartilhamento C$",
        )
        self.assertEqual(self.tab.dest_unc.text(), r"\\PC01\C$\temp")

    def test_switch_descriptions_match_mockup(self) -> None:
        expected = {
            "/NFL": "Não listar arquivos",
            "/NDL": "Não listar diretórios",
            "/NJH": "Ocultar cabeçalho",
            "/NJS": "Ocultar resumo",
            "/NC": "Ocultar classes",
            "/NS": "Ocultar tamanhos",
            "/NP": "Ocultar progresso",
        }
        self.assertEqual(dict(self.tab.switch_labels), expected)

    def test_two_cards_only(self) -> None:
        self.assertEqual(len(self.tab._form_cards), 2)


if __name__ == "__main__":
    unittest.main()
