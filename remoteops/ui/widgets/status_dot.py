"""Indicador visual de status (bolinha colorida) compartilhado pela UI."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

# Paleta alinhada ao status de conexão (PsExec) e demais legendas.
STATUS_COLORS = {
    "idle": "#9AA0A6",
    "checking": "#F9AB00",
    "warn": "#F9AB00",
    "ok": "#34A853",
    "online": "#34A853",
    "err": "#EA4335",
    "offline": "#EA4335",
    "invalid": "#E8710A",
}


class StatusDot(QWidget):
    """Bolinha colorida de status (online/offline/ok/erro/…)."""

    def __init__(self, parent=None, diameter: int = 10):
        super().__init__(parent)
        self._color = QColor(STATUS_COLORS["idle"])
        self.setFixedSize(diameter, diameter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def set_state(self, state: str) -> None:
        self.set_color(STATUS_COLORS.get(state, STATUS_COLORS["idle"]))

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(0, 0, self.width(), self.height())
        painter.end()
