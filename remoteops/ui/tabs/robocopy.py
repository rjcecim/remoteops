from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.robocopy_dest import (
    ALLOWED_RETRIES,
    ALLOWED_WAIT_SECONDS,
    DEFAULT_RETRIES,
    DEFAULT_WAIT_SECONDS,
    coerce_retries,
    coerce_wait_seconds,
    remote_unc_path,
    validate_robocopy_dest,
)
from remoteops.ui.style import (
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    FONT_UI,
    SIZE_UI,
    SIZE_UI_SMALL,
    SPACE_SM,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    bind_card_stack,
    make_card_stack,
)
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.flow import FlowLayout
from remoteops.utils.ping import format_unc_host

_OK_GREEN = "#107C10"
_ERR_RED = "#c42b1c"
_CHECK_GLYPH = "\uE73E"  # Accept / checkmark (Segoe MDL2)
_ERR_GLYPH = "\uE783"  # ErrorBadge


class RobocopyTab(QWidget):
    formLayoutChanged = pyqtSignal()
    paramsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._preview_host = "HOST"
        vbox = make_card_stack(self)

        # ── Card Destino ──────────────────────────────────────────────────────
        card_dest = CardWidget("\uE8B7", self.tr("Destino"))
        dest_body = QVBoxLayout()
        dest_body.setContentsMargins(0, 0, 0, 0)
        dest_body.setSpacing(6)

        dest_caption = QLabel(self.tr("Pasta de destino no computador remoto"))
        dest_caption.setObjectName("robocopyFieldCaption")
        dest_caption.setStyleSheet(
            f"QLabel#robocopyFieldCaption {{ color: {COLOR_TEXT_SECONDARY}; "
            f"font-size: {SIZE_UI}pt; }}"
        )
        dest_body.addWidget(dest_caption)

        self.dest_edit = QLineEdit()
        self.dest_edit.setText("C:\\temp")
        self.dest_edit.setPlaceholderText(self.tr("Ex.: C:\\temp ou C:\\temp\\scripts"))
        self.dest_edit.setToolTip(
            self.tr(
                "Caminho no C: do host remoto. "
                "Ex.: C:\\temp ou C:\\temp\\scripts. "
                "C: é opcional (temp equivale a C:\\temp). Não use UNC nem C$."
            )
        )
        dest_body.addWidget(self.dest_edit)

        status_row = QWidget()
        status_lay = QHBoxLayout(status_row)
        status_lay.setContentsMargins(0, 2, 0, 0)
        status_lay.setSpacing(6)
        self.dest_status_icon = QLabel(_CHECK_GLYPH)
        self.dest_status_icon.setObjectName("robocopyDestIcon")
        self.dest_status_icon.setFont(QFont("Segoe MDL2 Assets", 12))
        self.dest_status_icon.setStyleSheet(f"color: {_OK_GREEN}; background: transparent;")
        self.dest_status = QLabel("")
        self.dest_status.setObjectName("robocopyDestStatus")
        self.dest_status.setWordWrap(True)
        self.dest_status.setStyleSheet(
            f"QLabel#robocopyDestStatus {{ color: {_OK_GREEN}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
        )
        status_lay.addWidget(self.dest_status_icon, 0, Qt.AlignmentFlag.AlignTop)
        status_lay.addWidget(self.dest_status, 1)
        dest_body.addWidget(status_row)

        self.dest_unc = QLabel("")
        self.dest_unc.setObjectName("robocopyDestUnc")
        self.dest_unc.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.dest_unc.setStyleSheet(
            f"QLabel#robocopyDestUnc {{ color: {COLOR_TEXT_MUTED}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
        )
        dest_body.addWidget(self.dest_unc)

        card_dest.content_layout.addLayout(dest_body)
        vbox.addWidget(card_dest)

        # ── Card Parâmetros ─────────────────────────────────────────────────────
        card_params = CardWidget("\uE115", self.tr("Parâmetros"))
        params_body = QVBoxLayout()
        params_body.setContentsMargins(0, 0, 0, 0)
        params_body.setSpacing(SPACE_SM)

        display_header = QWidget()
        display_header_lay = QHBoxLayout(display_header)
        display_header_lay.setContentsMargins(0, 0, 0, 0)
        display_header_lay.setSpacing(8)
        display_title = QLabel(self.tr("Exibição da saída"))
        display_title.setObjectName("robocopySectionTitle")
        title_font = QFont(FONT_UI, SIZE_UI)
        title_font.setBold(True)
        display_title.setFont(title_font)
        display_title.setStyleSheet(f"color: {COLOR_TEXT}; background: transparent;")
        self.switches_count = QLabel("")
        self.switches_count.setObjectName("robocopySwitchesCount")
        self.switches_count.setStyleSheet(
            f"QLabel#robocopySwitchesCount {{ color: {COLOR_TEXT_MUTED}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
        )
        display_header_lay.addWidget(display_title, 0)
        display_header_lay.addStretch(1)
        display_header_lay.addWidget(self.switches_count, 0)
        params_body.addWidget(display_header)

        switches_container = QWidget()
        switches_flow = FlowLayout(switches_container, margin=0, h_spacing=10, v_spacing=6)
        self.switches: list[QCheckBox] = []
        self.switch_labels = [
            ("/NFL", self.tr("Não listar arquivos")),
            ("/NDL", self.tr("Não listar diretórios")),
            ("/NJH", self.tr("Ocultar cabeçalho")),
            ("/NJS", self.tr("Ocultar resumo")),
            ("/NC", self.tr("Ocultar classes")),
            ("/NS", self.tr("Ocultar tamanhos")),
            ("/NP", self.tr("Ocultar progresso")),
        ]
        for flag, desc in self.switch_labels:
            switches_flow.addWidget(self._make_switch_option(flag, desc))
        switches_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        params_body.addWidget(switches_container)

        retry_row = QWidget()
        retry_layout = QHBoxLayout(retry_row)
        retry_layout.setContentsMargins(0, 4, 0, 0)
        retry_layout.setSpacing(12)

        self.retries_combo = FluentComboBox()
        for n in ALLOWED_RETRIES:
            label = (
                "0 tentativas"
                if n == 0
                else ("1 tentativa" if n == 1 else f"{n} tentativas")
            )
            self.retries_combo.addItem(label, n)
        self.retries_combo.setCurrentIndex(list(ALLOWED_RETRIES).index(DEFAULT_RETRIES))
        self.retries_combo.setToolTip(
            self.tr(
                "Número de novas tentativas do Robocopy (/R) diante de falhas "
                "de cópia transitórias."
            )
        )
        self.retries_combo.currentIndexChanged.connect(self._emit_params_changed)

        self.wait_combo = FluentComboBox()
        for n in ALLOWED_WAIT_SECONDS:
            label = (
                "0 segundo"
                if n == 0
                else ("1 segundo" if n == 1 else f"{n} segundos")
            )
            self.wait_combo.addItem(label, n)
        self.wait_combo.setCurrentIndex(
            list(ALLOWED_WAIT_SECONDS).index(DEFAULT_WAIT_SECONDS)
        )
        self.wait_combo.setToolTip(
            self.tr("Espera em segundos entre tentativas do Robocopy (/W).")
        )
        self.wait_combo.currentIndexChanged.connect(self._emit_params_changed)

        retry_layout.addWidget(
            self._labeled_field(self.tr("Novas tentativas (/R)"), self.retries_combo),
            1,
        )
        retry_layout.addWidget(
            self._labeled_field(self.tr("Espera entre tentativas (/W)"), self.wait_combo),
            1,
        )
        params_body.addWidget(retry_row)

        card_params.content_layout.addLayout(params_body)
        vbox.addWidget(card_params)

        self._form_cards = (card_dest, card_params)
        for card, on_reset in zip(
            self._form_cards,
            (self._reset_card_destino, self._reset_card_parametros),
        ):
            card.set_collapsible(True, collapsed=False)
            card.set_resettable(True, self.tr("Restaurar"))
            card.resetRequested.connect(on_reset)
            card.collapsedChanged.connect(self._on_form_card_collapsed)
        bind_card_stack(vbox, self._form_cards)

        self.dest_edit.textChanged.connect(self._on_dest_changed)
        self._refresh_dest_status()
        self._refresh_switches_count()

    def _make_switch_option(self, flag: str, desc: str) -> QWidget:
        cell = QWidget()
        cell.setObjectName("robocopySwitchCell")
        lay = QVBoxLayout(cell)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(0)

        cb = QCheckBox(flag)
        cb.setChecked(True)
        cb.setToolTip(f"{flag} — {desc}")
        flag_font = QFont(FONT_UI, SIZE_UI)
        flag_font.setBold(True)
        cb.setFont(flag_font)
        cb.stateChanged.connect(self._on_switch_changed)
        self.switches.append(cb)
        lay.addWidget(cb)

        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("robocopySwitchDesc")
        desc_lbl.setStyleSheet(
            f"QLabel#robocopySwitchDesc {{ color: {COLOR_TEXT_MUTED}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; "
            f"padding-left: 22px; }}"
        )
        desc_lbl.setToolTip(cb.toolTip())
        lay.addWidget(desc_lbl)
        cell.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
        return cell

    def _labeled_field(self, caption: str, widget: QWidget) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lbl = QLabel(caption)
        lbl.setObjectName("robocopyFieldCaption")
        lbl.setStyleSheet(
            f"QLabel#robocopyFieldCaption {{ color: {COLOR_TEXT_SECONDARY}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
        )
        lay.addWidget(lbl)
        lay.addWidget(widget)
        return wrap

    def set_preview_host(self, host: str) -> None:
        """Atualiza o host usado no feedback UNC (sem alterar o destino)."""
        text = (host or "").strip() or "HOST"
        if text == self._preview_host:
            return
        self._preview_host = text
        self._refresh_dest_status()

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    def _emit_params_changed(self, *_args) -> None:
        self.paramsChanged.emit()

    def _on_switch_changed(self, *_args) -> None:
        self._refresh_switches_count()
        self.paramsChanged.emit()

    def _refresh_switches_count(self) -> None:
        n = sum(1 for cb in self.switches if cb.isChecked())
        total = len(self.switches)
        self.switches_count.setText(self.tr("%1 de %2 opções")
            .replace("%1", str(n))
            .replace("%2", str(total)))

    def _on_dest_changed(self, _text: str = "") -> None:
        self._refresh_dest_status()
        self.paramsChanged.emit()

    def _refresh_dest_status(self) -> None:
        canonical, err = validate_robocopy_dest(self.dest_edit.text())
        if err:
            self.dest_status_icon.setText(_ERR_GLYPH)
            self.dest_status_icon.setStyleSheet(
                f"color: {_ERR_RED}; background: transparent;"
            )
            self.dest_status.setText(err)
            self.dest_status.setStyleSheet(
                f"QLabel#robocopyDestStatus {{ color: {_ERR_RED}; "
                f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
            )
            self.dest_status.setToolTip(err)
            self.dest_unc.clear()
            self.dest_unc.setToolTip("")
            return

        try:
            unc_host = format_unc_host(self._preview_host)
        except Exception:
            unc_host = self._preview_host
        unc = remote_unc_path(unc_host, canonical)

        self.dest_status_icon.setText(_CHECK_GLYPH)
        self.dest_status_icon.setStyleSheet(
            f"color: {_OK_GREEN}; background: transparent;"
        )
        self.dest_status.setText(
            self.tr("Destino formatado para o compartilhamento C$")
        )
        self.dest_status.setStyleSheet(
            f"QLabel#robocopyDestStatus {{ color: {_OK_GREEN}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
        )
        self.dest_status.setToolTip(unc)
        self.dest_unc.setText(unc)
        self.dest_unc.setToolTip(unc)

    def get_params(self) -> dict:
        switches = " ".join(cb.text() for cb in self.switches if cb.isChecked())
        retries = self.retries_combo.currentData()
        wait = self.wait_combo.currentData()
        if retries is None:
            retries = DEFAULT_RETRIES
        if wait is None:
            wait = DEFAULT_WAIT_SECONDS
        return {
            "dest": self.dest_edit.text(),
            "switches": switches,
            "retries": coerce_retries(retries),
            "wait": coerce_wait_seconds(wait),
        }

    def _set_combo_data(
        self, combo: FluentComboBox, value: int, allowed: tuple[int, ...]
    ) -> None:
        target = value if value in allowed else allowed[0]
        idx = combo.findData(target)
        if idx < 0:
            idx = 0
        combo.setCurrentIndex(idx)

    def _reset_card_destino(self) -> None:
        self.dest_edit.setText("C:\\temp")
        self._refresh_dest_status()
        self.paramsChanged.emit()

    def _reset_card_parametros(self) -> None:
        for cb in self.switches:
            cb.setChecked(True)
        self._set_combo_data(self.retries_combo, DEFAULT_RETRIES, ALLOWED_RETRIES)
        self._set_combo_data(self.wait_combo, DEFAULT_WAIT_SECONDS, ALLOWED_WAIT_SECONDS)
        self._refresh_switches_count()
        self.paramsChanged.emit()

    def reset_to_defaults(self) -> None:
        self._reset_card_destino()
        self._reset_card_parametros()
