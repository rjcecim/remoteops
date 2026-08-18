"""Aba CMD — opções reais do cmd.exe, sem estados mutuamente exclusivos."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QRadioButton,
    QSizePolicy,
    QWidget,
)

from remoteops.core.cmd_options import (
    ENC_ANSI,
    ENC_SYSTEM,
    ENC_UNICODE,
    MODE_C,
    MODE_K,
    TOOLTIPS,
    TRI_OFF,
    TRI_ON,
    TRI_SYSTEM,
    CmdOptions,
    compute_cmd_option_state,
    options_to_params,
    sanitize_cmd_options,
)
from remoteops.ui.style import (
    CARD_GRID_VERTICAL_SPACING,
    FONT_MONO,
    INPUT_HEIGHT,
    SIZE_MONO,
    SIZE_UI_SMALL,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    grid_in_card,
    make_card_stack,
)


def _caption(object_name: str) -> QLabel:
    lbl = QLabel()
    lbl.setObjectName(object_name)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        f"QLabel#{object_name} {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
    )
    lbl.setContentsMargins(2, 0, 0, 0)
    return lbl


class CmdTab(QWidget):
    formLayoutChanged = pyqtSignal()
    optionsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._updating = False
        vbox = make_card_stack(self)

        card_opts = CardWidget("\uE115", self.tr("Opções"))
        g1 = grid_in_card(card_opts)
        row = 0

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(16)
        self.mode_c = QRadioButton(self.tr("Executar e encerrar (/C)"))
        self.mode_k = QRadioButton(self.tr("Manter sessão aberta (/K)"))
        self.mode_c.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.mode_c, 0)
        self._mode_group.addButton(self.mode_k, 1)
        mode_row.addWidget(self.mode_c)
        mode_row.addWidget(self.mode_k)
        mode_row.addStretch()
        mode_wrap = QWidget()
        mode_wrap.setLayout(mode_row)
        add_row(g1, row, self.tr("Modo"), mode_wrap)
        row += 1

        flags_row = QHBoxLayout()
        flags_row.setContentsMargins(0, 0, 0, 0)
        flags_row.setSpacing(16)
        self.d_checkbox = QCheckBox(self.tr("/D  AutoRun desligado"))
        self.d_checkbox.setChecked(True)
        self.q_checkbox = QCheckBox(self.tr("/Q  Sem echo"))
        flags_row.addWidget(self.d_checkbox)
        flags_row.addWidget(self.q_checkbox)
        flags_row.addStretch()
        flags_wrap = QWidget()
        flags_wrap.setLayout(flags_row)
        add_row(g1, row, self.tr("Switches"), flags_wrap)
        row += 1

        self.extensions_combo = QComboBox()
        self.extensions_combo.addItem(self.tr("Padrão do sistema"), TRI_SYSTEM)
        self.extensions_combo.addItem(self.tr("Ativadas (/E:ON)"), TRI_ON)
        self.extensions_combo.addItem(self.tr("Desativadas (/E:OFF)"), TRI_OFF)
        add_row(g1, row, self.tr("Extensões"), self.extensions_combo)
        row += 1

        self.delayed_combo = QComboBox()
        self.delayed_combo.addItem(self.tr("Padrão do sistema"), TRI_SYSTEM)
        self.delayed_combo.addItem(self.tr("Ativada (/V:ON)"), TRI_ON)
        self.delayed_combo.addItem(self.tr("Desativada (/V:OFF)"), TRI_OFF)
        add_row(g1, row, self.tr("Expansão atrasada"), self.delayed_combo)
        row += 1

        self.completion_combo = QComboBox()
        self.completion_combo.addItem(self.tr("Padrão do sistema"), TRI_SYSTEM)
        self.completion_combo.addItem(self.tr("Ativada (/F:ON)"), TRI_ON)
        self.completion_combo.addItem(self.tr("Desativada (/F:OFF)"), TRI_OFF)
        add_row(g1, row, self.tr("Conclusão Tab"), self.completion_combo)
        row += 1

        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem(self.tr("Padrão"), ENC_SYSTEM)
        self.encoding_combo.addItem(self.tr("ANSI (/A)"), ENC_ANSI)
        self.encoding_combo.addItem(self.tr("Unicode (/U)"), ENC_UNICODE)
        add_row(g1, row, self.tr("Saída interna"), self.encoding_combo)

        vbox.addWidget(card_opts)

        card_cmd = CardWidget("\uE768", self.tr("Comando"))
        g2 = grid_in_card(card_cmd)
        self.command_edit = QPlainTextEdit()
        self.command_edit.setPlaceholderText(
            self.tr("Cadeia CMD: whoami, dir \"C:\\Program Files\", echo A && echo B…")
        )
        self.command_edit.setFont(QFont(FONT_MONO, SIZE_MONO))
        self.command_edit.setTabChangesFocus(True)
        self.command_edit.setMinimumHeight(INPUT_HEIGHT * 3)
        self.command_edit.setMaximumHeight(INPUT_HEIGHT * 8)
        self.command_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.command_edit.setStyleSheet(
            "QPlainTextEdit { border: 1px solid palette(mid); border-radius: 4px; padding: 6px; }"
            "QPlainTextEdit:focus { border-color: palette(highlight); }"
        )
        add_row(g2, 0, self.tr("Comando"), self.command_edit)
        self._cmd_caption = _caption("cmdChainCaption")
        g2.addWidget(self._cmd_caption, 1, 1, Qt.AlignmentFlag.AlignTop)
        g2.setVerticalSpacing(CARD_GRID_VERTICAL_SPACING)

        vbox.addWidget(card_cmd)

        self._form_cards = (card_opts, card_cmd)
        for card, on_reset in zip(
            self._form_cards,
            (self._reset_card_opcoes, self._reset_card_comando),
        ):
            card.set_collapsible(True, collapsed=False)
            card.set_resettable(True, self.tr("Restaurar padrões deste card"))
            card.resetRequested.connect(on_reset)
            card.collapsedChanged.connect(self._on_form_card_collapsed)

        self._connect_signals()
        self.update_cmd_option_state()

    def _connect_signals(self) -> None:
        self._mode_group.buttonClicked.connect(lambda _btn: self.update_cmd_option_state())
        self.d_checkbox.stateChanged.connect(lambda: self.update_cmd_option_state())
        self.q_checkbox.stateChanged.connect(lambda: self.update_cmd_option_state())
        self.extensions_combo.currentIndexChanged.connect(
            lambda: self.update_cmd_option_state()
        )
        self.delayed_combo.currentIndexChanged.connect(
            lambda: self.update_cmd_option_state()
        )
        self.completion_combo.currentIndexChanged.connect(
            lambda: self.update_cmd_option_state()
        )
        self.encoding_combo.currentIndexChanged.connect(
            lambda: self.update_cmd_option_state()
        )
        self.command_edit.textChanged.connect(self.update_cmd_option_state)

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    def _reset_card_opcoes(self) -> None:
        self._updating = True
        try:
            self.mode_c.setChecked(True)
            self.d_checkbox.setChecked(True)
            self.q_checkbox.setChecked(False)
            self.extensions_combo.setCurrentIndex(0)
            self.delayed_combo.setCurrentIndex(0)
            self.completion_combo.setCurrentIndex(0)
            self.encoding_combo.setCurrentIndex(0)
        finally:
            self._updating = False
        self.update_cmd_option_state()

    def _reset_card_comando(self) -> None:
        self.command_edit.clear()
        self.update_cmd_option_state()

    def snapshot_options(self) -> CmdOptions:
        return sanitize_cmd_options(
            CmdOptions(
                mode=MODE_K if self.mode_k.isChecked() else MODE_C,
                disable_autorun=self.d_checkbox.isChecked(),
                quiet=self.q_checkbox.isChecked(),
                extensions=self.extensions_combo.currentData() or TRI_SYSTEM,
                delayed_expansion=self.delayed_combo.currentData() or TRI_SYSTEM,
                completion=self.completion_combo.currentData() or TRI_SYSTEM,
                encoding=self.encoding_combo.currentData() or ENC_SYSTEM,
                command=self.command_edit.toPlainText() or "",
            )
        )

    def get_params(self) -> dict:
        return options_to_params(self.snapshot_options())

    def set_command_field_enabled(self, enabled: bool) -> None:
        self.command_edit.setEnabled(enabled)

    def update_cmd_option_state(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            state = compute_cmd_option_state(self.snapshot_options())
            opts = state.options
            widgets = state.widgets

            def apply_tip(widget, key: str) -> None:
                ws = widgets.get(key)
                if ws is None:
                    return
                widget.setEnabled(bool(ws.enabled))
                widget.setToolTip(self.tr(ws.tooltip) if ws.tooltip else "")

            self.mode_c.setToolTip(self.tr(TOOLTIPS["mode_c"]))
            self.mode_k.setToolTip(self.tr(TOOLTIPS["mode_k"]))
            apply_tip(self.d_checkbox, "/D")
            apply_tip(self.q_checkbox, "/Q")
            apply_tip(self.extensions_combo, "extensions")
            apply_tip(self.delayed_combo, "delayed_expansion")
            apply_tip(self.completion_combo, "completion")
            apply_tip(self.encoding_combo, "encoding")
            apply_tip(self.command_edit, "command")

            if opts.mode != MODE_K and self.completion_combo.currentData() != TRI_SYSTEM:
                self.completion_combo.setCurrentIndex(0)

            if opts.mode == MODE_K:
                self._cmd_caption.setText(
                    self.tr(
                        "Sessão persistente: cwd e variáveis valem para o próximo comando. "
                        "Várias linhas são unidas com &. Digite exit para encerrar."
                    )
                )
            else:
                self._cmd_caption.setText(
                    self.tr(
                        "Execução única (/C): captura a saída e o código de saída. "
                        "Operadores CMD (& && || | >) são preservados. "
                        "Várias linhas viram sequência com &."
                    )
                )
        finally:
            self._updating = False
        self.optionsChanged.emit()
