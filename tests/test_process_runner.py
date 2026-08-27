"""Testes do process runner e helpers de win_cmd."""

from __future__ import annotations

import sys
import unittest

from remoteops.core.process_runner import CapturedProcess, run_argv_captured
from remoteops.core.win_cmd import CREATE_NO_WINDOW, run_captured


class WinCmdTests(unittest.TestCase):
    def test_create_no_window_defined(self) -> None:
        self.assertIsInstance(CREATE_NO_WINDOW, int)

    def test_run_captured_python(self) -> None:
        result = run_captured(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok')"],
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"ok")


class ProcessRunnerTests(unittest.TestCase):
    def test_empty_argv(self) -> None:
        captured = run_argv_captured([])
        self.assertIsInstance(captured, CapturedProcess)
        self.assertTrue(captured.spawn_error)

    def test_capture_stdout(self) -> None:
        captured = run_argv_captured(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'hello')"],
            timeout_s=10,
        )
        self.assertFalse(captured.spawn_error)
        self.assertFalse(captured.timed_out)
        self.assertFalse(captured.cancelled)
        self.assertEqual(captured.returncode, 0)
        self.assertEqual(captured.stdout, b"hello")

    def test_cancel(self) -> None:
        captured = run_argv_captured(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_s=30,
            should_cancel=lambda: True,
            poll_s=0.05,
        )
        self.assertTrue(captured.cancelled)
        self.assertFalse(captured.spawn_error)


if __name__ == "__main__":
    unittest.main()
