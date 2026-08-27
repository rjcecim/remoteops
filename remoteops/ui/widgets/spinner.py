from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QLabel, QWidget

from remoteops.ui.style import COLOR_TEXT_SECONDARY, SIZE_UI_SMALL


class LoadingEllipsisLabel(QLabel):
    """Texto ``Carregando`` com reticências animadas (``.``, ``..``, ``...``)."""

    def __init__(
        self,
        parent=None,
        *,
        base_text: str = "Carregando",
        interval_ms: int = 420,
    ):
        super().__init__(parent)
        self._base = (base_text or "Carregando").rstrip(".")
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(max(200, int(interval_ms)))
        self._timer.timeout.connect(self._tick)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {SIZE_UI_SMALL}pt;"
            " background: transparent; border: none;"
        )
        self._apply_text()

    def set_base_text(self, text: str) -> None:
        self._base = (text or "Carregando").rstrip(".")
        self._apply_text()

    def start(self) -> None:
        self._phase = 0
        self._apply_text()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:
        self.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 4
        self._apply_text()

    def _apply_text(self) -> None:
        # 0 → sem pontos; 1–3 → ".", "..", "..."
        dots = "." * self._phase
        # Largura estável evita “pulo” do layout ao animar.
        self.setText(f"{self._base}{dots:<3}")


class DotsSpinner(QWidget):
    """
    Spinner de bolinhas em círculo (sem assets externos).
    """

    def __init__(self, parent=None, dot_count: int = 10, interval_ms: int = 80):
        super().__init__(parent)
        self._dot_count = max(6, int(dot_count))
        self._index = 0
        self._timer = QTimer(self)
        self._timer.setInterval(max(30, int(interval_ms)))
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(34, 34)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        if self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:
        self.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self._index = (self._index + 1) % self._dot_count
        self.update()

    def paintEvent(self, _event) -> None:
        size = min(self.width(), self.height())
        r = size / 2.0
        center = (self.width() / 2.0, self.height() / 2.0)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        # Cor baseada no highlight do tema; fallback para azul
        col = self.palette().color(self.palette().ColorRole.Highlight)
        base = QColor(col) if col.isValid() else QColor(0, 120, 212)

        # Geometria das bolinhas
        dot_r = max(2.2, size * 0.075)
        ring_r = r - dot_r - 1

        for i in range(self._dot_count):
            # Fase: dot atual mais forte; os demais decaem
            dist = (i - self._index) % self._dot_count
            # alpha decai de ~220 até ~40
            alpha = int(max(40, 220 * (1.0 - (dist / self._dot_count))))
            c = QColor(base)
            c.setAlpha(alpha)
            painter.setBrush(c)

            angle = (i / self._dot_count) * 6.283185307179586  # 2*pi
            x = center[0] + ring_r * __import__("math").cos(angle)
            y = center[1] + ring_r * __import__("math").sin(angle)
            painter.drawEllipse(int(x - dot_r), int(y - dot_r), int(2 * dot_r), int(2 * dot_r))

        painter.end()

