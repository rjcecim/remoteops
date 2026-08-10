"""QTabWidget cuja altura segue apenas a aba atual."""

from __future__ import annotations

from PyQt6.QtCore import QSize
from PyQt6.QtWidgets import QSizePolicy, QStyle, QTabWidget

_QWIDGETSIZE_MAX = 16777215


class ContentSizedTabWidget(QTabWidget):
    """
    Abas com altura = conteúdo da página atual.

    O QTabWidget padrão usa o máximo entre todas as páginas; com isso, CMD e
    PowerShell herdavam a altura do PsExec e a sobra ficava dentro da aba.
    Aqui a sobra vai para Pré-visualização e Log — igual à aba principal.

    Em modo preenchimento (tela cheia), a aba pode crescer. Ao sair desse modo,
    a altura é limitada ao sizeHint da página atual — senão o Qt mantém a
    altura Expandida anterior e aparece um vão vazio abaixo do formulário.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fill_available = False
        self.currentChanged.connect(self._on_current_changed)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def set_fill_available(self, fill: bool) -> None:
        """True = ocupar o espaço vertical (PsInfo/Configurações/…); False = altura do conteúdo."""
        self._fill_available = bool(fill)
        self.setMinimumHeight(0)
        if self._fill_available:
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
        else:
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            self.sync_content_height()

    def sync_content_height(self) -> None:
        """Recalcula o teto de altura após trocar aba ou recolher/expandir cards."""
        if self._fill_available:
            return
        # Liberar teto antigo antes de medir — senão o sizeHint fica engessado.
        self.setMinimumHeight(0)
        self.setMaximumHeight(_QWIDGETSIZE_MAX)
        self.updateGeometry()
        self.setMaximumHeight(max(1, self.sizeHint().height()))

    def _on_current_changed(self, _index: int = 0) -> None:
        if not self._fill_available:
            self.sync_content_height()
        else:
            self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().activate()
            parent.updateGeometry()

    def _chrome_height(self) -> int:
        bar = self.tabBar()
        bar_h = bar.sizeHint().height() if bar is not None else 0
        m = self.contentsMargins()
        frame = 0
        if not self.documentMode():
            frame = 2 * self.style().pixelMetric(
                QStyle.PixelMetric.PM_DefaultFrameWidth, None, self
            )
        return bar_h + m.top() + m.bottom() + frame

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        page = self.currentWidget()
        if page is None:
            return hint
        return QSize(hint.width(), page.sizeHint().height() + self._chrome_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        page = self.currentWidget()
        if page is None:
            return hint
        return QSize(
            hint.width(), page.minimumSizeHint().height() + self._chrome_height()
        )
