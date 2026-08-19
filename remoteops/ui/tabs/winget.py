"""Aba WinGet — pacotes remotos via winget (integrado do WingetRM).

Reutiliza host e credenciais do PsExec (sem cards Conexao/Autenticacao).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import accent_button_qss
from remoteops.ui.widgets.card import CardWidget, add_row, grid_in_card, make_card_stack
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.winget.controllers.exec_log import ExecLogRouter
from remoteops.ui.winget.controllers.progress_controller import ProgressController
from remoteops.ui.winget.parsers.winget_text import (
    parse_winget_list,
    parse_winget_search,
    parse_winget_upgrade,
)
from remoteops.ui.winget.preview import build_preview_text
from remoteops.ui.winget.table_style import (
    apply_flat_list_table_style,
    apply_interactive_list_headers,
)
from remoteops.ui.winget.workers.winget_worker import WinGetWorker
from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool
from remoteops.winget.clixml import clixml_to_text, looks_like_clixml, summarize_one_line
from remoteops.winget.constants import is_winget_success_exit
from remoteops.winget.winget_output import (
    format_exec_result_line,
    is_winget_spinner_status,
    summarize_winget_output,
)


class WinGetTab(QWidget):
    def __init__(
        self,
        parent=None,
        *,
        host_source: Optional[QLineEdit] = None,
        creds_provider: Optional[Callable[[], Tuple[str, str]]] = None,
    ):
        super().__init__(parent)
        self._host_source = host_source
        self._creds_provider = creds_provider
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(0)

        self._root_layout = make_card_stack(self)
        self._collapsible_cards: list[CardWidget] = []
        self._bottom_stretch_idx: int | None = None
        self._page_stretch_idx: dict[int, int] = {}
        self._progress = None
        self._exec_log = None

        self._root_layout.addWidget(self._register_card(self._build_preview_card()))
        self._tabs = self._build_tabs()
        self._root_layout.addWidget(self._tabs, stretch=3)
        self._root_layout.addWidget(self._register_card(self._build_progress_card()))
        # Console exclusivo da aba WinGet (não mistura com o console principal)
        self.log_output = self._register_card(LogOutputWidget())
        self.log_output.set_layout_stretch(2)
        self._root_layout.addWidget(self.log_output, stretch=2)

        self._worker: WinGetWorker | None = None
        self._last_host: str | None = None
        if self._host_source is not None:
            self._host_source.textChanged.connect(self._refresh_live_preview)
        self._redistribute_card_space()

    def _get_host(self) -> str:
        if self._host_source is None:
            return ""
        try:
            return (self._host_source.text() or "").strip().strip("\\")
        except RuntimeError:
            return ""

    def _get_creds(self) -> Tuple[str, str]:
        if self._creds_provider is None:
            return "", ""
        try:
            user, password = self._creds_provider()
            return (user or "").strip(), password or ""
        except Exception:
            return "", ""

    def _psexec_path(self) -> str:
        return resolve_pstools_tool(get_pstools_dir(), ("PsExec64.exe", "PsExec.exe"))

    def shutdown(self, wait_ms: int = 3000) -> None:
        w = self._worker
        if w is None:
            return
        try:
            if w.isRunning():
                w.request_cancel()
                w.wait(wait_ms)
        except Exception:
            pass
        self._worker = None

    def _register_card(self, card: CardWidget) -> CardWidget:
        self._collapsible_cards.append(card)
        card.collapsedChanged.connect(lambda _c: self._redistribute_card_space())
        return card

    def _redistribute_card_space(self) -> None:
        """
        Divide o espaço entre abas/console abertos.
        Com expansíveis recolhidos, stretch final mantém os cabeçalhos no topo
        (mesmo padrão de Listar Apps / Pesquisa / janela principal).
        """
        layout = getattr(self, "_root_layout", None)
        if layout is None:
            return

        log_output = getattr(self, "log_output", None)
        tabs = getattr(self, "_tabs", None)

        log_open = log_output is not None and not log_output.is_collapsed
        # Abas crescem se o card da aba atual estiver expandido (ou se não houver card).
        tabs_open = True
        page_card = None
        if tabs is not None:
            page = tabs.currentWidget()
            if page is not None:
                page_card = page.findChild(CardWidget)
                if page_card is not None and page_card.is_collapsed:
                    tabs_open = False

        if log_open and tabs_open:
            tabs_stretch, log_stretch = 3, 2
        elif tabs_open:
            tabs_stretch, log_stretch = 1, 0
        elif log_open:
            tabs_stretch, log_stretch = 0, 1
        else:
            tabs_stretch, log_stretch = 0, 0

        if tabs is not None:
            tabs_idx = layout.indexOf(tabs)
            if tabs_idx >= 0:
                layout.setStretch(tabs_idx, tabs_stretch)
            bar_h = max(28, tabs.tabBar().sizeHint().height())
            tabs.setMinimumHeight(bar_h if not tabs_open else 120)
            tabs.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Maximum if not tabs_open else QSizePolicy.Policy.Expanding,
            )
            if not tabs_open:
                card_h = 36
                if page_card is not None:
                    card_h = max(32, page_card.sizeHint().height())
                tabs.setMaximumHeight(bar_h + card_h + 4)
            else:
                tabs.setMaximumHeight(16777215)

            # Dentro da página: card aberto absorve; recolhido → cabeçalho no topo.
            self._redistribute_tab_page(tabs.currentWidget(), page_card)

        if log_output is not None:
            log_idx = layout.indexOf(log_output)
            if log_idx >= 0:
                if log_stretch > 0:
                    log_output.set_layout_stretch(log_stretch)
                layout.setStretch(log_idx, log_stretch)

        # Sem AlignTop: stretch final mantém cabeçalhos no topo ao recolher tudo.
        need_tail = tabs_stretch == 0 and log_stretch == 0
        if need_tail:
            if self._bottom_stretch_idx is None:
                layout.addStretch(1)
                self._bottom_stretch_idx = layout.count() - 1
            else:
                layout.setStretch(self._bottom_stretch_idx, 1)
        elif self._bottom_stretch_idx is not None:
            layout.setStretch(self._bottom_stretch_idx, 0)

        layout.activate()
        self.updateGeometry()

    def _redistribute_tab_page(self, page: QWidget | None, card: CardWidget | None) -> None:
        """Empilha o card da subaba no topo quando recolhido (stretch final na página)."""
        if page is None:
            return
        lay = page.layout()
        if lay is None:
            return

        if card is not None:
            idx = lay.indexOf(card)
            if idx >= 0:
                if card.is_collapsed:
                    lay.setStretch(idx, 0)
                else:
                    card.set_layout_stretch(1)
                    lay.setStretch(idx, 1)

        # Stretch final da página (mesmo padrão das outras abas).
        page_id = id(page)
        tail_idx = self._page_stretch_idx.get(page_id)
        need_tail = card is None or card.is_collapsed
        if need_tail:
            if tail_idx is None:
                lay.addStretch(1)
                self._page_stretch_idx[page_id] = lay.count() - 1
            else:
                lay.setStretch(tail_idx, 1)
        elif tail_idx is not None:
            lay.setStretch(tail_idx, 0)

    def _reset_progress_ui(self) -> None:
        if self._exec_log is not None:
            self._exec_log.reset()
        elif self._progress is not None:
            self._progress.reset()

    def _reset_results_ui(self) -> None:
        # Atualizações
        if hasattr(self, "table"):
            self.table.setRowCount(0)
        if hasattr(self, "chk_all"):
            self.chk_all.blockSignals(True)
            self.chk_all.setChecked(False)
            self.chk_all.blockSignals(False)
        if hasattr(self, "lbl_count"):
            self.lbl_count.setText("0 itens")
        if hasattr(self, "btn_upg"):
            self.btn_upg.setEnabled(False)

        # Search
        if hasattr(self, "search_table"):
            self.search_table.setRowCount(0)
        if hasattr(self, "search_mark_all"):
            self.search_mark_all.blockSignals(True)
            self.search_mark_all.setChecked(False)
            self.search_mark_all.blockSignals(False)
        if hasattr(self, "search_count"):
            self.search_count.setText("0 itens")
        if hasattr(self, "btn_search_install_sel"):
            self.btn_search_install_sel.setEnabled(False)

        # Instalados
        if hasattr(self, "inst_table"):
            self.inst_table.setRowCount(0)
        if hasattr(self, "inst_mark_all"):
            self.inst_mark_all.blockSignals(True)
            self.inst_mark_all.setChecked(False)
            self.inst_mark_all.blockSignals(False)
        if hasattr(self, "inst_count"):
            self.inst_count.setText("0 itens")
        if hasattr(self, "btn_inst_uninstall_sel"):
            self.btn_inst_uninstall_sel.setEnabled(False)

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        inner_bar = Mdl2TabBar(tabs)
        inner_bar.setExpanding(False)
        tabs.setTabBar(inner_bar)
        tabs.setDocumentMode(True)
        tabs.setUsesScrollButtons(True)
        tabs.setMinimumWidth(0)
        tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        upgrades = QWidget()
        up_v = QVBoxLayout(upgrades)
        up_v.setContentsMargins(0, 0, 0, 0)
        up_v.setSpacing(3)
        up_v.addWidget(self._register_card(self._build_upgrades_card()), stretch=1)

        search = QWidget()
        s_v = QVBoxLayout(search)
        s_v.setContentsMargins(0, 0, 0, 0)
        s_v.setSpacing(3)
        s_v.addWidget(self._register_card(self._build_search_card()), stretch=1)

        installed = QWidget()
        i_v = QVBoxLayout(installed)
        i_v.setContentsMargins(0, 0, 0, 0)
        i_v.setSpacing(3)
        i_v.addWidget(self._register_card(self._build_installed_card()), stretch=1)

        idx_up = tabs.addTab(upgrades, "Atualizações")
        inner_bar.set_tab_meta(idx_up, "\uE8A7")  # UpdateRestore
        tabs.setTabToolTip(idx_up, "Lista atualizações disponíveis (winget upgrade)")
        idx_s = tabs.addTab(search, "Busca")
        inner_bar.set_tab_meta(idx_s, "\uE721")  # Search
        tabs.setTabToolTip(idx_s, "Buscar pacotes no winget (winget search)")
        idx_i = tabs.addTab(installed, "Instalados")
        inner_bar.set_tab_meta(idx_i, "\uE8B7")  # Copy/List (ícone neutro)
        tabs.setTabToolTip(idx_i, "Lista pacotes instalados (winget list)")
        inner_bar.refresh_layout()
        self._tab_idx_upgrades = idx_up
        self._tab_idx_search = idx_s
        self._tab_idx_installed = idx_i
        tabs.currentChanged.connect(self._on_winget_subtab_changed)
        return tabs

    def _on_winget_subtab_changed(self, _index: int) -> None:
        self._redistribute_card_space()
        self._refresh_live_preview()

    def _on_list_from_info(self) -> None:
        # Mesmo comportamento do botão “Consultar”, mas sempre troca para a aba Atualizações.
        try:
            if hasattr(self, "_tabs") and hasattr(self, "_tab_idx_upgrades"):
                self._tabs.setCurrentIndex(self._tab_idx_upgrades)
        except Exception:
            pass
        self._on_list()

    def _build_preview_card(self) -> CardWidget:
        card = CardWidget("\uE756", "Pré-visualização do comando")
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 9))
        self.preview.setMinimumHeight(70)
        self.preview.setMaximumHeight(110)
        self.preview.setMinimumWidth(0)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        v.addWidget(self.preview)
        card.content_layout.addLayout(v)
        self.preview.setPlainText("Selecione uma ação (Atualizar/Instalar/Desinstalar) para ver o comando.")
        card.set_collapsible(True, collapsed=False)
        card.set_copyable(True)
        card.copyRequested.connect(self._copy_preview_to_clipboard)
        return card

    def _copy_preview_to_clipboard(self) -> None:
        text = (self.preview.toPlainText() or "").strip()
        if not text:
            return
        QApplication.clipboard().setText(text)

    def _build_upgrades_card(self) -> CardWidget:
        card = CardWidget("\uE8A7", "Atualizações disponíveis")
        # \uE8FD = ViewList — verificar atualizações; \uE895 = Download — atualizar
        self.btn_info_list = card.make_header_button(
            "\uE8FD",
            "Verificar atualizações disponíveis (winget upgrade)",
        )
        self.btn_info_list.clicked.connect(self._on_list_from_info)
        card.add_header_button(self.btn_info_list)
        self.btn_upg = card.make_header_button(
            "\uE895",
            "Atualizar todos (winget upgrade --all)",
        )
        self.btn_upg.clicked.connect(self._on_upgrade)
        card.add_header_button(self.btn_upg)

        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "Nome", "Id", "Instalado", "Disponível", "Fonte"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        apply_interactive_list_headers(self.table)
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        apply_flat_list_table_style(self.table, object_name="wingetTblUpgrades")

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        self.chk_all = QCheckBox("Marcar tudo")
        self.chk_all.toggled.connect(self._toggle_all)
        self.lbl_count = QLabel("0 itens")
        self.lbl_count.setStyleSheet("opacity: 0.75;")
        top.addWidget(self.chk_all)
        top.addWidget(self.lbl_count)
        top.addStretch()

        v.addLayout(top)
        v.addWidget(self.table)
        card.content_layout.addLayout(v)
        card.set_expanding(True)
        card.set_collapsible(True, collapsed=False)
        return card

    def _build_search_card(self) -> CardWidget:
        card = CardWidget("\uE721", "Buscar e instalar (winget search)")
        self.btn_search = card.make_header_button(
            "\uE721",
            "Buscar pacotes (winget search)",
        )
        self.btn_search.clicked.connect(self._on_search)
        card.add_header_button(self.btn_search)
        self.btn_search_install_sel = card.make_header_button(
            "\uE896",
            "Instalar selecionados (winget install --id ...)",
        )
        self.btn_search_install_sel.clicked.connect(self._on_install_search_selected)
        card.add_header_button(self.btn_search_install_sel)

        g = grid_in_card(card)

        self.search_query = QLineEdit()
        self.search_query.setPlaceholderText("Ex: chrome, zoom, winrar...")
        self.search_query.returnPressed.connect(self._on_search)
        self.search_query.textChanged.connect(self._refresh_live_preview)
        self.search_query.installEventFilter(self)
        add_row(g, 0, "Termo", self.search_query)

        self.search_table = QTableWidget(0, 6)
        self.search_table.setHorizontalHeaderLabels(["", "Nome", "Id", "Versão", "Match", "Fonte"])
        self.search_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.search_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.search_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.search_table.verticalHeader().setVisible(False)
        self.search_table.setShowGrid(False)
        apply_interactive_list_headers(self.search_table)
        self.search_table.setMinimumWidth(0)
        self.search_table.setMinimumHeight(160)
        self.search_table.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        apply_flat_list_table_style(self.search_table, object_name="wingetTblSearch")

        self.search_mark_all = QCheckBox("Marcar tudo")
        self.search_mark_all.toggled.connect(self._toggle_all_search)
        self.search_count = QLabel("0 itens")
        self.search_count.setStyleSheet("opacity: 0.75;")
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.search_mark_all)
        top.addWidget(self.search_count)
        top.addStretch()

        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(6)
        wrap.addLayout(top)
        wrap.addWidget(self.search_table)
        card.content_layout.addLayout(wrap)
        card.set_expanding(True)
        card.set_collapsible(True, collapsed=False)
        return card

    def _build_installed_card(self) -> CardWidget:
        card = CardWidget("\uE8B7", "Pacotes instalados (winget list)")
        self.btn_refresh_installed = card.make_header_button(
            "\uE8FD",
            "Atualizar lista (winget list)",
        )
        self.btn_refresh_installed.clicked.connect(self._on_installed_list)
        card.add_header_button(self.btn_refresh_installed)
        self.btn_inst_uninstall_sel = card.make_header_button(
            "\uE74D",
            "Desinstalar selecionados (winget uninstall --id ...)",
        )
        self.btn_inst_uninstall_sel.clicked.connect(self._on_uninstall_installed_selected)
        card.add_header_button(self.btn_inst_uninstall_sel)

        self.inst_mark_all = QCheckBox("Marcar tudo")
        self.inst_mark_all.toggled.connect(self._toggle_all_installed)
        self.inst_count = QLabel("0 itens")
        self.inst_count.setStyleSheet("opacity: 0.75;")
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.inst_mark_all)
        top.addWidget(self.inst_count)
        top.addStretch()

        self.inst_table = QTableWidget(0, 6)
        self.inst_table.setHorizontalHeaderLabels(["", "Nome", "Id", "Versão", "Disponível", "Fonte"])
        self.inst_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.inst_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.inst_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.inst_table.verticalHeader().setVisible(False)
        self.inst_table.setShowGrid(False)
        apply_interactive_list_headers(self.inst_table)
        self.inst_table.setMinimumWidth(0)
        self.inst_table.setMinimumHeight(180)
        self.inst_table.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding
        )
        apply_flat_list_table_style(self.inst_table, object_name="wingetTblInstalled")

        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(6)
        wrap.addLayout(top)
        wrap.addWidget(self.inst_table)
        card.content_layout.addLayout(wrap)
        card.set_expanding(True)
        card.set_collapsible(True, collapsed=False)
        return card

    def _build_progress_card(self) -> CardWidget:
        card = CardWidget("\uE9D9", "Progresso")
        g = grid_in_card(card)

        self.lbl_step = QLabel("0 de 0 (00/00)")
        self.lbl_step.setStyleSheet("opacity: 0.75;")
        self.lbl_current = QLabel("-")
        self.lbl_current.setWordWrap(True)
        self.lbl_current.setStyleSheet("opacity: 0.90;")

        self.pb_current = QProgressBar()
        self.pb_current.setRange(0, 100)
        self.pb_current.setValue(0)
        self.pb_current.setFormat("%p%")

        self.pb_total = QProgressBar()
        self.pb_total.setRange(0, 100)
        self.pb_total.setValue(0)
        self.pb_total.setFormat("%p%")

        add_row(g, 0, "Item", self.lbl_step)
        add_row(g, 1, "Atual", self.lbl_current)
        add_row(g, 2, "Progresso do item", self.pb_current)
        add_row(g, 3, "Progresso total", self.pb_total)

        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setToolTip(
            "Interrompe a operação no host remoto (sinal de cancelamento) e, se preciso, o PsExec local"
        )
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setStyleSheet(accent_button_qss(padding="4px 12px"))
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel_operation)
        cancel_row = QWidget()
        cancel_lay = QHBoxLayout(cancel_row)
        cancel_lay.setContentsMargins(0, 0, 0, 0)
        cancel_lay.addStretch()
        cancel_lay.addWidget(self.btn_cancel)
        add_row(g, 4, "Operação", cancel_row)

        self._progress = ProgressController(
            parent=self,
            lbl_step=self.lbl_step,
            lbl_current=self.lbl_current,
            pb_current=self.pb_current,
            pb_total=self.pb_total,
        )
        self._exec_log = ExecLogRouter(self._progress)
        card.set_collapsible(True, collapsed=False)
        return card

    def _emit_console_line(self, text: str) -> None:
        """Spinner de espera do winget (\\r) fica numa linha só e anima - \\ | /."""
        if is_winget_spinner_status(text):
            self.log_output.set_partial_line(text)
            return
        self.log_output.append_log(text)

    def _append_log(self, text: str) -> None:
        if self._exec_log is None:
            if text:
                self._emit_console_line(text)
            return

        text, realtime = self._exec_log.strip_realtime_prefix(text)
        line = self._exec_log.process(text, realtime=realtime)
        if line is None:
            return

        self._emit_console_line(line if line else "")

    def _set_busy(self, busy: bool) -> None:
        widgets = [self.btn_info_list]
        # botões por aba (topo)
        for name in [
            "btn_upg",
            "btn_search_install_sel",
            "btn_inst_uninstall_sel",
            "btn_search",
            "btn_refresh_installed",
        ]:
            w = getattr(self, name, None)
            if w is not None:
                widgets.append(w)

        for w in widgets:
            w.setEnabled(not busy)

        if hasattr(self, "btn_cancel"):
            self.btn_cancel.setEnabled(busy)

        if not busy:
            # Recalcula estados dos botões de cada aba
            if hasattr(self, "btn_upg"):
                self._refresh_upgrades_bulk_buttons()
            if hasattr(self, "btn_search_install_sel"):
                self._refresh_search_bulk_buttons()
            if hasattr(self, "btn_inst_uninstall_sel"):
                self._refresh_installed_bulk_buttons()
            self._refresh_live_preview()

    def eventFilter(self, watched, event):
        query = getattr(self, "search_query", None)
        if (
            query is not None
            and watched is query
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and self._worker is not None
            and self._worker.isRunning()
        ):
            return True
        return super().eventFilter(watched, event)

    def _on_cancel_operation(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.request_cancel()
            self._append_log("[INFO] Cancelamento solicitado…")

    def _toggle_all(self, checked: bool) -> None:
        for row in range(self.table.rowCount()):
            chk = self._row_checkbox(row)
            if chk:
                chk.blockSignals(True)
                chk.setChecked(bool(checked))
                chk.blockSignals(False)
        self._on_upgrades_selection_changed()

    def _get_selected_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.table.rowCount()):
            chk = self._row_checkbox(row)
            if chk and chk.isChecked():
                item = self.table.item(row, 2)
                if item and item.text().strip():
                    ids.append(item.text().strip())
        return ids

    def _all_upgrades_selected(self) -> bool:
        """True quando todos os itens da lista de atualizações estão marcados."""
        n = self.table.rowCount()
        if n <= 0:
            return False
        for row in range(n):
            chk = self._row_checkbox(row)
            if chk is None or not chk.isChecked():
                return False
        return True

    def _sync_upgrades_mark_all(self) -> None:
        if not hasattr(self, "chk_all"):
            return
        all_on = self._all_upgrades_selected()
        if self.chk_all.isChecked() == all_on:
            return
        self.chk_all.blockSignals(True)
        self.chk_all.setChecked(all_on)
        self.chk_all.blockSignals(False)

    def _preview_pending_upgrade(self) -> None:
        if not hasattr(self, "table") or self.table.rowCount() <= 0:
            return
        ids = self._get_selected_ids()
        if ids and not self._all_upgrades_selected():
            self._set_preview(action="upgrade", ids=ids, query="")
        else:
            self._set_preview(action="upgrade_all", ids=[], query="")

    def _preview_pending_search(self) -> None:
        if not hasattr(self, "search_table"):
            return
        ids = self._get_selected_search_ids()
        query = ""
        if hasattr(self, "search_query"):
            query = (self.search_query.text() or "").strip()
        if ids:
            self._set_preview(action="install", ids=ids, query=query)
        elif query:
            self._set_preview(action="search", ids=[], query=query)

    def _preview_pending_uninstall(self) -> None:
        if not hasattr(self, "inst_table"):
            return
        ids = self._get_selected_installed_ids()
        if ids:
            self._set_preview(action="uninstall", ids=ids, query="")
        elif self.inst_table.rowCount() > 0:
            self._set_preview(action="installed", ids=[], query="")

    def _refresh_live_preview(self, *_args) -> None:
        """Atualiza a pré-visualização conforme a aba e a marcação atuais."""
        tabs = getattr(self, "_tabs", None)
        if tabs is None:
            return
        idx = tabs.currentIndex()
        if idx == getattr(self, "_tab_idx_upgrades", -1):
            self._preview_pending_upgrade()
        elif idx == getattr(self, "_tab_idx_search", -1):
            self._preview_pending_search()
        elif idx == getattr(self, "_tab_idx_installed", -1):
            self._preview_pending_uninstall()

    def _on_upgrades_selection_changed(self) -> None:
        self._refresh_upgrades_bulk_buttons()
        self._preview_pending_upgrade()

    def _on_search_selection_changed(self) -> None:
        self._refresh_search_bulk_buttons()
        self._preview_pending_search()

    def _on_installed_selection_changed(self) -> None:
        self._refresh_installed_bulk_buttons()
        self._preview_pending_uninstall()

    def _row_checkbox(self, row: int) -> QCheckBox | None:
        return self._checkbox_in_cell(self.table, row)

    def _refresh_upgrades_bulk_buttons(self) -> None:
        ids = self._get_selected_ids()
        any_rows = self.table.rowCount() > 0
        self.btn_upg.setEnabled(any_rows)
        if ids and not self._all_upgrades_selected():
            self.btn_upg.setToolTip(f"Atualizar {len(ids)} selecionado(s) (winget upgrade --id ...)")
        else:
            self.btn_upg.setToolTip("Atualizar todos (winget upgrade --all)")
        self._sync_upgrades_mark_all()

    def _refresh_search_bulk_buttons(self) -> None:
        any_rows = self.search_table.rowCount() > 0
        sel = len(self._get_selected_search_ids()) > 0
        self.btn_search_install_sel.setEnabled(any_rows and sel)

    def _refresh_installed_bulk_buttons(self) -> None:
        any_rows = self.inst_table.rowCount() > 0
        sel = len(self._get_selected_installed_ids()) > 0
        self.btn_inst_uninstall_sel.setEnabled(any_rows and sel)

    def _toggle_all_search(self, checked: bool) -> None:
        for row in range(self.search_table.rowCount()):
            chk = self._row_checkbox_search(row)
            if chk:
                chk.blockSignals(True)
                chk.setChecked(bool(checked))
                chk.blockSignals(False)
        self._on_search_selection_changed()

    def _toggle_all_installed(self, checked: bool) -> None:
        for row in range(self.inst_table.rowCount()):
            chk = self._row_checkbox_installed(row)
            if chk:
                chk.blockSignals(True)
                chk.setChecked(bool(checked))
                chk.blockSignals(False)
        self._on_installed_selection_changed()

    def _checkbox_in_cell(self, table: QTableWidget, row: int) -> QCheckBox | None:
        w = table.cellWidget(row, 0)
        if w is None:
            return None
        chk = getattr(w, "_row_chk", None)
        if isinstance(chk, QCheckBox):
            return chk
        if isinstance(w, QCheckBox):
            return w
        found = w.findChild(QCheckBox)
        return found if isinstance(found, QCheckBox) else None

    def _row_checkbox_installed(self, row: int) -> QCheckBox | None:
        return self._checkbox_in_cell(self.inst_table, row)

    def _get_selected_installed_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.inst_table.rowCount()):
            chk = self._row_checkbox_installed(row)
            if chk and chk.isChecked():
                item = self.inst_table.item(row, 2)
                if item and item.text().strip():
                    ids.append(item.text().strip())
        return ids

    def _row_checkbox_search(self, row: int) -> QCheckBox | None:
        return self._checkbox_in_cell(self.search_table, row)

    def _get_selected_search_ids(self) -> list[str]:
        ids: list[str] = []
        for row in range(self.search_table.rowCount()):
            chk = self._row_checkbox_search(row)
            if chk and chk.isChecked():
                item = self.search_table.item(row, 2)
                if item and item.text().strip():
                    ids.append(item.text().strip())
        return ids

    def _start_worker(
        self,
        *,
        action: str,
        ids: list[str] | None = None,
        query: str = "",
        log_header: str | None = None,
    ) -> None:
        host = self._get_host()
        if not host:
            QMessageBox.warning(
                self,
                "Host",
                "Preencha o Host remoto na aba PsExec antes de usar o WinGet.",
            )
            return

        if action == "search" and not (query or "").strip():
            QMessageBox.warning(self, "Busca", "Informe um termo para busca.")
            return

        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(self, "Aguarde", "Um comando ainda está em execução. Aguarde terminar antes de iniciar outro.")
            return

        # Se o host mudou, limpa resultados antigos antes de mostrar os novos.
        # E sempre reseta o progresso para a nova execução.
        if (self._last_host or "").strip().lower() != host.strip().lower():
            self._reset_results_ui()
            self._last_host = host
        self._reset_progress_ui()
        self._exec_log.begin_exec(action, ids or [])

        # Um comando por vez: console só desta aba / comando atual
        self.log_output.clear_log()
        if log_header is not None:
            self._append_log(log_header)

        # Atualiza pré-visualização antes de executar
        self._set_preview(action=action, ids=ids or [], query=query)

        self._set_busy(True)
        user, password = self._get_creds()
        self._worker = WinGetWorker(
            psexec_path=self._psexec_path(),
            host=host,
            username=user,
            password=password,
            action=action,
            ids=ids or [],
            query=query,
        )
        self._worker.log.connect(self._append_log)
        self._worker.finished_err.connect(self._on_worker_error)
        self._worker.finished_ok.connect(self._on_worker_ok)
        self._worker.item_started.connect(self._on_item_started)
        self._worker.item_finished.connect(self._on_item_finished)
        self._worker.item_progress.connect(self._on_item_progress)
        self._worker.start()

    def _set_preview(self, *, action: str, ids: list[str], query: str) -> None:
        user, password = self._get_creds()
        self.preview.setPlainText(
            build_preview_text(
                psexec_path=self._psexec_path(),
                host=self._get_host(),
                username=user,
                password=password,
                action=action,
                ids=ids,
                query=query,
            )
        )

    def _on_list(self) -> None:
        self._start_worker(
            action="list",
            log_header=f"--- CONSULTA {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---",
        )

    def _on_search(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        term = self.search_query.text().strip()
        self._start_worker(
            action="search",
            query=term,
            log_header=f"--- BUSCA {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---",
        )

    def _on_upgrade(self) -> None:
        if self.table.rowCount() <= 0:
            return
        ids = self._get_selected_ids()
        # --all quando nada está marcado, quando "Marcar tudo" está ativo,
        # ou quando o usuário marcou todos os itens um a um.
        if ids and not self._all_upgrades_selected():
            self._start_worker(
                action="upgrade",
                ids=ids,
                log_header=f"--- ATUALIZAÇÃO (selecionados: {len(ids)}) ---",
            )
            return
        self._start_worker(action="upgrade_all", ids=[], log_header="--- ATUALIZAÇÃO (todas / --all) ---")

    def _on_uninstall_installed_selected(self) -> None:
        ids = self._get_selected_installed_ids()
        if not ids:
            return
        self._start_worker(
            action="uninstall",
            ids=ids,
            log_header=f"--- DESINSTALAÇÃO (instalados selecionados: {len(ids)}) ---",
        )

    def _on_install_search_selected(self) -> None:
        ids = self._get_selected_search_ids()
        if not ids:
            return
        self._start_worker(
            action="install",
            ids=ids,
            log_header=f"--- INSTALAÇÃO (search selecionados: {len(ids)}) ---",
        )

    # _on_install_all removido: regra agora usa upgrade_all (--all) e upgrade por IDs selecionados.

    def _on_worker_error(self, msg: str) -> None:
        # Uma única linha no log — sem QMessageBox.
        text = msg or ""
        if looks_like_clixml(text):
            text = clixml_to_text(text)
        one_line = summarize_one_line(text)
        self._append_log(f"[ERRO] {one_line}")
        if one_line == "Operação cancelada pelo usuário.":
            self._reset_progress_ui()
        self._progress.stop_animation()
        self._set_busy(False)

    def _exec_result_summary(self, action: str, payload: dict) -> tuple[str, str]:
        """Monta prefixo e resumo textual para ações install/upgrade/uninstall."""
        results = payload.get("Results") or []
        fails = [r for r in results if not is_winget_success_exit(r.get("ExitCode"))]
        overall_ok = payload.get("Ok", True)
        label = {
            "install": "Instalação",
            "upgrade": "Atualização",
            "upgrade_all": "Atualização (--all)",
            "uninstall": "Desinstalação",
        }.get(str(action), "Execução")
        n = len(results)
        n_ok = n - len(fails)
        if not fails:
            return "[OK]", f"{label} concluída: {n} pacote(s), todos com sucesso."
        ids_fail = [str(r.get("Id") or "") for r in fails if str(r.get("Id") or "")]
        detail = ", ".join(ids_fail) if len(ids_fail) <= 6 else f"{len(fails)} pacotes"
        summary = (
            f"{label} concluída: {n_ok}/{n} com sucesso. "
            f"Falhas: {detail}."
        )
        tips: list[str] = []
        for r in fails[:3]:
            line = format_exec_result_line(r)
            if line:
                # O prefixo [ERRO]/[AVISO] já vai no status; aqui só o recorte humano.
                tips.append(line.split(" ", 1)[-1] if " " in line else line)
        if tips:
            summary += " Dicas: " + "; ".join(tips) + "."
        if not overall_ok:
            summary += " Retorno JSON com Ok=false."
        return "[AVISO]", summary

    def _on_worker_ok(self, payload: dict) -> None:
        action = payload.get("Action")
        meta = payload.get("Meta") or {}
        if action == "list":
            upgrades = payload.get("Upgrades") or []
            text = payload.get("Text") or []
            if isinstance(text, str):
                text = text.splitlines()
            if (not upgrades) and text:
                upgrades = parse_winget_upgrade(text)
            self._load_upgrades(meta, upgrades)
            self._append_log(f"[OK] {len(upgrades)} updates encontradas em {meta.get('Computer', '')}.")
        elif action == "search":
            results = payload.get("Results") or []
            text = payload.get("Text") or []
            if isinstance(text, str):
                text = text.splitlines()
            if (not results) and text:
                results = parse_winget_search(text)
            self._load_search_results(results)
            self._append_log(f"[OK] {len(results)} resultados para busca.")
        elif action == "installed":
            packages = payload.get("Packages") or []
            text = payload.get("Text") or []
            if isinstance(text, str):
                text = text.splitlines()
            if (not packages) and text:
                packages = parse_winget_list(text)
            self._load_installed(packages)
            self._append_log(f"[OK] {len(packages)} pacotes instalados listados.")
        elif action in ("install", "upgrade", "upgrade_all", "uninstall"):
            results = payload.get("Results") or []
            status_prefix, summary = self._exec_result_summary(str(action), payload)
            for r in results:
                line = format_exec_result_line(r)
                if line:
                    self._append_log(line)
            self._append_log(f"{status_prefix} {summary}")
            self._progress.complete_exec(item_count=len(results))
        else:
            self._append_log("[OK] Ação concluída.")
        self._set_busy(False)

    def _on_installed_list(self) -> None:
        self._start_worker(
            action="installed",
            ids=[],
            log_header=f"--- INSTALADOS {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---",
        )

    def _make_checkbox_cell(self, on_changed) -> QWidget:
        chk = QCheckBox()
        chk.setObjectName("rowChk")
        container = QWidget()
        container._row_chk = chk
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(chk)
        chk.toggled.connect(lambda _checked: on_changed())
        return container

    def _text_item(self, value: object) -> QTableWidgetItem:
        text = str(value or "")
        item = QTableWidgetItem(text)
        if text:
            item.setToolTip(text)
        return item

    def _load_installed(self, packages: list[dict]) -> None:
        self.inst_table.setSortingEnabled(False)
        self.inst_table.setRowCount(0)
        for p in packages:
            row = self.inst_table.rowCount()
            self.inst_table.insertRow(row)

            chk_container = self._make_checkbox_cell(self._on_installed_selection_changed)
            self.inst_table.setCellWidget(row, 0, chk_container)

            self.inst_table.setItem(row, 1, self._text_item(p.get("Name", "")))
            self.inst_table.setItem(row, 2, self._text_item(p.get("Id", "")))
            self.inst_table.setItem(row, 3, self._text_item(p.get("Version", "")))
            self.inst_table.setItem(row, 4, self._text_item(p.get("Available", "")))
            self.inst_table.setItem(row, 5, self._text_item(p.get("Source", "")))

        self.inst_table.setSortingEnabled(True)
        self.inst_count.setText(f"{self.inst_table.rowCount()} itens")
        self.inst_mark_all.blockSignals(True)
        self.inst_mark_all.setChecked(False)
        self.inst_mark_all.blockSignals(False)
        self._refresh_installed_bulk_buttons()
        self._preview_pending_uninstall()

    def _load_search_results(self, results: list[dict]) -> None:
        self.search_table.setSortingEnabled(False)
        self.search_table.setRowCount(0)
        for r in results:
            row = self.search_table.rowCount()
            self.search_table.insertRow(row)

            chk_container = self._make_checkbox_cell(self._on_search_selection_changed)
            self.search_table.setCellWidget(row, 0, chk_container)

            self.search_table.setItem(row, 1, self._text_item(r.get("Name", "")))
            self.search_table.setItem(row, 2, self._text_item(r.get("Id", "")))
            self.search_table.setItem(row, 3, self._text_item(r.get("Version", "")))
            self.search_table.setItem(row, 4, self._text_item(r.get("Match", "")))
            self.search_table.setItem(row, 5, self._text_item(r.get("Source", "")))

        self.search_table.setSortingEnabled(True)
        self.search_count.setText(f"{self.search_table.rowCount()} itens")
        self.search_mark_all.blockSignals(True)
        self.search_mark_all.setChecked(False)
        self.search_mark_all.blockSignals(False)
        self._refresh_search_bulk_buttons()
        self._preview_pending_search()

    def _on_item_started(self, idx: int, total: int, package_id: str) -> None:
        if self._exec_log.should_skip_item_started(package_id):
            return
        self._progress.on_item_started(idx, total, package_id)

    def _on_item_finished(self, idx: int, total: int, package_id: str, exit_code: int, output: str) -> None:
        hint = summarize_winget_output(output)
        if is_winget_success_exit(exit_code, if_missing=1):
            suffix = f"OK — {hint}" if hint else "OK"
        else:
            suffix = hint if hint else f"falhou (exit={exit_code})"
        self._progress.on_item_finished(
            idx, total, package_id, suffix, stream_done=self._exec_log.is_stream_finished(package_id)
        )

    def _on_item_progress(self, pct: int) -> None:
        self._progress.on_percent(pct)

    def _load_upgrades(self, meta: dict, upgrades: list[dict]) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for u in upgrades:
            row = self.table.rowCount()
            self.table.insertRow(row)

            chk_container = self._make_checkbox_cell(self._on_upgrades_selection_changed)
            self.table.setCellWidget(row, 0, chk_container)

            self.table.setItem(row, 1, self._text_item(u.get("Name", "")))
            self.table.setItem(row, 2, self._text_item(u.get("Id", "")))
            self.table.setItem(row, 3, self._text_item(u.get("Version", "")))
            self.table.setItem(row, 4, self._text_item(u.get("Available", "")))
            self.table.setItem(row, 5, self._text_item(u.get("Source", "")))

        self.table.setSortingEnabled(True)
        self.lbl_count.setText(f"{self.table.rowCount()} itens")
        self.chk_all.blockSignals(True)
        self.chk_all.setChecked(False)
        self.chk_all.blockSignals(False)
        self._refresh_upgrades_bulk_buttons()
        self._preview_pending_upgrade()

