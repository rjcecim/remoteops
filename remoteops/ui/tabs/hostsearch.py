"""Aba Pesquisa de Host — varredura da faixa de IP, independente da Pesquisa de Aplicativos."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

from PyQt6 import sip
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
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
    RADIUS_SMALL,
    SIZE_UI_SMALL,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    bind_card_stack,
    grid_in_card,
    make_card_stack,
    make_field_label,
)
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.ui.widgets.table import (
    SortableTableItem,
    configure_standard_table,
    pause_table_sorting,
)
from remoteops.utils.hostsearch import (
    EMPTY_CELL,
    SESSION_LOOKUP_WORKERS,
    display_hostname,
    lookup_active_session_users,
    psexec_target,
    require_active_network_range_message,
    row_matches_filter,
    snapshot_creds,
)
from remoteops.utils.network_range import (
    get_network_range_config,
    ips_for_config,
    network_range_search_mode,
)
from remoteops.utils.network_scan import scan_windows_host_hits
from remoteops.utils.ping import normalize_host

COL_IP = 0
COL_HOSTNAME = 1
COL_USER = 2
COL_EYE = 3


class _HostScanWorker(QThread):
    """Varre TCP 445/135/139 + NetBIOS/DNS. Não consulta sessões WTS."""

    progress = pyqtSignal(int, int, int, str, int)  # gen, done, total, last_ip, found
    hostFound = pyqtSignal(int, str, str)  # generation, ip, hostname
    finished_ok = pyqtSignal(int, int)  # generation, found
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

            def on_hit(ip: str, hostname: str) -> None:
                self.hostFound.emit(gen, ip, hostname)

            hits = scan_windows_host_hits(
                self.ips,
                max_workers=self.max_workers,
                should_cancel=lambda: self._abort,
                on_progress=on_progress,
                on_hit=on_hit,
            )
            if self._abort:
                self.finished_aborted.emit(gen)
                return
            self.finished_ok.emit(gen, len(hits))
        except Exception as exc:
            self.finished_err.emit(gen, f"Erro na varredura de rede: {exc}")


class _UserLookupBridge(QObject):
    userReady = pyqtSignal(int, str, str)  # generation, ip, display


class HostSearchTab(QWidget):
    """Lista hosts Windows da faixa de IP (TCP 445/135/139). Sem hosts.json."""

    openHostRequested = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        *,
        creds_provider: Optional[Callable[[], Tuple[str, str]]] = None,
    ):
        super().__init__(parent)
        self._creds_provider = creds_provider
        self._scan_worker: Optional[_HostScanWorker] = None
        self._session_pool: Optional[ThreadPoolExecutor] = None
        self._user_bridge = _UserLookupBridge(self)
        self._user_bridge.userReady.connect(self._on_user_ready)
        self._hits: list[tuple[str, str]] = []
        self._users: dict[str, str] = {}
        self._scan_ips_done = 0
        self._scan_ips_total = 0
        self._scan_hosts_found = 0
        self._search_generation = 0
        self._accepting_results = False
        self._scan_started_count = 0

        root = make_card_stack(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        search_card = CardWidget("\uE968", self.tr("Pesquisa de Host"))
        self.search_card = search_card
        search_card.set_collapsible(True, collapsed=False)
        search_card.set_layout_stretch(0)
        self.search_btn = search_card.make_header_button(
            "\uE721", self.tr("Pesquisar hosts")
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
                "Exige faixa de IP ativa em Configurações. "
                "Não usa hosts.json. ICMP não exclui o host."
            )
        )
        add_row(grid, 0, self.tr("Status"), status_wrap)

        self.scan_progress = QProgressBar()
        self.scan_progress.setMinimum(0)
        self.scan_progress.setMaximum(1)
        self.scan_progress.setValue(0)
        self.scan_progress.setTextVisible(True)
        self.scan_progress.setFormat("%v / %m IPs")
        self.scan_progress.setFixedHeight(18)
        self.scan_progress.setToolTip(self.tr("IPs verificados na faixa configurada"))
        scan_wrap = QWidget()
        scan_lay = QHBoxLayout(scan_wrap)
        scan_lay.setContentsMargins(0, 0, 0, 0)
        scan_lay.setSpacing(8)
        scan_lay.addWidget(self.scan_progress, 1)
        self._scan_row_label = make_field_label(self.tr("Rede"))
        grid.addWidget(self._scan_row_label, 1, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(scan_wrap, 1, 1, Qt.AlignmentFlag.AlignVCenter)
        self._scan_row_wrap = scan_wrap

        self.phase_lbl = QLabel("")
        self.phase_lbl.setObjectName("searchPhase")
        self.phase_lbl.setWordWrap(True)
        self.phase_lbl.setStyleSheet(
            f"QLabel#searchPhase {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        grid.addWidget(self.phase_lbl, 2, 0, 1, 2)
        self._set_progress_rows_visible(False)

        root.addWidget(search_card, 0)

        self.results_card = CardWidget("\uE71D", self.tr("Resultados"))
        self.results_card.set_collapsible(True, collapsed=False)
        self.results_card.set_expanding(True)
        self.results_card.set_layout_stretch(2)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        self.results_card.content_layout.addWidget(self.summary_lbl, 0)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            self.tr("Filtrar por IP, hostname ou usuário…")
        )
        self.filter_edit.setToolTip(
            self.tr(
                "Filtra as linhas já encontradas, ao vivo. "
                "Não inicia nova varredura; a pesquisa continua na faixa inteira."
            )
        )
        self.filter_count_lbl = QLabel("")
        self.filter_count_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        filter_row.addWidget(self.filter_edit)
        filter_row.addWidget(self.filter_count_lbl)
        filter_wrap = QWidget()
        filter_wrap.setLayout(filter_row)
        self.results_card.content_layout.addWidget(filter_wrap, 0)
        self.filter_edit.textChanged.connect(self._apply_results_filter)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("IP"),
                self.tr("Hostname"),
                self.tr("Usuário ativo"),
                "",
            ]
        )
        configure_standard_table(
            self.table,
            stretch_columns=(2,),
            fixed_columns={3: 48},
            skip_sort_columns=(3,),
        )
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.table.setMinimumHeight(80)
        self.results_card.content_layout.addWidget(self.table, 1)

        root.addWidget(self.results_card, 2)
        bind_card_stack(root, (self.results_card,))

        self.destroyed.connect(self._abort_worker)
        self.refresh_hosts_status()

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

    def _set_progress_rows_visible(self, visible: bool) -> None:
        self._scan_row_label.setVisible(visible)
        self._scan_row_wrap.setVisible(visible)
        if not visible:
            self._set_phase_message("")
        self.search_card.updateGeometry()

    def _set_phase_message(self, text: str = "") -> None:
        msg = (text or "").strip()
        self.phase_lbl.setText(msg)
        self.phase_lbl.setVisible(bool(msg))
        self.search_card.updateGeometry()

    def _refresh_progress_ui(self) -> None:
        ip_total = max(1, int(self._scan_ips_total))
        self.scan_progress.setMaximum(ip_total)
        self.scan_progress.setValue(min(int(self._scan_ips_done), ip_total))
        self.scan_progress.setFormat("%v / %m IPs")

    def refresh_hosts_status(self) -> None:
        """Atualiza a legenda só com a faixa de IP (sem hosts.json)."""
        mode, err, ip_count = network_range_search_mode()
        blocked = require_active_network_range_message(mode, err)
        if blocked:
            state = "invalid" if mode == "invalid" else "err"
            self._set_hosts_status(state, blocked)
            return
        cfg = get_network_range_config()
        self._set_hosts_status(
            "ok",
            self.tr(f"Faixa {cfg.start_ip}–{cfg.end_ip} ({ip_count} IPs)"),
            self.tr(
                f"Varredura de rede: {cfg.start_ip}–{cfg.end_ip}. "
                f"{ip_count} IP(s), {cfg.scan_threads} threads. "
                "hosts.json não é usado. ICMP não exclui o host."
            ),
        )

    def _is_busy(self) -> bool:
        w = self._scan_worker
        return bool(w is not None and w.isRunning())

    def _set_search_busy(self, busy: bool) -> None:
        self.search_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

    def _disconnect_scan_signals(self, w: _HostScanWorker) -> None:
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

    def _shutdown_session_pool(self) -> None:
        pool = self._session_pool
        self._session_pool = None
        if pool is None:
            return
        pool.shutdown(wait=False, cancel_futures=True)

    def _abort_worker(self, _destroyed: object = None) -> None:
        self._accepting_results = False
        self._shutdown_session_pool()
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

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._accepting_results = False
        self._shutdown_session_pool()
        w = self._scan_worker
        if w is None:
            return
        self._scan_worker = None
        self._disconnect_scan_signals(w)
        w.abort()
        if w.isRunning():
            w.wait(max(0, int(wait_ms)))
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def stop_search(self) -> None:
        scan = self._scan_worker
        if scan is None or not scan.isRunning():
            return
        self._accepting_results = False
        self._shutdown_session_pool()
        scan.abort()
        self.stop_btn.setEnabled(False)
        self._set_phase_message(self.tr("Interrompendo..."))

    def start_search(self) -> None:
        if self._is_busy():
            return

        self.refresh_hosts_status()
        mode, err, _ip_count = network_range_search_mode()
        blocked = require_active_network_range_message(mode, err)
        if blocked:
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Host"),
                self.tr(blocked),
            )
            return

        cfg = get_network_range_config()
        ips, range_err = ips_for_config(cfg)
        if range_err or not ips:
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de Host"),
                self.tr(range_err or "Nenhum IP para varrer."),
            )
            return

        self._hits = []
        self._users = {}
        self._scan_ips_done = 0
        self._scan_ips_total = len(ips)
        self._scan_hosts_found = 0
        self._search_generation += 1
        generation = self._search_generation
        self._accepting_results = True
        self._scan_started_count += 1
        self._shutdown_session_pool()
        self._session_pool = ThreadPoolExecutor(max_workers=SESSION_LOOKUP_WORKERS)
        with pause_table_sorting(self.table):
            self.table.setRowCount(0)
        self._apply_results_filter()
        self.summary_lbl.setText(self.tr("Varrendo a rede..."))
        self._set_search_busy(True)
        self._set_progress_rows_visible(True)
        self._set_phase_message("")
        self._refresh_progress_ui()

        self._scan_worker = _HostScanWorker(
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

    def _is_current_generation(self, generation: int) -> bool:
        return (
            self._accepting_results
            and int(generation) == int(self._search_generation)
        )

    def _on_scan_progress(
        self, generation: int, done: int, total: int, _ip: str, found: int
    ) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._scan_ips_done = int(done)
        self._scan_ips_total = int(total)
        self._scan_hosts_found = max(int(found), self._scan_hosts_found)
        self._refresh_progress_ui()

    def _on_scan_host_found(self, generation: int, ip: str, hostname: str) -> None:
        if not self._ui_alive() or not self._is_current_generation(generation):
            return
        self.add_discovered_host(ip, hostname, queue_user_lookup=True)

    def add_discovered_host(
        self,
        ip: str,
        hostname: str,
        *,
        queue_user_lookup: bool = True,
    ) -> None:
        """Acrescenta uma linha imediatamente; sessão WTS é consultada depois, em paralelo."""
        addr = normalize_host(ip)
        name = normalize_host(hostname) or addr
        if not addr:
            return
        key = addr.casefold()
        if any(existing.casefold() == key for existing, _hn in self._hits):
            return
        self._hits.append((addr, name))
        self._users[key] = EMPTY_CELL
        self._scan_hosts_found = max(self._scan_hosts_found, len(self._hits))
        self._append_hit_row(addr, name)
        self._apply_results_filter()
        self._update_summary()
        if queue_user_lookup:
            self._queue_user_lookup(addr, name)

    def _queue_user_lookup(self, ip: str, hostname: str = "") -> None:
        pool = self._session_pool
        if pool is None:
            return
        generation = int(self._search_generation)
        try:
            pool.submit(self._lookup_user, generation, ip, hostname)
        except RuntimeError:
            return

    def _lookup_user(self, generation: int, ip: str, hostname: str = "") -> None:
        if int(generation) != int(self._search_generation):
            return
        user, password = snapshot_creds(self._creds_provider)
        try:
            text = lookup_active_session_users(
                ip, user, password, hostname=hostname
            )
        except Exception:
            text = EMPTY_CELL
        if int(generation) != int(self._search_generation):
            return
        self._user_bridge.userReady.emit(int(generation), ip, text)

    def _on_user_ready(self, generation: int, ip: str, display: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self.set_active_user(ip, display)

    def set_active_user(self, ip: str, display: str) -> None:
        addr = normalize_host(ip)
        if not addr:
            return
        text = (display or "").strip() or EMPTY_CELL
        self._users[addr.casefold()] = text
        row = self._row_for_ip(addr)
        if row is None:
            return
        with pause_table_sorting(self.table):
            item = self.table.item(row, COL_USER)
            if item is None:
                item = QTableWidgetItem(text)
                self.table.setItem(row, COL_USER, item)
            else:
                item.setText(text)
        self._apply_results_filter()

    def _row_for_ip(self, ip: str) -> Optional[int]:
        key = normalize_host(ip).casefold()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_IP)
            stored = ""
            if item is not None:
                stored = str(item.data(Qt.ItemDataRole.UserRole) or item.text() or "")
            if normalize_host(stored).casefold() == key:
                return row
        return None

    def _append_hit_row(self, ip: str, hostname: str) -> None:
        shown = display_hostname(ip, hostname)
        user = self._users.get(ip.casefold(), EMPTY_CELL)
        with pause_table_sorting(self.table):
            row = self.table.rowCount()
            self.table.insertRow(row)

            ip_item = SortableTableItem(ip)
            ip_item.setData(Qt.ItemDataRole.UserRole, ip)
            host_item = QTableWidgetItem(shown)
            user_item = QTableWidgetItem(user)

            self.table.setItem(row, COL_IP, ip_item)
            self.table.setItem(row, COL_HOSTNAME, host_item)
            self.table.setItem(row, COL_USER, user_item)

            eye = QToolButton()
            eye.setObjectName("hostSearchEye")
            eye.setText("\uE890")
            eye.setFont(QFont("Segoe MDL2 Assets", 11))
            eye.setCursor(Qt.CursorShape.PointingHandCursor)
            eye.setAutoRaise(True)
            eye.setFixedSize(26, 26)
            eye.setToolTip(self.tr("Preencher o Host remoto no PsExec"))
            eye.setStyleSheet(
                f"""
                QToolButton {{
                    border: none;
                    background: transparent;
                    color: {COLOR_TEXT};
                }}
                QToolButton:hover {{
                    background: {COLOR_HOVER};
                    border-radius: {RADIUS_SMALL}px;
                }}
                """
            )
            eye.clicked.connect(
                lambda _checked=False, a=ip, h=hostname: self._on_eye_clicked(a, h)
            )

            cell = QWidget()
            cell_lay = QHBoxLayout(cell)
            cell_lay.setContentsMargins(0, 0, 0, 0)
            cell_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_lay.addWidget(eye)
            self.table.setCellWidget(row, COL_EYE, cell)

    def _on_eye_clicked(self, ip: str, hostname: str) -> None:
        target = psexec_target(ip, hostname)
        if not target:
            return
        self.openHostRequested.emit(target)

    def _apply_results_filter(self) -> None:
        if not self._ui_alive():
            return
        query = self.filter_edit.text() or ""
        total = self.table.rowCount()
        visible = 0
        for row in range(total):
            ip_item = self.table.item(row, COL_IP)
            host_item = self.table.item(row, COL_HOSTNAME)
            user_item = self.table.item(row, COL_USER)
            ip = ip_item.text() if ip_item else ""
            hostname = host_item.text() if host_item else ""
            user = user_item.text() if user_item else ""
            raw_hostname = hostname if hostname != EMPTY_CELL else ip
            ok = row_matches_filter(ip, raw_hostname, user, query)
            self.table.setRowHidden(row, not ok)
            if ok:
                visible += 1
        self.filter_count_lbl.setText(self.tr(f"{visible}/{total}") if total else "")

    def _update_summary(self, final: bool = False, interrupted: bool = False) -> None:
        count = len(self._hits)
        if interrupted:
            self.summary_lbl.setText(
                self.tr(f"Varredura interrompida — {count} host(s) encontrado(s).")
            )
            return
        if final:
            if count == 0:
                self.summary_lbl.setText(
                    self.tr("Nenhum computador Windows encontrado na faixa.")
                )
            else:
                self.summary_lbl.setText(
                    self.tr(f"{count} host(s) Windows encontrado(s).")
                )
            return
        if count:
            self.summary_lbl.setText(self.tr(f"{count} host(s) encontrado(s)…"))
        else:
            self.summary_lbl.setText(self.tr("Varrendo a rede..."))

    def _finish_scan_ui(self, *, aborted: bool = False, error: str = "") -> None:
        self._accepting_results = False
        self._set_search_busy(False)
        self._scan_ips_done = max(self._scan_ips_done, self._scan_ips_total)
        self._refresh_progress_ui()
        if error:
            self._set_phase_message(error)
            self.summary_lbl.setText(error)
            return
        if aborted:
            self._set_phase_message(self.tr("Varredura interrompida"))
            self._update_summary(interrupted=True)
            return
        self._set_phase_message("")
        self._update_summary(final=True)

    def _on_scan_ok(self, generation: int, found: int) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._scan_hosts_found = max(int(found), self._scan_hosts_found)
        self._finish_scan_ui()

    def _on_scan_aborted(self, generation: int) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._finish_scan_ui(aborted=True)

    def _on_scan_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._search_generation):
            return
        self._finish_scan_ui(error=msg or self.tr("Erro na varredura de rede."))
