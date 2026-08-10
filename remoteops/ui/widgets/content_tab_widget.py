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

    Não usa ``setMaximumHeight`` como teto do formulário: isso clipava cards
    (ex.: Conexão) ao recolher Flags. A política Maximum + sizeHint bastam;
    o mínimo impede o layout de esmagar o formulário sobrepondo cards.
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
        self.setMaximumHeight(_QWIDGETSIZE_MAX)
        if self._fill_available:
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
        else:
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            self.sync_content_height()

    def sync_content_height(self) -> None:
        """Recalcula geometria após trocar aba ou recolher/expandir cards."""
        if self._fill_available:
            return
        # Libera qualquer teto antigo (não recoloca setMaximumHeight).
        self.setMinimumHeight(0)
        self.setMaximumHeight(_QWIDGETSIZE_MAX)

        page = self.currentWidget()
        if page is not None:
            lay = page.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()
            page.updateGeometry()
        self.updateGeometry()

        # Impede o layout pai de comprimir a aba abaixo do formulário real
        # (causa sobreposição Conexão ↔ Autenticação ao recolher Flags).
        self.setMinimumHeight(max(1, self._content_height()))

    def _content_height(self) -> int:
        page = self.currentWidget()
        chrome = self._chrome_height()
        if page is None:
            return max(1, chrome)
        lay = page.layout()
        width = max(page.width(), self.width(), 1)
        if lay is not None:
            if hasattr(lay, "hasHeightForWidth") and lay.hasHeightForWidth():
                page_h = lay.heightForWidth(width)
            else:
                page_h = 0
            page_h = max(
                page_h,
                lay.totalSizeHint().height(),
                lay.totalMinimumSize().height(),
                page.sizeHint().height(),
                page.minimumSizeHint().height(),
            )
        else:
            page_h = max(page.sizeHint().height(), page.minimumSizeHint().height())
        # +4 evita clipar a última linha do card superior (ex.: Conexão) por arredondamento
        return page_h + chrome + 4

    def _on_current_changed(self, _index: int = 0) -> None:
        if not self._fill_available:
            self.sync_content_height()
        else:
            self.setMinimumHeight(0)
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
        return QSize(hint.width(), self._content_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(hint.width(), self._content_height())

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        page = self.currentWidget()
        if page is None:
            return False
        lay = page.layout()
        return bool(lay is not None and lay.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        page = self.currentWidget()
        chrome = self._chrome_height()
        if page is None:
            return chrome
        lay = page.layout()
        if lay is not None and lay.hasHeightForWidth():
            return lay.heightForWidth(max(1, width)) + chrome
        return self._content_height()
