"""Aba PowerShell — modos exclusivos do powershell.exe (não pwsh)."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QWidget,
)

from remoteops.core.powershell_options import (
    ENC_SRC_B64,
    ENC_SRC_TEXT,
    EXEC_POLICIES,
    MODE_COMMAND,
    MODE_ENCODED,
    MODE_FILE,
    MODE_SESSION,
    TOOLTIPS,
    PowerShellOptions,
    compute_powershell_option_state,
    options_to_params,
)
from remoteops.ui.style import (
    CARD_GRID_VERTICAL_SPACING,
    FONT_MONO,
    INPUT_HEIGHT,
    SIZE_MONO,
    SIZE_UI_SMALL,
    multiline_edit_qss,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    grid_in_card,
    make_card_stack,
    make_field_label,
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


def _mono_edit(min_rows: int = 3) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setFont(QFont(FONT_MONO, SIZE_MONO))
    edit.setTabChangesFocus(True)
    edit.setMinimumHeight(INPUT_HEIGHT * min_rows)
    edit.setMaximumHeight(INPUT_HEIGHT * 8)
    edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    edit.setStyleSheet(multiline_edit_qss())
    return edit


class PowerShellTab(QWidget):
    formLayoutChanged = pyqtSignal()
    optionsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._updating = False
        self._script_locked = False
        vbox = make_card_stack(self)

        card_opts = CardWidget("\uE115", self.tr("Opções"))
        g1 = grid_in_card(card_opts)
        row = 0

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(self.tr("Executar comando"), MODE_COMMAND)
        self.mode_combo.addItem(self.tr("Comando codificado"), MODE_ENCODED)
        self.mode_combo.addItem(self.tr("Executar script .ps1"), MODE_FILE)
        self.mode_combo.addItem(self.tr("Abrir sessão PowerShell"), MODE_SESSION)
        add_row(g1, row, self.tr("Modo"), self.mode_combo)
        row += 1

        flags_row = QHBoxLayout()
        flags_row.setContentsMargins(0, 0, 0, 0)
        flags_row.setSpacing(16)
        self.nologo_checkbox = QCheckBox(self.tr("-NoLogo"))
        self.nologo_checkbox.setChecked(True)
        self.noprofile_checkbox = QCheckBox(self.tr("-NoProfile"))
        self.noprofile_checkbox.setChecked(True)
        self.noninteractive_checkbox = QCheckBox(self.tr("-NonInteractive"))
        flags_row.addWidget(self.nologo_checkbox)
        flags_row.addWidget(self.noprofile_checkbox)
        flags_row.addWidget(self.noninteractive_checkbox)
        flags_row.addStretch()
        flags_wrap = QWidget()
        flags_wrap.setLayout(flags_row)
        add_row(g1, row, self.tr("Switches"), flags_wrap)
        row += 1

        self.execpol_combo = QComboBox()
        self.execpol_combo.addItem(self.tr("Padrão do sistema"), "")
        for pol in EXEC_POLICIES:
            if pol:
                self.execpol_combo.addItem(pol, pol)
        add_row(g1, row, self.tr("ExecutionPolicy"), self.execpol_combo)
        row += 1

        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText(self.tr("Opcional no host remoto, ex.: C:\\Temp"))
        add_row(g1, row, self.tr("WorkingDirectory"), self.workdir_edit)

        vbox.addWidget(card_opts)

        card_cmd = CardWidget("\uE768", self.tr("Comando"))
        g2 = grid_in_card(card_cmd)
        cmd_row = 0

        self.encode_source_combo = QComboBox()
        self.encode_source_combo.addItem(self.tr("Texto (gerar Base64 UTF-16LE)"), ENC_SRC_TEXT)
        self.encode_source_combo.addItem(self.tr("Base64 já pronto"), ENC_SRC_B64)
        self._lbl_encode = make_field_label(self.tr("Origem"))
        g2.addWidget(self._lbl_encode, cmd_row, 0, Qt.AlignmentFlag.AlignVCenter)
        g2.addWidget(self.encode_source_combo, cmd_row, 1, Qt.AlignmentFlag.AlignVCenter)
        cmd_row += 1

        self.command_edit = _mono_edit()
        self.command_edit.setPlaceholderText(
            self.tr("Get-Date, Get-Service | Where-Object Status -eq 'Running'…")
        )
        self._lbl_command = make_field_label(self.tr("Código"))
        g2.addWidget(self._lbl_command, cmd_row, 0, Qt.AlignmentFlag.AlignTop)
        g2.addWidget(self.command_edit, cmd_row, 1, Qt.AlignmentFlag.AlignTop)
        cmd_row += 1

        self.encoded_edit = _mono_edit(2)
        self.encoded_edit.setPlaceholderText(self.tr("Base64 UTF-16LE (não recodifique)"))
        self._lbl_encoded = make_field_label(self.tr("EncodedCommand"))
        g2.addWidget(self._lbl_encoded, cmd_row, 0, Qt.AlignmentFlag.AlignTop)
        g2.addWidget(self.encoded_edit, cmd_row, 1, Qt.AlignmentFlag.AlignTop)
        cmd_row += 1

        self.file_args_edit = QLineEdit()
        self.file_args_edit.setPlaceholderText(
            self.tr("Argumentos do .ps1, ex.: -Nome \"Remote Ops\" -Numero 10")
        )
        self._lbl_file_args = make_field_label(self.tr("Args do script"))
        g2.addWidget(self._lbl_file_args, cmd_row, 0, Qt.AlignmentFlag.AlignVCenter)
        g2.addWidget(self.file_args_edit, cmd_row, 1, Qt.AlignmentFlag.AlignVCenter)
        cmd_row += 1

        self._cmd_caption = _caption("psChainCaption")
        g2.addWidget(self._cmd_caption, cmd_row, 1, Qt.AlignmentFlag.AlignTop)
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
        self.update_powershell_option_state()

    def _connect_signals(self) -> None:
        self.mode_combo.currentIndexChanged.connect(self.update_powershell_option_state)
        self.nologo_checkbox.stateChanged.connect(self.update_powershell_option_state)
        self.noprofile_checkbox.stateChanged.connect(self.update_powershell_option_state)
        self.noninteractive_checkbox.stateChanged.connect(self.update_powershell_option_state)
        self.execpol_combo.currentIndexChanged.connect(self.update_powershell_option_state)
        self.workdir_edit.textChanged.connect(self.update_powershell_option_state)
        self.encode_source_combo.currentIndexChanged.connect(
            self.update_powershell_option_state
        )
        self.command_edit.textChanged.connect(self.update_powershell_option_state)
        self.encoded_edit.textChanged.connect(self.update_powershell_option_state)
        self.file_args_edit.textChanged.connect(self.update_powershell_option_state)

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
            if not self._script_locked:
                self.mode_combo.setCurrentIndex(0)
            self.nologo_checkbox.setChecked(True)
            self.noprofile_checkbox.setChecked(True)
            self.noninteractive_checkbox.setChecked(False)
            self.execpol_combo.setCurrentIndex(0)
            self.workdir_edit.clear()
        finally:
            self._updating = False
        self.update_powershell_option_state()

    def _reset_card_comando(self) -> None:
        self.encode_source_combo.setCurrentIndex(0)
        self.command_edit.clear()
        self.encoded_edit.clear()
        self.file_args_edit.clear()
        self.update_powershell_option_state()

    def snapshot_options(self) -> PowerShellOptions:
        mode = self.mode_combo.currentData() or MODE_COMMAND
        if self._script_locked:
            mode = MODE_FILE
        return PowerShellOptions(
            mode=mode,
            no_logo=self.nologo_checkbox.isChecked(),
            no_profile=self.noprofile_checkbox.isChecked(),
            non_interactive=self.noninteractive_checkbox.isChecked(),
            execution_policy=self.execpol_combo.currentData()
            if self.execpol_combo.currentData() is not None
            else "",
            working_directory=self.workdir_edit.text() or "",
            command=self.command_edit.toPlainText() or "",
            encoded_command=self.encoded_edit.toPlainText() or "",
            encode_from_text=(self.encode_source_combo.currentData() or ENC_SRC_TEXT)
            != ENC_SRC_B64,
            file_args=self.file_args_edit.text() or "",
        )

    def get_params(self) -> dict:
        return options_to_params(self.snapshot_options())

    def set_command_fields_enabled(self, enabled: bool) -> None:
        """False quando um .ps1 está selecionado: trava o modo -File."""
        self._script_locked = not bool(enabled)
        self.update_powershell_option_state()

    def is_session_mode(self) -> bool:
        return (not self._script_locked) and self.mode_combo.currentData() == MODE_SESSION

    def update_powershell_option_state(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            if self._script_locked:
                idx = self.mode_combo.findData(MODE_FILE)
                if idx >= 0:
                    self.mode_combo.setCurrentIndex(idx)
            else:
                if self.mode_combo.currentData() == MODE_FILE:
                    self.mode_combo.setCurrentIndex(0)
            file_idx = self.mode_combo.findData(MODE_FILE)
            model = self.mode_combo.model()
            if file_idx >= 0 and hasattr(model, "item"):
                item = model.item(file_idx)
                if item is not None:
                    item.setEnabled(self._script_locked)
            state = compute_powershell_option_state(self.snapshot_options())
            opts = state.options
            widgets = state.widgets

            def apply(widget, key: str) -> None:
                ws = widgets.get(key)
                if ws is None:
                    return
                widget.setEnabled(bool(ws.enabled))
                widget.setToolTip(self.tr(ws.tooltip) if ws.tooltip else "")
                if hasattr(widget, "setVisible"):
                    widget.setVisible(bool(ws.visible))

            self.mode_combo.setEnabled(not self._script_locked)
            if self._script_locked:
                self.mode_combo.setToolTip(
                    self.tr("Modo -File: um script .ps1 está selecionado.")
                )
            else:
                self.mode_combo.setToolTip(self.tr(TOOLTIPS["mode"]))
            apply(self.nologo_checkbox, "no_logo")
            apply(self.noprofile_checkbox, "no_profile")
            apply(self.noninteractive_checkbox, "non_interactive")
            apply(self.execpol_combo, "execution_policy")
            apply(self.workdir_edit, "working_directory")
            apply(self.encode_source_combo, "encode_source")
            apply(self.command_edit, "command")
            apply(self.encoded_edit, "encoded_command")
            apply(self.file_args_edit, "file_args")
            if opts.mode == MODE_SESSION and self.noninteractive_checkbox.isChecked():
                self.noninteractive_checkbox.setChecked(False)

            self._lbl_encode.setVisible(opts.mode == MODE_ENCODED)
            self.encode_source_combo.setVisible(opts.mode == MODE_ENCODED)
            self._lbl_command.setVisible(widgets["command"].visible)
            self.command_edit.setVisible(widgets["command"].visible)
            self._lbl_encoded.setVisible(widgets["encoded_command"].visible)
            self.encoded_edit.setVisible(widgets["encoded_command"].visible)
            self._lbl_file_args.setVisible(opts.mode == MODE_FILE)
            self.file_args_edit.setVisible(opts.mode == MODE_FILE)

            if opts.mode == MODE_SESSION:
                self._cmd_caption.setText(
                    self.tr(
                        "Sessão persistente: cwd, variáveis e funções valem para o próximo comando. "
                        "Comando inicial opcional. Ctrl+C interrompe o comando; Encerrar sessão envia exit."
                    )
                )
            elif opts.mode == MODE_ENCODED:
                self._cmd_caption.setText(
                    self.tr(
                        "UTF-16LE + Base64 gerado pelo RemoteOps (ou cole Base64 pronto). "
                        "Não misture com -Command nem -File."
                    )
                )
            elif opts.mode == MODE_FILE:
                self._cmd_caption.setText(
                    self.tr(
                        "powershell.exe -File script.ps1 [argumentos do script]. "
                        "-NoProfile e -ExecutionPolicy são do powershell.exe, não do .ps1."
                    )
                )
            else:
                self._cmd_caption.setText(
                    self.tr(
                        "Execução única (-Command): o código é um único argumento. "
                        "Pipelines, aspas e várias linhas são preservados."
                    )
                )
        finally:
            self._updating = False
        self.formLayoutChanged.emit()
        self.optionsChanged.emit()
