"""Bootstrap da aplicação Qt (entry point limpo)."""

from __future__ import annotations

import multiprocessing
import sys
import traceback

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from remoteops.ui.branding import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    ORG_NAME,
    app_icon,
)
from remoteops.ui.main_window import MainWindow
from remoteops.ui.mica import enable_mica_for_widget
from remoteops.ui.style import apply_ui_defaults
from remoteops.utils.redaction import redact_command_text


class StreamToLog:
    """Redireciona stdout/stderr para o log da UI, com redação de senhas."""

    def __init__(self, log_func):
        self.log_func = log_func

    def write(self, msg):
        msg = str(msg)
        if msg and not msg.isspace():
            self.log_func(redact_command_text(msg.rstrip()))

    def flush(self):
        pass


def run(argv: list[str] | None = None) -> int:
    """Inicializa QApplication e a janela principal. Retorna código de saída."""
    multiprocessing.freeze_support()

    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    apply_ui_defaults(app)
    QCoreApplication.setOrganizationName(ORG_NAME)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(APP_VERSION)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)

    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    window = MainWindow()
    enable_mica_for_widget(window)

    sys.stdout = StreamToLog(window.log_output.append_log)
    sys.stderr = StreamToLog(window.log_output.append_log)

    def excepthook(exc_type, value, tb):
        lines = traceback.format_exception(exc_type, value, tb)
        window.log_output.append_log(redact_command_text("".join(lines)))

    sys.excepthook = excepthook
    window.show()
    return int(app.exec())
