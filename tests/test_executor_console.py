"""Executor: redação em pipes, limpeza de senhas e emissão ConPTY."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class ExecutorPipeRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.core.executor import Executor

        self.executor = Executor()

    def tearDown(self) -> None:
        self.executor.shutdown(wait=False)
        self.app.processEvents()

    def test_pipe_stdout_is_redacted(self) -> None:
        from remoteops.core.models import ExecutionResult
        from remoteops.utils.redaction import REDACTED

        secret = "PipeSecretXYZ"
        self.executor._passwords = [secret]

        class FakeProc:
            returncode = 0
            stdout = MagicMock()
            stderr = MagicMock()

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        def fake_popen(*_a, **_k):
            proc = FakeProc()
            proc.stdout.readline = MagicMock(
                side_effect=[f"echo {secret}\n".encode("utf-8"), b""]
            )
            proc.stderr.readline = MagicMock(side_effect=[b""])
            return proc

        with patch("remoteops.core.executor.popen_argv", side_effect=fake_popen):
            result = self.executor._run_command(
                ["cmd.exe", "/c", "echo"],
                timeout=None,
                generation=self.executor._run_generation,
                use_conpty=False,
            )

        self.assertIsInstance(result, ExecutionResult)
        self.assertNotIn(secret, result.stdout or "")
        self.assertIn(REDACTED, result.stdout or "")
        self.assertEqual(self.executor._passwords, [])

    def test_clear_passwords_on_stop(self) -> None:
        self.executor._passwords = ["keep-me"]
        self.executor.stop()
        self.assertEqual(self.executor._passwords, [])


class EmitAliasTests(unittest.TestCase):
    def test_executor_reexports_emit(self) -> None:
        from remoteops.core import executor as ex
        from remoteops.core.console_stream import emit_conpty_text

        self.assertIs(ex._emit_conpty_text, emit_conpty_text)


if __name__ == "__main__":
    unittest.main()
