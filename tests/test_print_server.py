"""Normalização UNC do servidor de impressão (UI / settings.ini / uso interno)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication

import remoteops.utils.app_settings as app_settings
import remoteops.utils.printer_settings as printer_settings
from remoteops.utils.printer_settings import (
    normalize_print_server,
    parse_print_server_input,
)
from remoteops.utils.printers import print_server_host, print_server_unc


def _ensure_qt_app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    return app


class PrintServerNormalizeTests(unittest.TestCase):
    def test_hostname_becomes_unc(self) -> None:
        self.assertEqual(parse_print_server_input("printserver"), r"\\printserver")

    def test_unc_stays_unc(self) -> None:
        self.assertEqual(parse_print_server_input(r"\\printserver"), r"\\printserver")

    def test_empty_stays_empty(self) -> None:
        self.assertEqual(parse_print_server_input(""), "")
        self.assertEqual(parse_print_server_input("   "), "")
        self.assertEqual(parse_print_server_input("\\"), "")
        self.assertEqual(parse_print_server_input("\\\\"), "")
        self.assertEqual(normalize_print_server(""), "")

    def test_repeated_normalization_never_doubles_slashes(self) -> None:
        once = parse_print_server_input("printserver")
        twice = parse_print_server_input(once)
        thrice = parse_print_server_input(print_server_unc(twice))
        self.assertEqual(once, r"\\printserver")
        self.assertEqual(twice, r"\\printserver")
        self.assertEqual(thrice, r"\\printserver")
        self.assertNotIn(r"\\\\printserver", once)
        self.assertEqual(once.count("\\"), 2)

    def test_internal_use_strips_unc_to_hostname(self) -> None:
        self.assertEqual(print_server_host(r"\\printserver"), "printserver")
        self.assertEqual(print_server_host("printserver"), "printserver")
        self.assertEqual(print_server_host(""), "")


class PrintServerSettingsPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_qt_app()
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._ini = self._root / "settings.ini"
        printer_settings._runtime_server = None
        printer_settings._runtime_timeout = None
        self._patches = [
            patch.object(app_settings, "get_settings_path", return_value=self._ini),
            patch.object(app_settings, "get_app_dir", return_value=self._root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        printer_settings._runtime_server = None
        printer_settings._runtime_timeout = None
        self._tmp.cleanup()

    def test_load_legacy_hostname_displays_unc(self) -> None:
        self._ini.write_text("[printers]\nserver=printserver\n", encoding="utf-8")
        printer_settings._runtime_server = None
        self.assertEqual(printer_settings.get_print_server(), r"\\printserver")

    def test_save_persists_unc_in_settings_ini(self) -> None:
        saved = printer_settings.set_print_server("printserver")
        self.assertEqual(saved, r"\\printserver")

        text = self._ini.read_text(encoding="utf-8").replace("\r\n", "\n")
        # No arquivo: exatamente duas barras (não o escape quádruplo do QSettings).
        self.assertIn("server=\\\\printserver\n", text)
        self.assertNotIn("server=\\\\\\\\printserver", text)

        printer_settings._runtime_server = None
        self.assertEqual(printer_settings.get_print_server(), r"\\printserver")

    def test_save_empty_persists_empty(self) -> None:
        printer_settings.set_print_server(r"\\printserver")
        saved = printer_settings.set_print_server("")
        self.assertEqual(saved, "")
        printer_settings._runtime_server = None
        self.assertEqual(printer_settings.get_print_server(), "")


if __name__ == "__main__":
    unittest.main()
