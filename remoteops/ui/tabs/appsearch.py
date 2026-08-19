from __future__ import annotations

import os
from dataclasses import dataclass
from queue import Queue
from typing import List, Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QWidget,
)

from remoteops.ui.style import (
    COLOR_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    RADIUS_SMALL,
    SIZE_UI_SMALL,
    table_frame_qss,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    grid_in_card,
    make_card_stack,
    make_field_label,
)
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.utils.app_catalog import resolve_uninstall_extras
from remoteops.utils.hosts import load_hosts_file, save_hosts_file
from remoteops.utils.network_range import (
    get_network_range_config,
    ips_for_config,
    network_range_search_mode,
)
from remoteops.utils.network_scan import scan_windows_hosts
from remoteops.utils.psinfo import (
    InstalledApp,
    build_uninstall_remote_cmd,
    describe_uninstall,
)
from remoteops.utils.remote_registry_query import (
    get_remote_registry_timeout,
    run_remote_inventory_batch,
)
from remoteops.utils.search_settings import (
    MAX_SEARCH_MAX_WORKERS,
    MIN_SEARCH_MAX_WORKERS,
    get_search_max_workers,
    resolve_configured_hosts_path,
)


def parse_app_results_filter(raw: str) -> tuple[List[str], List[str]]:
    """Separa o filtro em inclusões (OR) e exclusões (NOT).

    Termos separados por ``;``. Um termo com prefixo ``-`` é exclusão.
    """
    include: List[str] = []
    exclude: List[str] = []
    text = (raw or "").strip().lower()
    if not text:
        return include, exclude
    for part in text.split(";"):
        term = part.strip()
        if not term:
            continue
        if term.startswith("-"):
            term = term[1:].strip()
            if term:
                exclude.append(term)
            continue
        include.append(term)
    return include, exclude


def row_matches_app_results_filter(
    haystack: str, include: List[str], exclude: List[str]
) -> bool:
    """True se o texto da linha passa nas inclusões (OR) e em nenhuma exclusão."""
    text = haystack.lower()
    if include and not any(term in text for term in include):
        return False
    if exclude and any(term in text for term in exclude):
        return False
    return True


@dataclass
class SearchHit:
    host: str
    app: InstalledApp
    # status do host no momento do hit (consultado com sucesso)
    host_ok: bool = True


class _AppSearchWorker(QThread):
    # generation, done, failed, total, último host, ok, error_kind
    progress = pyqtSignal(int, int, int, int, str, bool, str)
    # generation, List[SearchHit]
    hitsFound = pyqtSignal(int, list)
    finished_ok = pyqtSignal(int, str)  # generation, query
    finished_aborted = pyqtSignal(int, str)  # generation, query
    finished_err = pyqtSignal(int, str)  # generation, msg

    def __init__(
        self,
        hosts: List[str],
        query: str,
        max_workers: int = 8,
        *,
        generation: int = 0,
        timeout: Optional[float] = None,
        inbox: Optional[Queue] = None,
    ):
        super().__init__()
        self.hosts = list(hosts)
        self.query = (query or "").strip()
        try:
            n = int(max_workers)
        except (TypeError, ValueError):
            n = 8
        self.max_workers = max(MIN_SEARCH_MAX_WORKERS, min(MAX_SEARCH_MAX_WORKERS, n))
        self.generation = int(generation)
        self.timeout = float(timeout) if timeout else get_remote_registry_timeout()
        self._inbox = inbox
        self._offered = len(self.hosts)
        self._intake_closed = inbox is None
        self._abort = False

    def offer_host(self, host: str) -> None:
        """Enfileira um host descoberto (varredura em andamento)."""
        if self._inbox is None or self._intake_closed or self._abort:
            return
        h = (host or "").strip().strip("\\")
        if not h:
            return
        self._offered += 1
        self._inbox.put(h)

    def close_intake(self) -> None:
        """Não haverá mais hosts da varredura."""
        if self._inbox is None or self._intake_closed:
            return
        self._intake_closed = True
        self._inbox.put(None)

    def abort(self) -> None:
        self._abort = True
        self.close_intake()

    def run(self) -> None:
        gen = self.generation
        try:
            q = self.query.casefold()
            if not q:
                self.finished_err.emit(gen, "Informe o nome do aplicativo a pesquisar.")
                return
            streaming = self._inbox is not None
            if not streaming and not self.hosts:
                self.finished_err.emit(gen, "Nenhum host para consultar.")
                return

            total = len(self.hosts)
            workers = self.max_workers if streaming else min(self.max_workers, max(1, total))
            done = 0
            failed = 0
            saw_cancel = False
            saw_any = False

            for status in run_remote_inventory_batch(
                self.hosts,
                max_workers=workers,
                timeout=self.timeout,
                should_cancel=lambda: self._abort,
                extra_hosts=self._inbox,
            ):
                saw_any = True
                if streaming:
                    total = max(done + 1, int(self._offered))
                if self._abort and status.error_kind == "cancelled":
                    saw_cancel = True
                    # Não conta hosts cancelados (já iniciados) como "falha de rede";
                    # ainda incrementa progresso para refletir vagas liberadas.
                    done += 1
                    self.progress.emit(
                        gen, done, failed, total, status.host, False, "cancelled"
                    )
                    continue

                hits: List[SearchHit] = []
                ok = bool(status.ok)
                error_kind = status.error_kind or ("" if ok else "internal_error")
                if ok:
                    for app in status.apps:
                        name = (app.display_name or "").casefold()
                        if q in name:
                            hits.append(
                                SearchHit(host=status.host, app=app, host_ok=True)
                            )
                done += 1
                if not ok:
                    failed += 1
                self.progress.emit(
                    gen, done, failed, total, status.host, ok, error_kind
                )
                if hits:
                    self.hitsFound.emit(gen, hits)

            if self._abort or saw_cancel:
                self.finished_aborted.emit(gen, self.query)
                return
            if not streaming and not saw_any:
                self.finished_err.emit(gen, "Nenhum host para consultar.")
                return
            final_total = max(done, int(self._offered) if streaming else total)
            self.progress.emit(gen, done, failed, final_total, "", True, "")
            self.finished_ok.emit(gen, self.query)
        except Exception as exc:
            self.finished_err.emit(gen, f"Erro na pesquisa: {exc}")


class _NetworkScanWorker(QThread):
    """Varre a faixa de IP em segundo plano (ping + portas Windows + nome)."""

    progress = pyqtSignal(int, int, int, str, int)  # gen, done, total, last_ip, found
    hostFound = pyqtSignal(int, str)  # generation, hostname
    finished_ok = pyqtSignal(int, list)  # generation, hosts
    finished_aborted = pyqtSignal(int)
    finished_err = pyqtSignal(int, str)

    def __init__(
        self,
        ips: List[str],
        max_workers: int,
        *,
        generation: int = 0,
    ):
        super().__init__()
        self.ips = list(ips)
        self.max_workers = int(max_workers)
        self.generation = int(generation)
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        gen = self.generation
        try:
            def on_progress(done: int, total: int, ip: str, found: int) -> None:
                self.progress.emit(gen, done, total, ip, found)

            def on_host(name: str) -> None:
                self.hostFound.emit(gen, name)

            hosts = scan_windows_hosts(
                self.ips,
                max_workers=self.max_workers,
                should_cancel=lambda: self._abort,
                on_progress=on_progress,
                on_host=on_host,
            )
            if self._abort:
                self.finished_aborted.emit(gen)
                return
            self.finished_ok.emit(gen, hosts)
        except Exception as exc:
            self.finished_err.emit(gen, f"Erro na varredura de rede: {exc}")


class AppSearchTab(QWidget):
    """Tela de pesquisa de aplicativos instalados em múltiplos hosts."""

    # host, remote_cmd, rótulo do app (para log)
    uninstallRequested = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_AppSearchWorker] = None
        self._scan_worker: Optional[_NetworkScanWorker] = None
        self._hosts_path = ""
        self._hits: List[SearchHit] = []
        self._active_query = ""
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = 0
        self._search_from_network = False
        self._scan_ips_done = 0
        self._scan_ips_total = 0
        self._scan_hosts_found = 0
        # Após interrupção, ignora sinais tardios do worker antigo
        self._accepting_search_results = False
        self._search_generation = 0

        # Pesquisa no topo; Resultados + Console de Saída próprios (não misturam com a main)
        root = make_card_stack(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bottom_stretch_idx = None

        # ── Card Pesquisa ──────────────────────────────────────────────
        search_card = CardWidget("\uE721", self.tr("Pesquisa"))
        search_card.set_collapsible(True, collapsed=False)
        self.search_btn = search_card.make_header_button(
            "\uE721", self.tr("Pesquisar")
        )
        self.search_btn.clicked.connect(self.start_search)
        search_card.add_header_button(self.search_btn)
        self.stop_btn = search_card.make_header_button(
            "\uE71A", self.tr("Parar pesquisa")
        )
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_search)
        search_card.add_header_button(self.stop_btn)
        grid = grid_in_card(search_card)

        self.app_edit = QLineEdit()
        self.app_edit.setPlaceholderText(self.tr("Nome completo ou parte do nome do aplicativo"))
        self.app_edit.returnPressed.connect(self.start_search)
        add_row(grid, 0, self.tr("Aplicativo a pesquisar"), self.app_edit)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.hosts_status_dot = _StatusDot()
        self.hosts_status_lbl = QLabel("")
        self.hosts_status_lbl.setObjectName("hostsStatus")
        self.hosts_status_lbl.setStyleSheet(
            f"QLabel#hostsStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        status_row.addWidget(self.hosts_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.hosts_status_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch()
        status_wrap = QWidget()
        status_wrap.setLayout(status_row)
        status_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        status_wrap.setToolTip(
            self.tr(
                "Definido em Configurações. Com a faixa de IP ativada, varre a rede; "
                "senão usa hosts.json."
            )
        )
        add_row(grid, 1, self.tr("Status"), status_wrap)

        def _search_bar(fmt: str) -> QProgressBar:
            bar = QProgressBar()
            bar.setMinimum(0)
            bar.setMaximum(1)
            bar.setValue(0)
            bar.setTextVisible(True)
            bar.setFormat(fmt)
            bar.setFixedHeight(18)
            return bar

        self.scan_progress = _search_bar("%v / %m IPs")
        self.scan_progress.setToolTip(self.tr("IPs verificados na faixa configurada"))
        scan_wrap = QWidget()
        scan_lay = QHBoxLayout(scan_wrap)
        scan_lay.setContentsMargins(0, 0, 0, 0)
        scan_lay.setSpacing(8)
        scan_lay.addWidget(self.scan_progress, 1)
        self._scan_row_label = make_field_label(self.tr("Rede"))
        grid.addWidget(self._scan_row_label, 2, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(scan_wrap, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        self._scan_row_wrap = scan_wrap

        self.progress = _search_bar("%v / %m")
        self.progress.setToolTip(self.tr("Hosts consultados / encontrados"))
        self.ok_count_lbl = QLabel(self.tr("Sucesso: 0"))
        self.fail_count_lbl = QLabel(self.tr("Falharam: 0"))
        self.ok_count_lbl.setStyleSheet(
            "color: palette(highlight); font-weight: 600;"
        )
        self.fail_count_lbl.setStyleSheet(
            "color: #c42b1c; font-weight: 600;"
        )
        hosts_wrap = QWidget()
        hosts_lay = QHBoxLayout(hosts_wrap)
        hosts_lay.setContentsMargins(0, 0, 0, 0)
        hosts_lay.setSpacing(12)
        hosts_lay.addWidget(self.progress, 1)
        hosts_lay.addWidget(self.ok_count_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        hosts_lay.addWidget(self.fail_count_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        self._hosts_row_label = make_field_label(self.tr("Hosts"))
        grid.addWidget(self._hosts_row_label, 3, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(hosts_wrap, 3, 1, Qt.AlignmentFlag.AlignVCenter)
        self._hosts_row_wrap = hosts_wrap

        self.phase_lbl = QLabel("")
        self.phase_lbl.setObjectName("searchPhase")
        self.phase_lbl.setWordWrap(True)
        self.phase_lbl.setStyleSheet(
            f"QLabel#searchPhase {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        grid.addWidget(self.phase_lbl, 4, 0, 1, 2)
        self._set_progress_rows_visible(False, network=False)

        root.addWidget(search_card, 0)

        # ── Card Resultados ────────────────────────────────────────────
        self.results_card = CardWidget("\uE71D", self.tr("Resultados"))
        self.results_card.set_collapsible(True, collapsed=False)
        self.results_card.set_expanding(True)
        self.results_card.set_layout_stretch(2)
        self.results_card.set_downloadable(True)
        self._results_download_btn = self.results_card.findChild(QToolButton, "cardDownload")
        if self._results_download_btn is not None:
            self._results_download_btn.setToolTip(
                self.tr("Exportar computadores dos resultados como hosts.json")
            )
            self._results_download_btn.setEnabled(False)
        self.results_card.downloadRequested.connect(self._export_results_hosts_json)
        results_card = self.results_card

        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        results_card.content_layout.addWidget(self.summary_lbl, 0)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            self.tr("Buscar aplicativo... (vários com ; · excluir com -)")
        )
        self.filter_edit.setToolTip(
            self.tr(
                "Separe termos com ; (qualquer um). Prefixe com - para excluir.\n"
                "Ex.: chrome; firefox   |   -edge   |   office; -365"
            )
        )
        self.filter_count_lbl = QLabel("")
        self.filter_count_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        filter_row.addWidget(self.filter_edit)
        filter_row.addWidget(self.filter_count_lbl)
        filter_wrap = QWidget()
        filter_wrap.setLayout(filter_row)
        results_card.content_layout.addWidget(filter_wrap, 0)
        self.filter_edit.textChanged.connect(self._apply_results_filter)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Computador"),
                self.tr("Nome"),
                self.tr("Editor"),
                self.tr("Versão"),
                self.tr("Tipo"),
                self.tr("Ações"),
            ]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(5, 48)
        self.table.setStyleSheet(
            table_frame_qss()
            + "QTableWidget::item { padding: 4px 6px; }"
        )
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setMinimumHeight(80)
        results_card.content_layout.addWidget(self.table, 1)

        extras_row = QHBoxLayout()
        extras_row.setContentsMargins(0, 4, 0, 0)
        extras_row.setSpacing(10)
        extras_lbl = make_field_label(self.tr("Parametros Extras"))
        self.extras_edit = QLineEdit()
        self.extras_edit.setPlaceholderText(
            self.tr("Opcional — vazio usa ApplicationCatalog.json. EXE: /S. MSI: REBOOT=ReallySuppress")
        )
        self.extras_edit.setToolTip(
            self.tr(
                "Se preenchido, sobrescreve o ApplicationCatalog.json.\n"
                "Se vazio, usa uninstallArgs do catálogo quando o app for reconhecido.\n"
                "EXE: switches do fabricante (WinRAR: /S).\n"
                "MSI: adicionais além de /qn /norestart."
            )
        )
        extras_row.addWidget(extras_lbl)
        extras_row.addWidget(self.extras_edit, 1)
        extras_wrap = QWidget()
        extras_wrap.setLayout(extras_row)
        results_card.content_layout.addWidget(extras_wrap, 0)
        self._trash_buttons: list = []
        self.extras_edit.textChanged.connect(lambda _t: self._refresh_trash_tooltips())

        root.addWidget(results_card, 2)

        # Console de saída exclusivo da Pesquisa (desinstalação segue em terminal externo)
        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(1)
        root.addWidget(self.log_output, 1)

        self.results_card.collapsedChanged.connect(self._redistribute_expandable_space)
        self.log_output.collapsedChanged.connect(self._redistribute_expandable_space)
        self._redistribute_expandable_space()

        self.destroyed.connect(self._abort_worker)
        self.refresh_hosts_status()

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
        """Divide o espaço entre Resultados e Console abertos; cards ficam no topo."""
        lay = self.layout()
        if lay is None:
            return
        open_cards = []
        for w, stretch in ((self.results_card, 2), (self.log_output, 1)):
            idx = lay.indexOf(w)
            if idx < 0:
                continue
            if w.is_collapsed:
                lay.setStretch(idx, 0)
            else:
                open_cards.append(w)
                w.set_layout_stretch(stretch)
                lay.setStretch(idx, stretch)

        # Sem AlignTop: stretch final mantém cabeçalhos no topo ao recolher tudo.
        need_tail = len(open_cards) == 0
        if need_tail:
            if getattr(self, "_bottom_stretch_idx", None) is None:
                lay.addStretch(1)
                self._bottom_stretch_idx = lay.count() - 1
            else:
                lay.setStretch(self._bottom_stretch_idx, 1)
        elif getattr(self, "_bottom_stretch_idx", None) is not None:
            lay.setStretch(self._bottom_stretch_idx, 0)

        lay.activate()
        self.updateGeometry()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_hosts_status()

    def _ui_alive(self) -> bool:
        return not sip.isdeleted(self)

    def _set_hosts_status(self, state: str, text: str, tooltip: str = "") -> None:
        color = _STATUS_COLORS.get(state, _STATUS_COLORS["idle"])
        self.hosts_status_dot.set_color(color)
        self.hosts_status_lbl.setText(text)
        tip = (tooltip or text).strip()
        self.hosts_status_dot.setToolTip(tip)
        self.hosts_status_lbl.setToolTip(tip)

    def _set_progress_rows_visible(self, visible: bool, *, network: bool = False) -> None:
        show_scan = bool(visible and network)
        self._scan_row_label.setVisible(show_scan)
        self._scan_row_wrap.setVisible(show_scan)
        self._hosts_row_label.setVisible(visible)
        self._hosts_row_wrap.setVisible(visible)
        if not visible:
            self._set_phase_message("")

    def _set_phase_message(self, text: str = "") -> None:
        msg = (text or "").strip()
        self.phase_lbl.setText(msg)
        self.phase_lbl.setVisible(bool(msg))

    def _refresh_progress_ui(self) -> None:
        """Atualiza as duas barras (Rede / Hosts) sem textos longos misturados."""
        if self._search_from_network:
            ip_total = max(1, int(self._scan_ips_total))
            self.scan_progress.setMaximum(ip_total)
            self.scan_progress.setValue(min(int(self._scan_ips_done), ip_total))
            found = max(int(self._scan_hosts_found), int(self._hosts_total), 0)
            if found <= 0:
                self.progress.setMaximum(1)
                self.progress.setValue(0)
                self.progress.setFormat(self.tr("0 encontrados"))
            else:
                self.progress.setMaximum(found)
                self.progress.setValue(min(int(self._hosts_done), found))
                self.progress.setFormat("%v / %m")
        else:
            host_total = max(1, int(self._hosts_total))
            self.progress.setMaximum(host_total)
            self.progress.setValue(min(int(self._hosts_done), host_total))
            self.progress.setFormat("%v / %m")
        ok = max(0, int(self._hosts_done) - int(self._hosts_failed))
        self.ok_count_lbl.setText(self.tr(f"Sucesso: {ok}"))
        self.fail_count_lbl.setText(self.tr(f"Falharam: {self._hosts_failed}"))

    def refresh_hosts_status(self) -> None:
        """Atualiza legenda a partir das Configurações (faixa de IP ou hosts.json)."""
        mode, err, ip_count = network_range_search_mode()
        if mode == "network":
            self._hosts_path = ""
            cfg = get_network_range_config()
            self._set_hosts_status(
                "ok",
                self.tr(f"Faixa {cfg.start_ip}–{cfg.end_ip} ({ip_count} IPs)"),
                self.tr(
                    f"Varredura de rede: {cfg.start_ip}–{cfg.end_ip}. "
                    f"{ip_count} IP(s), {cfg.scan_threads} threads. "
                    "hosts.json não é usado."
                ),
            )
            return
        if mode == "invalid":
            self._hosts_path = ""
            self._set_hosts_status("invalid", err or self.tr("Faixa de IP inválida"))
            return

        path, origin = resolve_configured_hosts_path()
        if origin == "missing" or not path or not os.path.isfile(path):
            self._hosts_path = ""
            self._set_hosts_status("err", self.tr("Não encontrado"))
            return
        p = os.path.normpath(path)
        if len(p) >= 2 and p[1] == ":":
            p = p[0].upper() + p[1:]
        try:
            hosts = load_hosts_file(p)
        except Exception:
            self._hosts_path = ""
            self._set_hosts_status("invalid", self.tr("Arquivo inválido"))
            return
        if not hosts:
            self._hosts_path = p
            self._set_hosts_status("warn", self.tr("Encontrado — lista vazia"))
            return
        self._hosts_path = p
        self._set_hosts_status(
            "ok", self.tr(f"Encontrado — {len(hosts)} host(s)")
        )

    def _disconnect_scan_signals(self, w: _NetworkScanWorker) -> None:
        for signal, slot in (
            (w.progress, self._on_scan_progress),
            (w.hostFound, self._on_scan_host_found),
            (w.finished_ok, self._on_scan_ok),
            (w.finished_aborted, self._on_scan_aborted),
            (w.finished_err, self._on_scan_err),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

    def _is_busy(self) -> bool:
        w = self._worker
        s = self._scan_worker
        return bool(
            (w is not None and w.isRunning())
            or (s is not None and s.isRunning())
        )

    def _set_search_busy(self, busy: bool) -> None:
        self.search_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.app_edit.setEnabled(not busy)

    def _abort_scan_worker(self) -> None:
        w = self._scan_worker
        if w is None:
            return
        self._scan_worker = None
        self._disconnect_scan_signals(w)
        w.abort()
        if w.isRunning():
            w.wait(4000)
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def _disconnect_worker_signals(self, w: _AppSearchWorker) -> None:
        for signal, slot in (
            (w.progress, self._on_progress),
            (w.hitsFound, self._on_hits_found),
            (w.finished_ok, self._on_search_ok),
            (w.finished_aborted, self._on_search_aborted),
            (w.finished_err, self._on_search_err),
            (w.finished, self._on_worker_finished),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

    def _is_current_generation(self, generation: int) -> bool:
        return (
            self._accepting_search_results
            and int(generation) == int(self._search_generation)
        )

    def _abort_worker(self, _destroyed: object = None) -> None:
        self._abort_scan_worker()
        w = self._worker
        if w is None:
            return
        self._worker = None
        self._accepting_search_results = False
        self._disconnect_worker_signals(w)
        w.abort()
        if w.isRunning():
            # Aguarda o lote encerrar processos filhos (terminate + join).
            w.wait(max(3000, int(get_remote_registry_timeout() * 1000) // 3))
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def shutdown(self, wait_ms: int = 8000) -> None:
        """Aborta varredura/pesquisa, encerra filhos RR e espera a QThread."""
        self._accepting_search_results = False
        self._abort_scan_worker()
        w = self._worker
        if w is None:
            return
        self._worker = None
        self._disconnect_worker_signals(w)
        w.abort()
        if w.isRunning():
            w.wait(max(0, int(wait_ms)))
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def stop_search(self) -> None:
        """
        Interrompe a pesquisa: para o agendamento, encerra processos filhos
        ativos e descarta resultados posteriores.
        """
        scan = self._scan_worker
        worker = self._worker
        scanning = scan is not None and scan.isRunning()
        searching = worker is not None and worker.isRunning()
        if not scanning and not searching:
            return
        self._accepting_search_results = False
        if scanning:
            scan.abort()
        if searching:
            worker.abort()
        elif scanning:
            w = self._worker
            if w is not None:
                w.abort()
        self.stop_btn.setEnabled(False)
        self._set_phase_message(self.tr("Interrompendo..."))
        self.log_output.append_log(
            self.tr(
                "[PESQUISA] Interrupção solicitada. "
                "Consultas ativas estão sendo encerradas."
            )
        )

    def start_search(self) -> None:
        if self._is_busy():
            return

        query = (self.app_edit.text() or "").strip()
        if not query:
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Aplicativos"),
                self.tr("Informe o aplicativo a pesquisar."),
            )
            return

        self.refresh_hosts_status()
        mode, err, _ip_count = network_range_search_mode()
        if mode == "invalid":
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Aplicativos"),
                self.tr(
                    err
                    or "Faixa de IP inválida. Corrija em Configurações ou limpe os campos."
                ),
            )
            return
        if mode == "network":
            self._start_network_scan(query)
            return

        if not self._hosts_path or not os.path.isfile(self._hosts_path):
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Aplicativos"),
                self.tr(
                    "hosts.json não encontrado.\n"
                    "Configure o arquivo ou a faixa de IP em Configurações."
                ),
            )
            return

        try:
            hosts = load_hosts_file(self._hosts_path)
        except Exception as exc:
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Aplicativos"),
                self.tr(f"Não foi possível ler o arquivo de hosts:\n{exc}"),
            )
            return

        self._begin_app_search(hosts, query)

    def _start_network_scan(self, query: str) -> None:
        cfg = get_network_range_config()
        ips, err = ips_for_config(cfg)
        if err or not ips:
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Aplicativos"),
                self.tr(err or "Nenhum IP para varrer."),
            )
            return

        self._hits = []
        self._trash_buttons = []
        self._active_query = query
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = 0
        self._search_from_network = True
        self._scan_ips_done = 0
        self._scan_ips_total = len(ips)
        self._scan_hosts_found = 0
        self._search_generation += 1
        generation = self._search_generation
        self._accepting_search_results = True
        self.table.setRowCount(0)
        self._apply_results_filter()
        self.summary_lbl.setText(self.tr("Varrendo a rede..."))
        self._set_search_busy(True)
        self._set_progress_rows_visible(True, network=True)
        self._set_phase_message("")
        self._refresh_progress_ui()

        self._begin_streaming_search(query, generation)

        self._scan_worker = _NetworkScanWorker(
            ips,
            cfg.scan_threads,
            generation=generation,
        )
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.hostFound.connect(self._on_scan_host_found)
        self._scan_worker.finished_ok.connect(self._on_scan_ok)
        self._scan_worker.finished_aborted.connect(self._on_scan_aborted)
        self._scan_worker.finished_err.connect(self._on_scan_err)
        self._scan_worker.start()

        self.log_output.append_log(
            self.tr(
                f"[PESQUISA] Varrendo {cfg.start_ip}–{cfg.end_ip} "
                f"({len(ips)} IP(s), {cfg.scan_threads} threads) "
                f"e pesquisando '{query}' em cada host encontrado..."
            )
        )

    def _on_scan_progress(
        self,
        generation: int,
        done: int,
        total: int,
        _ip: str,
        found: int,
    ) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._scan_ips_done = done
        self._scan_ips_total = total
        self._scan_hosts_found = found
        self._refresh_progress_ui()

    def _on_scan_host_found(self, generation: int, host: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        name = (host or "").strip()
        if not name:
            return
        w = self._worker
        if w is not None:
            w.offer_host(name)
        if self._accepting_search_results and not self._hits:
            self.summary_lbl.setText(self.tr("Pesquisando..."))

    def _on_scan_ok(self, generation: int, hosts: list) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        names = [str(h).strip() for h in hosts if str(h).strip()]
        w = self._worker
        if w is not None:
            w.close_intake()
        self._scan_hosts_found = len(names)
        self._hosts_total = len(names)
        self._scan_ips_done = self._scan_ips_total
        self._refresh_progress_ui()
        self.log_output.append_log(
            self.tr(
                f"[PESQUISA] Varredura concluída: {len(names)} host(s) Windows. "
                "Consultas em andamento atualizam os resultados."
            )
        )

    def _on_scan_aborted(self, generation: int) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        w = self._worker
        if w is not None:
            w.abort()
        if w is not None and w.isRunning():
            self._set_phase_message(self.tr("Interrompendo..."))
            return
        self._accepting_search_results = False
        self._set_search_busy(False)
        self._set_phase_message(self.tr("Varredura interrompida"))
        self.summary_lbl.setText(self.tr("Varredura de rede interrompida."))
        self.log_output.append_log(self.tr("[PESQUISA] Varredura de rede interrompida."))

    def _on_scan_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        w = self._worker
        if w is not None:
            w.abort()
        if w is not None and w.isRunning():
            self.log_output.append_log(self.tr(f"[PESQUISA] {msg}"))
            return
        self._accepting_search_results = False
        self._set_search_busy(False)
        self._set_phase_message(self.tr(f"Falha: {msg}"))
        self.log_output.append_log(self.tr(f"[PESQUISA] {msg}"))

    def _begin_streaming_search(self, query: str, generation: int) -> None:
        configured_workers = get_search_max_workers()
        rr_timeout = get_remote_registry_timeout()
        inbox: Queue = Queue()
        self._worker = _AppSearchWorker(
            [],
            query,
            max_workers=configured_workers,
            generation=generation,
            timeout=rr_timeout,
            inbox=inbox,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.hitsFound.connect(self._on_hits_found)
        self._worker.finished_ok.connect(self._on_search_ok)
        self._worker.finished_aborted.connect(self._on_search_aborted)
        self._worker.finished_err.connect(self._on_search_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _begin_app_search(
        self,
        hosts: List[str],
        query: str,
        *,
        new_generation: bool = True,
    ) -> None:
        self._search_from_network = False
        if new_generation:
            self._hits = []
            self._trash_buttons = []
            self.table.setRowCount(0)
            self._apply_results_filter()
            self._search_generation += 1
        self._active_query = query
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = len(hosts)
        generation = self._search_generation
        self._accepting_search_results = True
        self.summary_lbl.setText(self.tr("Pesquisando..."))
        self._set_search_busy(True)
        self._set_progress_rows_visible(True, network=False)
        self._set_phase_message("")
        self._refresh_progress_ui()

        configured_workers = get_search_max_workers()
        effective_workers = min(configured_workers, max(1, len(hosts)))
        rr_timeout = get_remote_registry_timeout()
        self._worker = _AppSearchWorker(
            hosts,
            query,
            max_workers=effective_workers,
            generation=generation,
            timeout=rr_timeout,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.hitsFound.connect(self._on_hits_found)
        self._worker.finished_ok.connect(self._on_search_ok)
        self._worker.finished_aborted.connect(self._on_search_aborted)
        self._worker.finished_err.connect(self._on_search_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

        self.log_output.append_log(
            self.tr(
                f"[PESQUISA] Buscando '{query}' em {len(hosts)} host(s) "
                f"({effective_workers} consultas simultâneas, "
                f"timeout {int(rr_timeout)}s/host)..."
            )
        )

    def _on_progress(
        self,
        generation: int,
        done: int,
        failed: int,
        total: int,
        host: str,
        _ok: bool,
        error_kind: str = "",
    ) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._hosts_done = done
        self._hosts_failed = failed
        self._hosts_total = total
        self._refresh_progress_ui()
        if host and not _ok and error_kind:
            kind_labels = {
                "auth": "falha de autenticação",
                "remote_registry": "Remote Registry/RPC indisponível",
                "unreachable": "host inacessível",
                "invalid_host": "host inválido",
                "timed_out": "consulta expirada (timeout)",
                "cancelled": "consulta cancelada",
                "internal_error": "erro interno na consulta",
            }
            label = kind_labels.get(error_kind, error_kind)
            self.log_output.append_log(
                self.tr(f"[PESQUISA] {host}: {label}")
            )
        if self._accepting_search_results:
            self._update_summary(final=False)

    def _on_worker_finished(self) -> None:
        if not self._ui_alive():
            return
        scan = self._scan_worker
        if scan is not None and scan.isRunning():
            return
        self._set_search_busy(False)

    def _on_search_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._accepting_search_results = False
        self._set_phase_message(self.tr(f"Falha: {msg}"))
        self.log_output.append_log(self.tr(f"[PESQUISA] {msg}"))

    def _on_hits_found(self, generation: int, hits: list) -> None:
        """Exibe imediatamente as correspondências do host recém-consultado."""
        if not self._ui_alive() or not self._is_current_generation(generation) or not hits:
            return
        for hit in hits:
            if isinstance(hit, SearchHit):
                self._hits.append(hit)
                self._append_hit_row(hit)
        self._apply_results_filter()
        self._update_summary(final=False)

    def _on_search_ok(self, generation: int, query: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._accepting_search_results = False
        self._active_query = query or self._active_query
        total = int(self._hosts_total or 0) or int(self._hosts_done or 0)
        failed = self._hosts_failed
        if self._search_from_network and total == 0 and not self._hits:
            self._refresh_progress_ui()
            self._set_phase_message(self.tr("Nenhum host Windows encontrado na faixa."))
            self.summary_lbl.setText(self.tr("Nenhum computador encontrado na varredura."))
            self.log_output.append_log(
                self.tr("[PESQUISA] Varredura concluída sem hosts Windows.")
            )
            QMessageBox.information(
                self,
                self.tr("Pesquisa de Aplicativos"),
                self.tr(
                    "Nenhum computador Windows foi encontrado na faixa de IP.\n"
                    "Verifique o intervalo, as sub-redes ignoradas e a rede."
                ),
            )
            return
        self._hosts_done = total
        self._hosts_total = total
        self._refresh_progress_ui()
        self._set_phase_message("")
        self._update_summary(final=True)
        computers = {h.host.casefold() for h in self._hits}
        self.log_output.append_log(
            self.tr(
                f"[PESQUISA] Concluída: {len(computers)} computador(es) com app, "
                f"{len(self._hits)} correspondência(s), "
                f"{failed} host(s) falharam."
            )
        )

    def _on_search_aborted(self, generation: int, query: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._accepting_search_results = False
        self._active_query = query or self._active_query
        done = self._hosts_done
        total = self._hosts_total or self._scan_hosts_found
        failed = self._hosts_failed
        self._refresh_progress_ui()
        self._set_phase_message(self.tr("Pesquisa interrompida"))
        self._update_summary(final=True, interrupted=True)
        computers = {h.host.casefold() for h in self._hits}
        self.log_output.append_log(
            self.tr(
                f"[PESQUISA] Interrompida: {len(computers)} computador(es) com app, "
                f"{len(self._hits)} correspondência(s) até o momento "
                f"({done} de {total} hosts processados, {failed} falha(s)). "
                "Consultas ativas foram encerradas."
            )
        )

    def _apply_results_filter(self) -> None:
        """Filtra a tabela de resultados (mesmo padrão do PsInfo: nome/editor/versão/tipo + computador).

        Termos separados por `;` (OR). Prefixo ``-`` exclui o termo (NOT).
        Só exclusões: mostra todos menos os que casam. Inclusão + exclusão: OR das
        inclusões e depois remove as exclusões.
        """
        if not self._ui_alive():
            return
        include, exclude = parse_app_results_filter(self.filter_edit.text() or "")
        total = self.table.rowCount()
        visible = 0
        for r in range(total):
            parts = []
            for c in range(5):  # Computador, Nome, Editor, Versão, Tipo
                it = self.table.item(r, c)
                parts.append(it.text() if it else "")
            text = " ".join(parts).lower()
            ok = row_matches_app_results_filter(text, include, exclude)
            self.table.setRowHidden(r, not ok)
            if ok:
                visible += 1
        self.filter_count_lbl.setText(self.tr(f"{visible}/{total}") if total else "")
        self._sync_results_download_button()

    def _sync_results_download_button(self) -> None:
        """Download só habilitado quando há computador visível para exportar."""
        btn = getattr(self, "_results_download_btn", None)
        if btn is None or sip.isdeleted(btn):
            return
        btn.setEnabled(bool(self._collect_result_hosts()))

    def _update_summary(self, final: bool = False, interrupted: bool = False) -> None:
        query = getattr(self, "_active_query", "") or ""
        computers = {h.host.casefold() for h in self._hits}
        count_hosts = len(computers)
        count_apps = len(self._hits)

        if count_apps == 0:
            if final:
                if interrupted:
                    self.summary_lbl.setText(
                        self.tr(
                            f"Pesquisa interrompida — nenhum computador com "
                            f"aplicativo correspondente a “{query}” até o momento."
                        )
                    )
                else:
                    self.summary_lbl.setText(
                        self.tr(
                            f"Nenhum computador com aplicativo correspondente a “{query}”."
                        )
                    )
            else:
                self.summary_lbl.setText(self.tr("Pesquisando..."))
            return
        if interrupted:
            prefix = self.tr("Interrompida — ")
        elif final:
            prefix = ""
        else:
            prefix = self.tr("Em andamento — ")
        self.summary_lbl.setText(
            self.tr(
                f"{prefix}Aplicativo encontrado em {count_hosts} computador(es) "
                f"({count_apps} correspondência(s) para “{query}”)."
            )
        )

    def _append_hit_row(self, hit: SearchHit) -> None:
        app = hit.app
        name = app.display_name or app.display_line
        kind = "MSI" if (app.is_msi and app.product_code) else "EXE"
        try:
            build_uninstall_remote_cmd(app, "")
            can_uninstall = True
        except ValueError:
            can_uninstall = False

        row = self.table.rowCount()
        self.table.insertRow(row)

        host_item = QTableWidgetItem(hit.host)
        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, hit)
        pub_item = QTableWidgetItem(app.publisher or "")
        ver_item = QTableWidgetItem(app.version or "")
        kind_item = QTableWidgetItem(kind)
        kind_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        self.table.setItem(row, 0, host_item)
        self.table.setItem(row, 1, name_item)
        self.table.setItem(row, 2, pub_item)
        self.table.setItem(row, 3, ver_item)
        self.table.setItem(row, 4, kind_item)

        trash = QToolButton()
        trash.setText("\uE74D")
        trash.setFont(QFont("Segoe MDL2 Assets", 11))
        trash.setCursor(Qt.CursorShape.PointingHandCursor)
        trash.setAutoRaise(True)
        trash.setFixedSize(26, 26)
        trash.setStyleSheet(
            f"""
            QToolButton {{
                border: none;
                background: transparent;
                color: {COLOR_TEXT};
            }}
            QToolButton:hover {{
                background: {COLOR_HOVER};
                border-radius: {RADIUS_SMALL}px;
                color: #c42b1c;
            }}
            QToolButton:disabled {{ color: {COLOR_TEXT_MUTED}; }}
            """
        )
        if can_uninstall:
            extras_now = resolve_uninstall_extras(app, self._current_extras())
            trash.setToolTip(describe_uninstall(app, extras_now))
            trash._installed_app = app  # type: ignore[attr-defined]
            self._trash_buttons.append(trash)
            trash.clicked.connect(
                lambda _checked=False, h=hit: self._on_uninstall_clicked(h)
            )
        else:
            trash.setEnabled(False)
            trash.setToolTip(self.tr("Desinstalação indisponível (sem UninstallString)"))

        cell = QWidget()
        cell_lay = QHBoxLayout(cell)
        cell_lay.setContentsMargins(0, 0, 0, 0)
        cell_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cell_lay.addWidget(trash)
        self.table.setCellWidget(row, 5, cell)

    def _current_extras(self) -> str:
        if self.extras_edit is None or sip.isdeleted(self.extras_edit):
            return ""
        return (self.extras_edit.text() or "").strip()

    def _refresh_trash_tooltips(self) -> None:
        if not self._ui_alive():
            return
        extras_manual = self._current_extras()
        for btn in list(self._trash_buttons):
            if sip.isdeleted(btn):
                continue
            app_obj = getattr(btn, "_installed_app", None)
            if isinstance(app_obj, InstalledApp):
                btn.setToolTip(
                    describe_uninstall(
                        app_obj, resolve_uninstall_extras(app_obj, extras_manual)
                    )
                )

    def _collect_result_hosts(self) -> List[str]:
        """Hosts únicos dos resultados visíveis (respeita o filtro da tabela)."""
        hosts: List[str] = []
        seen: set[str] = set()
        for r in range(self.table.rowCount()):
            if self.table.isRowHidden(r):
                continue
            it = self.table.item(r, 0)
            h = (it.text() if it else "").strip().strip("\\")
            if not h:
                continue
            key = h.casefold()
            if key in seen:
                continue
            seen.add(key)
            hosts.append(h)
        return hosts

    def _export_results_hosts_json(self) -> None:
        """Gera hosts.json no formato {\"hosts\": [...]} a partir dos resultados."""
        if not self._ui_alive():
            return
        hosts = self._collect_result_hosts()
        if not hosts:
            QMessageBox.information(
                self,
                self.tr("Exportar hosts.json"),
                self.tr(
                    "Não há computadores nos resultados para exportar.\n"
                    "Execute uma pesquisa (e ajuste o filtro, se houver)."
                ),
            )
            return

        query = (getattr(self, "_active_query", "") or "").strip()
        safe_q = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in query)[:40]
        default_name = f"hosts_{safe_q}.json" if safe_q else "hosts.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Salvar hosts.json"),
            default_name,
            self.tr("JSON (*.json)"),
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"

        try:
            saved = save_hosts_file(path, hosts)
        except Exception as exc:
            QMessageBox.warning(
                self,
                self.tr("Exportar hosts.json"),
                self.tr(f"Não foi possível salvar o arquivo:\n{exc}"),
            )
            return

        self.log_output.append_log(
            self.tr(f"[PESQUISA] hosts.json exportado: {path} ({len(saved)} host(s))")
        )

    def _on_uninstall_clicked(self, hit: SearchHit) -> None:
        if not self._ui_alive():
            return
        manual = self._current_extras()
        extras = resolve_uninstall_extras(hit.app, manual)
        try:
            remote_cmd = build_uninstall_remote_cmd(hit.app, extras)
        except ValueError as exc:
            self.log_output.append_log(self.tr(f"[PESQUISA] {exc}"))
            return

        if extras and not manual:
            self.log_output.append_log(
                self.tr(
                    f"[PESQUISA] Parametros do catálogo para {hit.app.display_name}: {extras}"
                )
            )

        self.uninstallRequested.emit(hit.host, remote_cmd, hit.app.display_line)
