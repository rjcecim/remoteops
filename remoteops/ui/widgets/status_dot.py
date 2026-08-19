"""Indicador visual de status (bolinha colorida) compartilhado pela UI."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

from remoteops.ui.style import animations_enabled

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
        self._pulse = False
        self._pulse_phase = 0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(80)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self.setFixedSize(diameter, diameter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        checking = (color or "").lower() == STATUS_COLORS["checking"].lower()
        self._set_pulse(checking)
        self.update()

    def set_state(self, state: str) -> None:
        self.set_color(STATUS_COLORS.get(state, STATUS_COLORS["idle"]))

    def _set_pulse(self, active: bool) -> None:
        self._pulse = bool(active) and animations_enabled()
        if self._pulse:
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self._pulse_phase = 0

    def _tick_pulse(self) -> None:
        self._pulse_phase = (self._pulse_phase + 1) % 20
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._pulse_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._pulse and not self._pulse_timer.isActive():
            self._pulse_timer.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        color = QColor(self._color)
        if self._pulse:
            # 0.45–1.0 sem bounce; só em "checking"
            t = abs(10 - self._pulse_phase) / 10.0
            color.setAlpha(int(115 + 140 * t))
        painter.setBrush(color)
        painter.drawEllipse(0, 0, self.width(), self.height())
        painter.end()
