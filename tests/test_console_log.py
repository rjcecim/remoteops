"""LogOutputWidget: limpeza, cópia, histórico seguro, progresso e rolagem."""

from __future__ import annotations

import os
import sys
import unittest

from PyQt6.QtWidgets import QApplication


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class LogOutputWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_app()

    def setUp(self) -> None:
        from remoteops.ui.widgets.log import LogOutputWidget

        self.widget = LogOutputWidget()

    def tearDown(self) -> None:
        self.widget._batch_timer.stop()
        self.widget._batch.clear()
        self.widget.set_interactive(False)
        self.widget.hide()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_clear_is_visual_only(self) -> None:
        self.widget.append_log("linha 1")
        self.widget._flush_batch()
        self.widget.clear_log()
        self.assertEqual(self.widget.text_edit.toPlainText().strip(), "")

    def test_redacts_known_secret_in_append_and_partial(self) -> None:
        from remoteops.utils.redaction import REDACTED

        secret = "ConsoleSecretXYZ"
        self.widget.set_redaction_secrets([secret])
        self.widget.append_log(f"echo {secret}")
        self.widget._flush_batch()
        plain = self.widget.text_edit.toPlainText()
        self.assertNotIn(secret, plain)
        self.assertIn(REDACTED, plain)

        self.widget.set_partial_line(f"Password: {secret}")
        partial = self.widget.text_edit.toPlainText()
        self.assertNotIn(secret, partial)
        self.assertIn(REDACTED, partial)
        self.widget.clear_redaction_secrets()
        self.assertEqual(self.widget.redaction_secrets(), ())

    def test_redacts_p_flag_without_explicit_secret(self) -> None:
        from remoteops.utils.redaction import REDACTED

        self.widget.clear_redaction_secrets()
        self.widget.append_log('psexec \\\\h -u u -p "MyPass99" cmd')
        self.widget._flush_batch()
        plain = self.widget.text_edit.toPlainText()
        self.assertNotIn("MyPass99", plain)
        self.assertIn(REDACTED, plain)

    def test_copy_reapplies_redaction(self) -> None:
        from remoteops.utils.redaction import REDACTED

        secret = "CopySecretABC"
        self.widget.set_redaction_secrets([secret])
        # Simula texto antigo no documento (defesa no clipboard).
        self.widget.text_edit.setPlainText(f"leak {secret}")
        self.widget._copy_visible()
        clip = QApplication.clipboard().text()
        self.assertNotIn(secret, clip)
        self.assertIn(REDACTED, clip)

    def test_copy_selection_and_full(self) -> None:
        from PyQt6.QtGui import QTextCursor

        self.widget.append_log("alpha")
        self.widget.append_log("beta")
        self.widget._flush_batch()
        cursor = self.widget.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )
        self.widget.text_edit.setTextCursor(cursor)
        self.widget._copy_visible()
        clip = QApplication.clipboard().text()
        self.assertIn("alpha", clip)
        self.assertNotIn("beta", clip)

        cursor.clearSelection()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.widget.text_edit.setTextCursor(cursor)
        self.widget._copy_visible()
        clip = QApplication.clipboard().text()
        self.assertIn("alpha", clip)
        self.assertIn("beta", clip)

    def test_partial_line_then_commit(self) -> None:
        self.widget.set_partial_line("C:\\>")
        self.assertIn("C:\\>", self.widget.text_edit.toPlainText())
        self.widget.append_log("dir")
        self.widget._flush_batch()
        text = self.widget.text_edit.toPlainText()
        self.assertIn("dir", text)
        self.widget.set_partial_line("")
        self.assertIsNone(self.widget._partial_anchor)

    def test_history_skips_sensitive_prompt(self) -> None:
        self.widget.set_interactive(True)
        self.widget.set_partial_line("Password:")
        self.widget._input.setText("my-secret")
        self.widget._on_return()
        self.assertEqual(self.widget._history, [])

        self.widget.set_partial_line("C:\\>")
        self.widget._pending_sensitive_input = False
        self.widget._input.setText("dir")
        self.widget._on_return()
        self.assertEqual(self.widget._history, ["dir"])

    def test_history_cleared_when_session_ends(self) -> None:
        self.widget.set_interactive(True)
        self.widget._input.setText("whoami")
        self.widget._on_return()
        self.assertEqual(self.widget._history, ["whoami"])
        self.widget.set_interactive(False)
        self.assertEqual(self.widget._history, [])

    def test_spinner_chars_not_dropped(self) -> None:
        self.widget.append_log("-")
        self.widget.append_log("|")
        self.widget._flush_batch()
        plain = self.widget.text_edit.toPlainText()
        self.assertIn("-", plain)
        self.assertIn("|", plain)

    def test_input_ignored_when_not_interactive(self) -> None:
        submitted: list[str] = []
        self.widget.inputSubmitted.connect(submitted.append)
        self.widget.set_interactive(False)
        self.widget._input.setText("should-not-send")
        self.widget._on_return()
        self.assertEqual(submitted, [])

    def test_resize_dedup(self) -> None:
        sizes: list[tuple[int, int]] = []
        self.widget.consoleResized.connect(lambda c, r: sizes.append((c, r)))
        self.widget._last_console_size = (0, 0)
        self.widget._emit_console_size()
        first_len = len(sizes)
        self.widget._emit_console_size()
        self.assertEqual(len(sizes), first_len)


@unittest.skipUnless(os.name == "nt", "ConPTY chunk drain is Windows-oriented")
class ConPtyChunkDrainTests(unittest.TestCase):
    def test_take_pending_clears_chunks(self) -> None:
        from remoteops.core.conpty import ConPtySession

        session = ConPtySession(["cmd.exe", "/c", "echo ok"])
        with session._chunk_lock:
            session._output_chunks.append(b"hello")
            session._pending_bytes = 5
        raw = session._take_pending_bytes()
        self.assertEqual(raw, b"hello")
        with session._chunk_lock:
            self.assertEqual(len(session._output_chunks), 0)
            self.assertEqual(session._pending_bytes, 0)


if __name__ == "__main__":
    unittest.main()
