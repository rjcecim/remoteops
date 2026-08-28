from __future__ import annotations

from collections.abc import Sequence
from weakref import WeakKeyDictionary

from PyQt6 import sip
from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QSpacerItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import (
    ANIM_CARD,
    CARD_GRID_VERTICAL_SPACING,
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_HOVER,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    FONT_UI,
    HEADER_BTN_SIZE,
    HEADER_HEIGHT,
    RADIUS_CARD,
    RADIUS_SMALL,
    SIZE_UI,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    anim_ms,
    animations_enabled,
)

# Limite padrão do Qt para "sem máximo"
_QWIDGETSIZE_MAX = 16777215


def make_field_label(text: str) -> QLabel:
    """Label padronizado para campos dentro de cards (mesma largura e estilo das abas)."""
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    lbl.setMinimumWidth(120)
    lbl.setStyleSheet(f"QLabel#fieldLabel {{ color: {COLOR_TEXT_SECONDARY}; }}")
    return lbl


def add_row(grid: QGridLayout, row: int, label_text: str, widget: QWidget) -> None:
    """Adiciona uma linha label + widget no grid do card (label centralizado na vertical)."""
    lbl = make_field_label(label_text)
    grid.addWidget(lbl, row, 0, Qt.AlignmentFlag.AlignVCenter)
    grid.addWidget(widget, row, 1, Qt.AlignmentFlag.AlignVCenter)


def add_row_full_width(grid: QGridLayout, row: int, widget: QWidget) -> None:
    """Adiciona um widget ocupando toda a largura da linha (ex.: checkbox sem label)."""
    grid.addWidget(widget, row, 0, 1, 2, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)


def grid_in_card(card: "CardWidget") -> QGridLayout:
    """Cria um QGridLayout padronizado dentro do card e retorna para adicionar linhas."""
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(CARD_GRID_VERTICAL_SPACING)
    grid.setColumnStretch(1, 1)
    card.content_layout.addLayout(grid)
    return grid


def make_card_stack(parent: QWidget) -> QVBoxLayout:
    """
    Layout padrão para empilhar cards: um abaixo do outro, sem vãos.

    Não usa ``AlignTop`` — no Qt isso sobrepõe widgets ao recolher.
    A sobra vertical é gerida por ``bind_card_stack`` (cards abertos ou
    spacer final somente quando todos estão recolhidos).
    """
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(SPACE_SM)
    # PyQt6: Qt.AlignmentFlag(0) limpa o alinhamento; nunca Qt.Alignment().
    layout.setAlignment(Qt.AlignmentFlag(0))
    return layout


class _CardStackBinding:
    """Estado idempotente de um stack de CardWidget (stretch + spacer)."""

    def __init__(self, layout: QVBoxLayout) -> None:
        self.layout = layout
        self.cards: list[CardWidget] = []
        self.fill = True
        self._connected: set[int] = set()
        self._spacer_item: QSpacerItem | None = None

    def rebind(self, cards: Sequence[CardWidget], *, fill: bool) -> None:
        self.fill = bool(fill)
        new_cards = list(cards)
        new_ids = {id(c) for c in new_cards}
        for card in self.cards:
            if id(card) in new_ids:
                continue
            self._disconnect_card(card)
        for card in new_cards:
            self._capture_stretch(card)
            self._connect_card(card)
        self.cards = new_cards
        self.apply()

    def _alive_cards(self) -> list[CardWidget]:
        alive: list[CardWidget] = []
        for card in self.cards:
            try:
                if sip.isdeleted(card):
                    self._connected.discard(id(card))
                    continue
            except RuntimeError:
                self._connected.discard(id(card))
                continue
            alive.append(card)
        self.cards = alive
        return alive

    def _capture_stretch(self, card: CardWidget) -> None:
        if card.layout_stretch > 0:
            return
        idx = self.layout.indexOf(card)
        if idx < 0:
            return
        current = self.layout.stretch(idx)
        if current > 0:
            card.set_layout_stretch(current)

    def _connect_card(self, card: CardWidget) -> None:
        cid = id(card)
        card._stack_bound = True
        if cid in self._connected:
            return
        card.collapsedChanged.connect(
            self._on_collapsed,
            Qt.ConnectionType.UniqueConnection,
        )
        self._connected.add(cid)

    def _disconnect_card(self, card: CardWidget) -> None:
        cid = id(card)
        self._connected.discard(cid)
        try:
            if sip.isdeleted(card):
                return
        except RuntimeError:
            return
        card._stack_bound = False
        try:
            card.collapsedChanged.disconnect(self._on_collapsed)
        except TypeError:
            pass

    def _on_collapsed(self, _collapsed: bool = False) -> None:
        self.apply()

    def apply(self) -> None:
        cards = self._alive_cards()
        if not self.fill:
            for card in cards:
                self._set_card_stretch(card, 0)
            self._set_tail_spacer(False)
            self._activate()
            return

        open_cards = [c for c in cards if not c.is_collapsed]
        for card in cards:
            if card.is_collapsed:
                self._set_card_stretch(card, 0)
            else:
                self._set_card_stretch(card, max(1, card.layout_stretch))
        self._set_tail_spacer(len(open_cards) == 0)
        self._activate()

    def _set_card_stretch(self, card: CardWidget, stretch: int) -> None:
        idx = self.layout.indexOf(card)
        if idx >= 0:
            self.layout.setStretch(idx, int(stretch))
        if card.is_collapsed:
            return
        card._apply_vertical_policies(absorb_extra=stretch > 0)

    def _find_spacer_index(self) -> int | None:
        if self._spacer_item is None:
            return None
        for i in range(self.layout.count()):
            item = self.layout.itemAt(i)
            if item is not None and item.spacerItem() is self._spacer_item:
                return i
        self._spacer_item = None
        return None

    def _set_tail_spacer(self, active: bool) -> None:
        layout = self.layout
        idx = self._find_spacer_index()
        if active:
            if idx is None:
                layout.addStretch(1)
                last = layout.itemAt(layout.count() - 1)
                self._spacer_item = last.spacerItem() if last is not None else None
                return
            if idx != layout.count() - 1:
                item = layout.takeAt(idx)
                layout.addItem(item)
                idx = layout.count() - 1
            layout.setStretch(idx, 1)
            return
        if idx is not None:
            layout.setStretch(idx, 0)

    def _activate(self) -> None:
        layout = self.layout
        layout.invalidate()
        layout.activate()
        parent = layout.parentWidget()
        if parent is not None:
            parent.updateGeometry()


_STACK_BINDINGS: WeakKeyDictionary[QVBoxLayout, _CardStackBinding] = (
    WeakKeyDictionary()
)


def bind_card_stack(
    layout: QVBoxLayout,
    cards: Sequence[CardWidget],
    *,
    fill: bool = True,
) -> None:
    """
    Liga um stack de ``CardWidget`` ao algoritmo padrão de stretch.

    Idempotente: chamadas repetidas não duplicam conexões nem spacers e
    não alteram os pesos gravados em ``layout_stretch``.

    * Cards abertos: stretch ``max(1, card.layout_stretch)`` (se ``fill``).
    * Cards recolhidos: stretch 0 (não absorvem sobra).
    * Todos recolhidos: spacer final ``addStretch(1)`` ativo.
    * ``fill=False``: nenhum card absorve sobra e o spacer fica inativo
      (útil em stacks mistos cuja sobra vai para outro widget).
    """
    binding = _STACK_BINDINGS.get(layout)
    if binding is None:
        binding = _CardStackBinding(layout)
        _STACK_BINDINGS[layout] = binding
    binding.rebind(cards, fill=fill)


class CardWidget(QWidget):
    """
    Widget de card com cabeçalho (ícone Unicode + título em negrito),
    linha divisória e área de conteúdo em grid.

    Por padrão a altura segue o conteúdo. O stack (``bind_card_stack``)
    pode dar altura extra ao card sem esticar o conteúdo interno.
    Use ``set_expanding`` somente quando o widget interno (tabela, log,
    editor de resultado) deve absorver essa sobra.
    """

    collapsedChanged = pyqtSignal(bool)
    downloadRequested = pyqtSignal()
    copyRequested = pyqtSignal()
    resetRequested = pyqtSignal()
    runRequested = pyqtSignal()
    stopRequested = pyqtSignal()

    def __init__(self, icon_char: str, title: str, parent=None):
        super().__init__(parent)
        self._setup_style()
        self._is_collapsible = False
        self._is_collapsed = False
        self._wants_expanding = False
        self._stack_bound = False
        # 0 = peso padrão; no stack o stretch efetivo aberto é max(1, valor).
        self._layout_stretch = 0
        self._divider_spacing_idx: int | None = None
        self._container_tail_idx: int | None = None
        self._collapse_anim: QPropertyAnimation | None = None
        self._anim_hint_h: int | None = None
        self._collapse_target: bool | None = None
        # Padrão: altura = conteúdo; cards ficam empilhados sem se espalhar
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Container interno com fundo e bordas arredondadas via stylesheet
        self._container = QWidget()
        self._container.setObjectName("cardContainer")
        self._container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(SPACE_LG, SPACE_SM, SPACE_LG, SPACE_MD)
        self._container_layout.setSpacing(0)

        # Cabeçalho (altura fixa para todos os cards ficarem iguais)
        self._header_widget = QWidget()
        self._header_widget.setObjectName("cardHeader")
        self._header_widget.setFixedHeight(HEADER_HEIGHT)
        header = QHBoxLayout(self._header_widget)
        header.setSpacing(6)
        header.setContentsMargins(0, 0, 0, 0)

        self._icon_label = QLabel(icon_char)
        self._icon_label.setObjectName("cardIcon")
        icon_font = QFont()
        icon_font.setFamily("Segoe MDL2 Assets")
        icon_font.setPointSize(13)
        self._icon_label.setFont(icon_font)
        self._icon_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._title_label = QLabel(title)
        self._title_label.setObjectName("cardTitle")
        title_font = QFont(FONT_UI, SIZE_UI)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        self._title_label.setWordWrap(False)
        self._title_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

        self._download_btn = QToolButton()
        self._download_btn.setObjectName("cardDownload")
        self._download_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._download_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._download_btn.setAutoRaise(True)
        self._download_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._download_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._download_btn.setText("\uE896")  # Download
        self._download_btn.setToolTip("Baixar informações deste card")
        self._download_btn.clicked.connect(self.downloadRequested.emit)
        self._download_btn.hide()

        self._run_btn = QToolButton()
        self._run_btn.setObjectName("cardRun")
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._run_btn.setAutoRaise(True)
        self._run_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._run_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._run_btn.setText("\uE768")  # Play
        self._run_btn.setToolTip("Executar")
        self._run_btn.clicked.connect(self.runRequested.emit)
        self._run_btn.hide()

        self._stop_btn = QToolButton()
        self._stop_btn.setObjectName("cardStop")
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._stop_btn.setAutoRaise(True)
        self._stop_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._stop_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._stop_btn.setText("\uE71A")  # Stop
        self._stop_btn.setToolTip("Encerrar sessão (exit)")
        self._stop_btn.clicked.connect(self.stopRequested.emit)
        self._stop_btn.hide()

        self._copy_btn = QToolButton()
        self._copy_btn.setObjectName("cardCopy")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._copy_btn.setAutoRaise(True)
        self._copy_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._copy_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._copy_btn.setText("\uE8C8")  # Copy
        self._copy_btn.setToolTip("Copiar para a área de transferência")
        self._copy_btn.clicked.connect(self.copyRequested.emit)
        self._copy_btn.hide()

        self._reset_btn = QToolButton()
        self._reset_btn.setObjectName("cardReset")
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._reset_btn.setAutoRaise(True)
        self._reset_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._reset_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._reset_btn.setText("\uE777")  # Reset
        self._reset_btn.setToolTip("Restaurar padrões deste card")
        self._reset_btn.clicked.connect(self.resetRequested.emit)
        self._reset_btn.hide()

        self._toggle_btn = QToolButton()
        self._toggle_btn.setObjectName("cardToggle")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._toggle_btn.setAutoRaise(True)
        self._toggle_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._toggle_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._toggle_btn.clicked.connect(self.toggle_collapsed)
        self._toggle_btn.hide()

        header.addWidget(self._icon_label)
        header.addWidget(self._title_label)
        header.addStretch()
        header.addWidget(self._download_btn)
        header.addWidget(self._run_btn)
        header.addWidget(self._stop_btn)
        header.addWidget(self._copy_btn)
        header.addWidget(self._reset_btn)
        header.addWidget(self._toggle_btn)

        self._container_layout.addWidget(self._header_widget)

        # Linha divisória
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.Shape.HLine)
        self._divider.setObjectName("cardDivider")
        self._divider.setFixedHeight(1)
        self._container_layout.addWidget(self._divider)
        self._container_layout.addSpacing(2)
        self._divider_spacing_idx = self._container_layout.count() - 1

        # Área de conteúdo — o chamador adiciona widgets aqui
        self._content_widget = QWidget()
        self.content_layout = QVBoxLayout(self._content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(CARD_GRID_VERTICAL_SPACING)
        self._container_layout.addWidget(self._content_widget)
        # Stretch interno: formulários empilham o conteúdo no topo do card
        # quando o stack dá altura extra. Cards com set_expanding desligam isto.
        self._container_layout.addStretch(0)
        self._container_tail_idx = self._container_layout.count() - 1

        outer.addWidget(self._container)

    def set_collapsible(self, collapsible: bool = True, collapsed: bool = False) -> None:
        self._is_collapsible = bool(collapsible)
        self._toggle_btn.setVisible(self._is_collapsible)
        if self._is_collapsible:
            self.set_collapsed(bool(collapsed))

    def set_downloadable(self, downloadable: bool = True) -> None:
        self._download_btn.setVisible(bool(downloadable))

    def set_copyable(self, copyable: bool = True) -> None:
        self._copy_btn.setVisible(bool(copyable))

    def set_runnable(self, runnable: bool = True) -> None:
        """Mostra Executar/Parar no cabeçalho, no mesmo tamanho dos demais ícones."""
        visible = bool(runnable)
        self._run_btn.setVisible(visible)
        self._stop_btn.setVisible(visible)

    @property
    def run_button(self) -> QToolButton:
        return self._run_btn

    @property
    def stop_button(self) -> QToolButton:
        return self._stop_btn

    def set_resettable(self, resettable: bool = True, tooltip: str = "") -> None:
        """Mostra o botão de restaurar no cabeçalho, ao lado de recolher/expandir."""
        self._reset_btn.setVisible(bool(resettable))
        if tooltip:
            self._reset_btn.setToolTip(tooltip)

    def make_header_button(
        self,
        icon_char: str,
        tooltip: str = "",
        *,
        object_name: str = "cardHeaderAction",
    ) -> QToolButton:
        """Botão de ícone MDL2 no mesmo tamanho dos demais do título do card."""
        btn = QToolButton()
        btn.setObjectName(object_name)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setAutoRaise(True)
        btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        btn.setFont(QFont("Segoe MDL2 Assets", 10))
        btn.setText(icon_char)
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def add_header_button(self, button: QToolButton) -> None:
        """Insere um botão no título, à esquerda de reset/recolher."""
        header = self._header_widget.layout()
        if header is None:
            return
        idx = header.indexOf(self._reset_btn)
        if idx < 0:
            idx = header.indexOf(self._toggle_btn)
        if idx < 0:
            idx = header.count()
        header.insertWidget(idx, button)

    def set_expanding(self, expanding: bool = True) -> None:
        """Faz a área de conteúdo interna absorver o espaço extra do card.

        Não use em formulários só para o card preencher a aba — o stack
        já pode dar altura ao ``CardWidget`` sem esticar campos.
        """
        self._wants_expanding = bool(expanding)
        if self._is_collapsed:
            return
        self._apply_vertical_policies(absorb_extra=self._wants_expanding)

    def _apply_vertical_policies(self, *, absorb_extra: bool) -> None:
        """Política do card no stack vs. expansão do conteúdo interno."""
        if self._is_collapsed:
            return
        v_card = (
            QSizePolicy.Policy.Expanding
            if (absorb_extra or self._wants_expanding)
            else QSizePolicy.Policy.Maximum
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, v_card)
        self._container.setSizePolicy(QSizePolicy.Policy.Expanding, v_card)
        if self._wants_expanding:
            self._content_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            self._container_layout.setStretchFactor(self._content_widget, 1)
            self._set_container_tail_stretch(0)
            self.content_layout.setAlignment(Qt.AlignmentFlag(0))
        else:
            self._content_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            self._container_layout.setStretchFactor(self._content_widget, 0)
            self._set_container_tail_stretch(1 if absorb_extra else 0)
            self.content_layout.setAlignment(Qt.AlignmentFlag(0))

    def _set_container_tail_stretch(self, stretch: int) -> None:
        idx = self._container_tail_idx
        if idx is None:
            return
        self._container_layout.setStretch(idx, max(0, int(stretch)))

    def set_layout_stretch(self, stretch: int) -> None:
        """Guarda o stretch no layout pai (usado ao expandir de novo após minimizar)."""
        self._layout_stretch = max(0, int(stretch))

    @property
    def layout_stretch(self) -> int:
        return self._layout_stretch

    @property
    def is_collapsed(self) -> bool:
        return bool(self._is_collapsed)

    def _header_only_height(self) -> int:
        """Altura só do cabeçalho (cards minimizados / padrão recolhido)."""
        m = self._container_layout.contentsMargins()
        header_h = max(self._header_widget.height(), self._header_widget.sizeHint().height())
        # +2 ≈ borda do container
        return header_h + m.top() + m.bottom() + 2

    def sizeHint(self) -> QSize:  # noqa: N802
        if self._anim_hint_h is not None:
            base = super().sizeHint()
            return QSize(base.width(), self._anim_hint_h)
        if self._is_collapsible and self._is_collapsed:
            base = super().sizeHint()
            return QSize(base.width(), self._header_only_height())
        # Flags (FlowLayout): altura depende da largura — sizeHint “seco” subestima.
        if self.hasHeightForWidth():
            width = self.width() if self.width() > 0 else super().sizeHint().width()
            return QSize(max(1, width), self.heightForWidth(width))
        return super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        if self._anim_hint_h is not None:
            base = super().minimumSizeHint()
            return QSize(base.width(), self._anim_hint_h)
        if self._is_collapsible and self._is_collapsed:
            base = super().minimumSizeHint()
            return QSize(base.width(), self._header_only_height())
        if self.hasHeightForWidth():
            width = self.width() if self.width() > 0 else super().minimumSizeHint().width()
            return QSize(max(1, width), self.heightForWidth(width))
        return super().minimumSizeHint()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        if self._anim_hint_h is not None:
            return False
        if self._is_collapsible and self._is_collapsed:
            return False
        lay = self.layout()
        return bool(lay is not None and lay.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if self._anim_hint_h is not None:
            return self._anim_hint_h
        if self._is_collapsible and self._is_collapsed:
            return self._header_only_height()
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            return lay.heightForWidth(max(1, width))
        return super().sizeHint().height()

    def set_collapsed(self, collapsed: bool) -> None:
        if not self._is_collapsible:
            self._is_collapsed = False
            self._content_widget.setVisible(True)
            self._divider.setVisible(True)
            self._set_divider_spacing_visible(True)
            self._toggle_btn.hide()
            return

        collapsed = bool(collapsed)
        self._toggle_btn.setText("\uE70E" if collapsed else "\uE70D")
        self._toggle_btn.setToolTip("Expandir" if collapsed else "Ocultar")

        animate = (
            animations_enabled()
            and self.isVisible()
            and not self._wants_expanding
            and collapsed != self._is_collapsed
        )
        if not animate:
            self._stop_collapse_anim()
            self._apply_collapsed_state(collapsed)
            self.collapsedChanged.emit(self._is_collapsed)
            self._notify_geometry()
            return

        self._animate_collapsed(collapsed)

    def toggle_collapsed(self) -> None:
        if not self._is_collapsible:
            return
        if self._collapse_anim is not None and self._collapse_target is not None:
            self.set_collapsed(not self._collapse_target)
            return
        self.set_collapsed(not self._is_collapsed)

    def _stop_collapse_anim(self) -> None:
        if self._collapse_anim is not None:
            self._collapse_anim.stop()
            self._collapse_anim = None
        self._anim_hint_h = None
        self._content_widget.setMaximumHeight(_QWIDGETSIZE_MAX)

    def _content_target_height(self) -> int:
        lay = self._content_widget.layout()
        width = max(1, self._content_widget.width(), self.width() - 24)
        hint = self._content_widget.sizeHint().height()
        if lay is not None and lay.hasHeightForWidth():
            hint = max(hint, lay.heightForWidth(width))
        return max(1, hint)

    def _composed_height(self, content_h: int) -> int:
        extra = 0
        if content_h > 0:
            extra = 1 + 2 + int(content_h)
        return self._header_only_height() + extra

    def _animate_collapsed(self, collapsed: bool) -> None:
        self._stop_collapse_anim()
        self._collapse_target = collapsed
        content = self._content_widget
        if collapsed:
            start = max(0, content.height())
            end = 0
            content.setVisible(True)
            self._divider.setVisible(True)
            self._set_divider_spacing_visible(True)
            content.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            content.setMaximumHeight(start)
        else:
            self._is_collapsed = False
            content.setVisible(True)
            self._divider.setVisible(True)
            self._set_divider_spacing_visible(True)
            content.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            start = 0
            end = self._content_target_height()
            content.setMaximumHeight(0)

        ms = anim_ms(ANIM_CARD)
        if ms <= 0 or start == end:
            self._apply_collapsed_state(collapsed)
            self.collapsedChanged.emit(self._is_collapsed)
            self._notify_geometry()
            return

        self._anim_hint_h = self._composed_height(start)
        anim = QPropertyAnimation(content, b"maximumHeight", self)
        anim.setDuration(ms)
        anim.setStartValue(int(start))
        anim.setEndValue(int(end))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(self._on_collapse_anim_value)
        anim.finished.connect(lambda: self._on_collapse_anim_finished(collapsed))
        self._collapse_anim = anim
        anim.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def _on_collapse_anim_value(self, value) -> None:
        try:
            h = int(value)
        except (TypeError, ValueError):
            h = int(self._content_widget.maximumHeight())
        self._anim_hint_h = self._composed_height(h)
        self._propagate_geometry()

    def _on_collapse_anim_finished(self, collapsed: bool) -> None:
        self._collapse_anim = None
        self._anim_hint_h = None
        self._content_widget.setMaximumHeight(_QWIDGETSIZE_MAX)
        self._apply_collapsed_state(collapsed)
        self.collapsedChanged.emit(self._is_collapsed)
        self._notify_geometry()

    def _apply_collapsed_state(self, collapsed: bool) -> None:
        self._is_collapsed = bool(collapsed)
        self._content_widget.setVisible(not self._is_collapsed)
        self._divider.setVisible(not self._is_collapsed)
        self._set_divider_spacing_visible(not self._is_collapsed)
        self._toggle_btn.setText("\uE70E" if self._is_collapsed else "\uE70D")
        self._toggle_btn.setToolTip("Expandir" if self._is_collapsed else "Ocultar")

        # Nunca setFixedHeight: com teto da aba (ContentSizedTabWidget) isso
        # comprime o layout e os cards se sobrepõem. Maximum + sizeHint bastam.
        self.setMinimumHeight(0)
        self.setMaximumHeight(_QWIDGETSIZE_MAX)

        if self._is_collapsed:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            self._container.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            self._content_widget.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
            )
            self._container_layout.setStretchFactor(self._content_widget, 0)
            self._set_container_tail_stretch(0)
            if not self._stack_bound:
                self._apply_parent_stretch(0)
        else:
            self._apply_vertical_policies(absorb_extra=self._wants_expanding)
            if not self._stack_bound:
                self._apply_parent_stretch(self._layout_stretch)

    def _notify_geometry(self) -> None:
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().invalidate()
            parent.layout().activate()
            parent.updateGeometry()

    def _propagate_geometry(self) -> None:
        self.updateGeometry()
        w = self.parentWidget()
        depth = 0
        while w is not None and depth < 8:
            if w.layout() is not None:
                w.layout().invalidate()
                w.updateGeometry()
            if isinstance(w, (QTabWidget, QMainWindow)):
                w.updateGeometry()
                break
            w = w.parentWidget()
            depth += 1

    def _set_divider_spacing_visible(self, visible: bool) -> None:
        idx = self._divider_spacing_idx
        if idx is None:
            return
        item = self._container_layout.itemAt(idx)
        if item is not None and item.spacerItem() is not None:
            # spacer não tem setVisible; altura 0 / 2 via changeSize
            sp = item.spacerItem()
            if visible:
                sp.changeSize(0, 2, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            else:
                sp.changeSize(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            self._container_layout.invalidate()

    def _apply_parent_stretch(self, stretch: int) -> None:
        if self._stack_bound:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        lay = parent.layout()
        if lay is None:
            return
        idx = lay.indexOf(self)
        if idx < 0:
            return
        # Se ainda não gravamos o stretch (ex.: addWidget depois de set_expanding),
        # captura o atual antes de zerar.
        if stretch == 0 and self._layout_stretch <= 0:
            current = lay.stretch(idx)
            if current > 0:
                self._layout_stretch = current
        lay.setStretch(idx, stretch)

    def _setup_style(self):
        r_btn = RADIUS_SMALL
        self.setStyleSheet(f"""
            QWidget#cardContainer {{
                background-color: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_CARD}px;
            }}
            QWidget#cardHeader {{
                background: transparent;
                border-radius: {r_btn}px;
            }}
            QWidget#cardHeader:hover {{
                background: {COLOR_HOVER};
            }}
            QLabel#cardIcon {{
                color: {COLOR_ACCENT};
            }}
            QLabel#cardTitle {{
                color: {COLOR_TEXT};
            }}
            QToolButton#cardToggle {{
                border: none;
                background: transparent;
                color: {COLOR_TEXT_SECONDARY};
            }}
            QToolButton#cardToggle:hover {{
                background: {COLOR_HOVER};
                border-radius: {r_btn}px;
                color: {COLOR_TEXT};
            }}
            QToolButton#cardDownload, QToolButton#cardCopy, QToolButton#cardRun,
            QToolButton#cardStop, QToolButton#cardHeaderAction, QToolButton#cardReset {{
                border: none;
                background: transparent;
                color: {COLOR_ACCENT};
            }}
            QToolButton#cardDownload:hover, QToolButton#cardCopy:hover,
            QToolButton#cardRun:hover, QToolButton#cardStop:hover,
            QToolButton#cardHeaderAction:hover, QToolButton#cardReset:hover {{
                background: {COLOR_HOVER};
                border-radius: {r_btn}px;
            }}
            QToolButton#cardDownload:disabled, QToolButton#cardRun:disabled,
            QToolButton#cardStop:disabled, QToolButton#cardHeaderAction:disabled {{
                color: {COLOR_TEXT_MUTED};
                background: transparent;
            }}
            QFrame#cardDivider {{
                color: {COLOR_BORDER};
                background-color: {COLOR_BORDER};
                border: none;
            }}
        """)
