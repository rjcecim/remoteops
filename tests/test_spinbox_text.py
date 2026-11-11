"""O valor do StepSpinBox permanece visível com o QSS global."""

from __future__ import annotations

import sys
import unittest

from PyQt6.QtWidgets import QApplication

from remoteops.ui.widgets.spinbox import StepSpinBox

_QSS = (
    "QSpinBox { border: 1px solid #ccc; padding: 0 6px; "
    "min-height: 30px; max-height: 30px; }"
)


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


class StepSpinBoxTextTests(unittest.TestCase):
    def test_value_keeps_a_readable_width_under_stylesheet(self) -> None:
        app = _ensure_app()
        previous = app.styleSheet()
        app.setStyleSheet(_QSS)
        spin = StepSpinBox()
        try:
            spin.setRange(5, 300)
            spin.setSuffix(" s")
            spin.setValue(15)
            spin.setFixedWidth(max(spin.sizeHint().width(), 72))
            spin.show()
            app.processEvents()
            edit = spin.lineEdit()
            self.assertIsNotNone(edit)
            self.assertIn("15", edit.text())
            self.assertGreaterEqual(
                edit.width(),
                edit.fontMetrics().horizontalAdvance(edit.text()),
            )
            spin.setValue(300)
            app.processEvents()
            self.assertGreaterEqual(
                edit.width(),
                edit.fontMetrics().horizontalAdvance(edit.text()),
            )
        finally:
            spin.close()
            app.setStyleSheet(previous)


if __name__ == "__main__":
    unittest.main()
