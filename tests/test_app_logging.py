"""Testes do logging unificado (uma rota via logging.Logger)."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from remoteops.utils import app_logging


class AppLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        app_logging.reset_logging_for_tests()

    def tearDown(self) -> None:
        app_logging.reset_logging_for_tests()

    def test_log_operation_writes_via_file_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            log_path = log_dir / app_logging.LOG_FILENAME

            with (
                patch.object(app_logging, "get_log_dir", return_value=str(log_dir)),
                patch.object(app_logging, "is_file_logging_enabled", return_value=True),
            ):
                app_logging._file_logging_enabled = True
                app_logging._file_logging_loaded = True
                app_logging.configure_logging()
                app_logging._ensure_file_handler()
                msg = app_logging.log_operation("TEST_OP", detail="sem senha")

                self.assertIn("TEST_OP", msg)
                self.assertTrue(log_path.is_file())
                text = log_path.read_text(encoding="utf-8")
                self.assertIn("TEST_OP", text)
                self.assertIn("INFO", text)

                handlers = [
                    h
                    for h in logging.getLogger("remoteops").handlers
                    if isinstance(h, logging.FileHandler)
                ]
                self.assertEqual(len(handlers), 1)
                # Fecha handlers antes de apagar o TemporaryDirectory (Windows).
                app_logging.reset_logging_for_tests()

    def test_disabled_skips_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            log_path = log_dir / app_logging.LOG_FILENAME
            with patch.object(app_logging, "get_log_dir", return_value=str(log_dir)):
                app_logging._file_logging_enabled = False
                app_logging._file_logging_loaded = True
                app_logging.configure_logging()
                app_logging.append_history("não deve gravar")
            self.assertFalse(log_path.exists())


if __name__ == "__main__":
    unittest.main()
