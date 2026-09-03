"""Aba Instalação em Lote — instala o EXE do FileSelector em vários hosts."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from typing import Dict, List, Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QWidget,
)

from remoteops.core.executor import _emit_conpty_text
from remoteops.services.batch_install import (
    REASON_CANCELLED,
    REASON_IN_PROGRESS,
    RESULT_ERROR,
    RESULT_INSTALLED,
    RESULT_SKIPPED,
    RESULT_UPDATED,
    RESULT_UPDATING,
    BatchHostRow,
    apply_install_outcome,
    build_batch_install_spec,
    decide_host_action,
    run_remote_installer,
    summarize_rows,
)
from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_TEXT,
    COLOR_TEXT_SECONDARY,
    SIZE_UI_SMALL,
)
from remoteops.ui.tabs.appsearch import _NetworkScanWorker
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    bind_card_stack,
    grid_in_card,
    make_card_stack,
    make_field_label,
)
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.ui.widgets.table import configure_standard_table, pause_table_sorting
from remoteops.utils.host_reachability import (
    decide_batch_connectivity,
    probe_host_reachability,
)
from remoteops.utils.hosts import load_hosts_file
from remoteops.utils.network_range import (
    get_network_range_config,
    ips_for_config,
    network_range_search_mode,
)
from remoteops.utils.product_identity import (
    identify_product,
    parse_version_key,
    read_exe_metadata,
)
from remoteops.utils.pstools import get_pstools_dir
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

_RESULT_COLORS = {
    RESULT_INSTALLED: "#107C10",
    RESULT_UPDATED: COLOR_ACCENT,
    RESULT_UPDATING: COLOR_ACCENT,
    RESULT_SKIPPED: COLOR_TEXT_SECONDARY,
    RESULT_ERROR: "#c42b1c",
}


class _BatchInstallWorker(QThread):
    """Detecta (paralelo) e instala (sequencial) sem bloquear a UI."""

    progress = pyqtSignal(int, int, int, int, str)  # gen, done, failed, total, host
    rowUpsert = pyqtSignal(int, object)
    logLine = pyqtSignal(str, str)  # category, message
    consoleStarted = pyqtSignal(str)
    consoleChunk = pyqtSignal(str)
    consoleActive = pyqtSignal(bool)
    finished_ok = pyqtSignal(int)
    finished_aborted = pyqtSignal(int)
    finished_err = pyqtSignal(int, str)

    def __init__(
        self,
        hosts: List[str],
        *,
        exe_path: str,
        desired_version: str,
        psexec_params: dict,
        user: str,
        password: str,
        identity,
        max_workers: int = 8,
        generation: int = 0,
        timeout: Optional[float] = None,
        inbox: Optional[Queue] = None,
        detect_only: bool = False,
        pending_rows: Optional[List[BatchHostRow]] = None,
        console_cols: int = 120,
        console_rows: int = 30,
    ):
        super().__init__()
        self.hosts = list(hosts)
        self.exe_path = exe_path
        self.desired_version = (desired_version or "").strip()
        self.psexec_params = dict(psexec_params or {})
        self._user = user or ""
        self._password = password or ""
        self.identity = identity
        try:
            n = int(max_workers)
        except (TypeError, ValueError):
            n = 8
        self.max_workers = max(MIN_SEARCH_MAX_WORKERS, min(MAX_SEARCH_MAX_WORKERS, n))
        self.generation = int(generation)
        self.timeout = float(timeout) if timeout else get_remote_registry_timeout()
        self._inbox = inbox
        self.detect_only = bool(detect_only)
        self.pending_rows = list(pending_rows or [])
        self._offered = len(self.pending_rows) if self.pending_rows else len(self.hosts)
        self._intake_closed = inbox is None
        self._abort = False
        self._install_proc_cancel = False
        self._done = 0
        self._failed = 0
        self._next_order = 0
        self._state_lock = threading.Lock()
        self._console_cols = max(20, int(console_cols or 120))
        self._console_rows = max(5, int(console_rows or 30))

    def offer_host(self, host: str) -> None:
        if self._inbox is None or self._intake_closed or self._abort:
            return
        h = (host or "").strip().strip("\\")
        if not h:
            return
        with self._state_lock:
            self._offered += 1
        self._inbox.put(h)

    def close_intake(self) -> None:
        if self._inbox is None or self._intake_closed:
            return
        self._intake_closed = True
        self._inbox.put(None)

    def abort(self) -> None:
        self._abort = True
        self._install_proc_cancel = True
        self.close_intake()

    def set_console_size(self, cols: int, rows: int) -> None:
        with self._state_lock:
            self._console_cols = max(20, int(cols))
            self._console_rows = max(5, int(rows))

    def run(self) -> None:
        gen = self.generation
        ping_thread: Optional[threading.Thread] = None
        try:
            if self.pending_rows:
                self._run_installs(self.pending_rows)
                if self._abort:
                    self.finished_aborted.emit(gen)
                    return
                self._emit_progress("")
                self.finished_ok.emit(gen)
                return

            streaming = self._inbox is not None
            if not streaming and not self.hosts:
                self.finished_err.emit(gen, "Nenhum host para consultar.")
                return

            ping_q: Queue = Queue()
            ping_thread = self._start_ping_filter(ping_q)

            seed_total = max(1, len(self.hosts))
            workers = (
                self.max_workers if streaming else min(self.max_workers, seed_total)
            )
            saw_cancel = False
            saw_any = False
            pending_installs: List[BatchHostRow] = []

            for status in run_remote_inventory_batch(
                [],
                max_workers=workers,
                timeout=self.timeout,
                should_cancel=lambda: self._abort,
                extra_hosts=ping_q,
            ):
                saw_any = True
                if self._abort and status.error_kind == "cancelled":
                    saw_cancel = True
                    self._bump_progress(status.host, failed=False)
                    continue

                row = decide_host_action(
                    host=status.host,
                    desired_version=self.desired_version,
                    online=True,
                    inventory=status,
                    identity=self.identity,
                )
                if not status.ok:
                    self._log_detect_error(status)
                self._upsert_row(row)
                self._bump_progress(
                    status.host, failed=row.result == RESULT_ERROR
                )
                if row.needs_install:
                    pending_installs.append(row)

            if self._abort or saw_cancel:
                self.finished_aborted.emit(gen)
                return
            if not streaming and not saw_any and self._done == 0:
                self.finished_err.emit(gen, "Nenhum host para consultar.")
                return
            if not self.detect_only:
                self._run_installs(pending_installs)
                if self._abort:
                    self.finished_aborted.emit(gen)
                    return
            self._emit_progress("")
            self.finished_ok.emit(gen)
        except Exception as exc:
            self.finished_err.emit(gen, f"Erro na instalação em lote: {exc}")
        finally:
            self._password = ""
            if ping_thread is not None and ping_thread.is_alive():
                ping_thread.join(timeout=2.0)

    def _emit_log(self, category: str, message: str) -> None:
        cat = (category or "INFO").strip().upper()
        self.logLine.emit(cat, message or "")

    def _run_installs(self, pending_installs: List[BatchHostRow]) -> None:
        pending_installs.sort(key=lambda item: (item.order, item.host.casefold()))
        if pending_installs and not self._abort:
            self._emit_log(
                "INFO",
                f"Instalando em {len(pending_installs)} computador(es) "
                "na ordem da tabela...",
            )
        if self.pending_rows:
            with self._state_lock:
                self._offered = len(pending_installs)
                self._done = 0
                self._failed = 0
            self._emit_progress("")
        for row in pending_installs:
            if self._abort:
                row.result = RESULT_ERROR
                row.reason = REASON_CANCELLED
                row.needs_install = False
                self._upsert_row(row)
                self._mark_failed()
                continue
            row.result = RESULT_UPDATING
            row.reason = REASON_IN_PROGRESS
            self._upsert_row(row)
            self._emit_log(
                "INFO",
                f"{row.host}: "
                f"{'Atualizando' if row.is_update else 'Instalando'}...",
            )
            self._install_one(row)
            self._upsert_row(row)
            if self.pending_rows:
                self._bump_progress(row.host, failed=row.result == RESULT_ERROR)
            elif row.result == RESULT_ERROR:
                self._mark_failed()

    def _bump_progress(self, host: str, *, failed: bool) -> None:
        with self._state_lock:
            self._done += 1
            if failed:
                self._failed += 1
        self._emit_progress(host)

    def _mark_failed(self) -> None:
        with self._state_lock:
            self._failed += 1
        self._emit_progress("")

    def _emit_progress(self, host: str) -> None:
        with self._state_lock:
            done = self._done
            failed = self._failed
            total = max(done, int(self._offered))
        self.progress.emit(self.generation, done, failed, total, host)

    def _upsert_row(self, row: BatchHostRow) -> None:
        with self._state_lock:
            if int(row.order) <= 0:
                self._next_order += 1
                row.order = self._next_order
        self.rowUpsert.emit(self.generation, row)

    def _start_ping_filter(self, ping_q: Queue) -> threading.Thread:
        """ICMP + TCP 445 antes do inventário; ICMP bloqueado não exclui o host."""

        def _feed() -> None:
            remaining = list(self.hosts)
            intake = self._inbox
            workers = min(4, max(1, len(remaining) or 1))

            def _handle(raw: str) -> None:
                host = (raw or "").strip().strip("\\")
                if not host or self._abort:
                    return
                reach = probe_host_reachability(
                    host,
                    should_cancel=lambda: self._abort,
                    log=False,
                )
                decision = decide_batch_connectivity(reach)
                if not decision.continue_inventory:
                    row = decide_host_action(
                        host=host,
                        desired_version=self.desired_version,
                        online=False,
                        inventory=None,
                        identity=self.identity,
                    )
                    self._upsert_row(row)
                    self._bump_progress(host, failed=False)
                    if decision.log_message:
                        self._emit_log("FALHA", decision.log_message)
                    return
                if decision.continue_inventory and decision.log_message:
                    self._emit_log("AVISO", decision.log_message)
                ping_q.put(host)

            if remaining:
                executor = ThreadPoolExecutor(max_workers=workers)
                try:
                    futures = [executor.submit(_handle, host) for host in remaining]
                    for fut in as_completed(futures):
                        if self._abort:
                            break
                        try:
                            fut.result()
                        except Exception:
                            pass
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
            if intake is None:
                ping_q.put(None)
                return
            while True:
                item = intake.get()
                if item is None or self._abort:
                    ping_q.put(None)
                    return
                _handle(str(item))

        thread = threading.Thread(target=_feed, name="lote-ping", daemon=True)
        thread.start()
        return thread

    def _install_one(self, row: BatchHostRow) -> None:
        pstools = get_pstools_dir()
        spec = build_batch_install_spec(
            host=row.host,
            exe_path=self.exe_path,
            psexec_params=self.psexec_params,
            pstools_path=pstools,
            has_password=bool(self._password),
        )
        display = (spec.display_command or "").strip()
        if display and not display.startswith("#"):
            self.consoleStarted.emit(display)
        with self._state_lock:
            cols = self._console_cols
            rows = self._console_rows
        self.consoleActive.emit(True)
        try:
            outcome = run_remote_installer(
                spec,
                password=self._password,
                should_cancel=lambda: self._install_proc_cancel,
                on_output=lambda chunk: self.consoleChunk.emit(chunk),
                cols=cols,
                rows=rows,
            )
        finally:
            self.consoleActive.emit(False)
        apply_install_outcome(row, outcome)
        if outcome.ok:
            self._emit_log(
                "OK",
                f"{row.host}: {row.result} (código {outcome.return_code})",
            )
        else:
            detail = outcome.message or row.reason
            self._emit_log(
                "FALHA",
                f"{row.host}: {row.result} — {detail}",
            )

    def _log_detect_error(self, status) -> None:
        kind = status.error_kind or "internal_error"
        labels = {
            "auth": "falha de autenticação",
            "remote_registry": "Remote Registry/RPC indisponível",
            "unreachable": "host inacessível",
            "invalid_host": "host inválido",
            "timed_out": "consulta expirada (timeout)",
            "cancelled": "consulta cancelada",
            "internal_error": "erro interno na consulta",
        }
        label = labels.get(kind, kind)
        self._emit_log("FALHA", f"{status.host}: falha na detecção ({label})")


class BatchInstallTab(QWidget):
    """Instala o EXE selecionado no main em massa (faixa de IP ou hosts.json)."""

    def __init__(
        self,
        parent=None,
        *,
        exe_provider=None,
        creds_provider=None,
        psexec_params_provider=None,
    ):
        super().__init__(parent)
        self._exe_provider = exe_provider or (lambda: "")
        self._creds_provider = creds_provider or (lambda: ("", ""))
        self._psexec_params_provider = psexec_params_provider or (lambda: {})
        self._worker: Optional[_BatchInstallWorker] = None
        self._scan_worker: Optional[_NetworkScanWorker] = None
        self._hosts_path = ""
        self._rows: Dict[str, BatchHostRow] = {}
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = 0
        self._from_network = False
        self._scan_ips_done = 0
        self._scan_ips_total = 0
        self._scan_hosts_found = 0
        self._accepting = False
        self._generation = 0
        self._identity_label = ""
        self._scan_ready = False
        self._busy_kind = ""
        self._hosts_source_sig = None
        self._console_size = (120, 30)
        self._console_carry: List[str] = []

        root = make_card_stack(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        install_card = CardWidget("\uE118", self.tr("Instalação em Lote"))
        self.install_card = install_card
        install_card.set_collapsible(True, collapsed=False)
        # Formulário compacto: teto = altura em que abre (sizeHint), sem absorver sobra.
        install_card.set_layout_stretch(0)
        self.scan_btn = install_card.make_header_button(
            "\uE721", self.tr("Varrer hosts (faixa de IP ou hosts.json)")
        )
        self.scan_btn.clicked.connect(self.start_scan)
        install_card.add_header_button(self.scan_btn)
        self.start_btn = install_card.make_header_button(
            "\uE768",
            self.tr("Instalar nos hosts varridos. Varra a lista antes."),
        )
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_install)
        install_card.add_header_button(self.start_btn)
        self.stop_btn = install_card.make_header_button(
            "\uE71A", self.tr("Parar varredura ou instalação em lote")
        )
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_install)
        install_card.add_header_button(self.stop_btn)
        grid = grid_in_card(install_card)

        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText(
            self.tr("Opcional — vazio usa a versão do instalador")
        )
        self.version_edit.setToolTip(
            self.tr(
                "Comparação numérica (4.10.0 > 4.9.0). Nunca faz downgrade.\n"
                "Vazio: usa a versão do EXE. Hosts já nessa versão não são instalados."
            )
        )
        self.version_edit.textChanged.connect(lambda _t: self._invalidate_scan())
        add_row(grid, 0, self.tr("Versão Desejada"), self.version_edit)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.hosts_status_dot = _StatusDot()
        self.hosts_status_lbl = QLabel("")
        self.hosts_status_lbl.setObjectName("loteHostsStatus")
        self.hosts_status_lbl.setStyleSheet(
            f"QLabel#loteHostsStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
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

        def _bar(fmt: str) -> QProgressBar:
            bar = QProgressBar()
            bar.setMinimum(0)
            bar.setMaximum(1)
            bar.setValue(0)
            bar.setTextVisible(True)
            bar.setFormat(fmt)
            bar.setFixedHeight(18)
            return bar

        self.scan_progress = _bar("%v / %m IPs")
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

        self.progress = _bar("%v / %m")
        self.progress.setToolTip(self.tr("Hosts processados"))
        self.ok_count_lbl = QLabel(self.tr("Sucesso: 0"))
        self.fail_count_lbl = QLabel(self.tr("Falharam: 0"))
        self.ok_count_lbl.setStyleSheet("color: palette(highlight); font-weight: 600;")
        self.fail_count_lbl.setStyleSheet("color: #c42b1c; font-weight: 600;")
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
        self.phase_lbl.setObjectName("lotePhase")
        self.phase_lbl.setWordWrap(True)
        self.phase_lbl.setStyleSheet(
            f"QLabel#lotePhase {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        grid.addWidget(self.phase_lbl, 4, 0, 1, 2)
        self._set_progress_rows_visible(False, network=False)

        self.product_lbl = QLabel("")
        self.product_lbl.setObjectName("loteProduct")
        self.product_lbl.setWordWrap(True)
        self.product_lbl.setStyleSheet(
            f"QLabel#loteProduct {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        grid.addWidget(self.product_lbl, 6, 0, 1, 2)
        self.product_lbl.setVisible(False)

        root.addWidget(install_card, 0)

        self.results_card = CardWidget("\uE71D", self.tr("Resultados"))
        self.results_card.set_collapsible(True, collapsed=False)
        self.results_card.set_expanding(True)
        self.results_card.set_layout_stretch(2)

        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        self.results_card.content_layout.addWidget(self.summary_lbl, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Computador/IP"),
                self.tr("Aplicativo"),
                self.tr("Versão"),
                self.tr("Versão desejada"),
                self.tr("Ação"),
                self.tr("Resultado"),
                self.tr("Motivo"),
            ]
        )
        configure_standard_table(self.table, stretch_columns=(1, 6))
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.table.setMinimumHeight(80)
        self.results_card.content_layout.addWidget(self.table, 1)
        root.addWidget(self.results_card, 2)

        output_card = CardWidget("\uE9F9", self.tr("Saída"))
        self.output_card = output_card
        output_card.set_collapsible(True, collapsed=False)
        output_card.set_expanding(True)
        output_card.set_layout_stretch(1)

        self._output_copy_btn = output_card.make_header_button(
            "\uE8C8",
            self.tr("Copiar seleção (ou tudo, se nada estiver selecionado)"),
        )
        self._output_copy_btn.clicked.connect(self._copy_current_output)
        output_card.add_header_button(self._output_copy_btn)
        self._output_clear_btn = output_card.make_header_button(
            "\uE74D", self.tr("Limpar este console (apenas na tela)")
        )
        self._output_clear_btn.clicked.connect(self._clear_current_output)
        output_card.add_header_button(self._output_clear_btn)

        self._output_tabs = QTabWidget()
        inner_bar = Mdl2TabBar(self._output_tabs)
        inner_bar.setExpanding(False)
        self._output_tabs.setTabBar(inner_bar)
        self._output_tabs.setDocumentMode(True)
        self._output_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.log_messages = LogOutputWidget(
            self, title=self.tr("Mensagens"), icon="\uE8BD"
        )
        self.log_messages.set_interactive(False)
        self.log_messages.set_session_status("idle")
        self._embed_inner_card(self.log_messages)

        self.log_console = LogOutputWidget(
            self, title=self.tr("Console de Saída"), icon="\uE9F9"
        )
        self.log_console.set_interactive(False)
        self.log_console.set_session_status("idle")
        self.log_console.interruptRequested.connect(self.stop_install)
        self.log_console.sessionExitRequested.connect(self.stop_install)
        self.log_console.consoleResized.connect(self._on_console_resized)
        self._embed_inner_card(self.log_console)

        idx_msg = self._output_tabs.addTab(
            self.log_messages, self.tr("Mensagens")
        )
        inner_bar.set_tab_meta(idx_msg, "\uE8BD")
        idx_console = self._output_tabs.addTab(
            self.log_console, self.tr("Console de Saída")
        )
        inner_bar.set_tab_meta(idx_console, "\uE9F9")
        self._output_tabs.setCurrentIndex(0)
        output_card.content_layout.addWidget(self._output_tabs, 1)
        root.addWidget(output_card, 1)
        # Formulário (install_card) fica fora do bind: stretch 0 / altura = conteúdo.
        bind_card_stack(root, (self.results_card, output_card))
        self.destroyed.connect(self._abort_worker)
        self.refresh_hosts_status()
        self.refresh_product_hint()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_hosts_status()
        self.refresh_product_hint()

    def _ui_alive(self) -> bool:
        return not sip.isdeleted(self)

    def _log(self, category: str, message: str) -> None:
        """Console da aba com categoria: INFO, OK, AVISO, FALHA, ERRO."""
        if not self._ui_alive():
            return
        cat = (category or "INFO").strip().upper()
        self.log_messages.append_log(f"[{cat}] {message or ''}")

    @staticmethod
    def _embed_inner_card(card: CardWidget) -> None:
        """Remove borda/cabeçalho do card embutido numa aba interna."""
        card.set_collapsible(False)
        card._header_widget.hide()
        card._divider.hide()
        card._set_divider_spacing_visible(False)
        card._container.setStyleSheet(
            "QWidget#cardContainer { background: transparent; border: none; }"
        )
        card._container_layout.setContentsMargins(0, 0, 0, 0)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        card._container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def _current_output_log(self) -> LogOutputWidget:
        if int(self._output_tabs.currentIndex()) == 1:
            return self.log_console
        return self.log_messages

    def _copy_current_output(self) -> None:
        self._current_output_log().copyRequested.emit()

    def _clear_current_output(self) -> None:
        self._current_output_log().clear_log()

    def _show_output_tab(self, index: int) -> None:
        if hasattr(self, "_output_tabs"):
            self._output_tabs.setCurrentIndex(int(index))

    def _on_console_resized(self, cols: int, rows: int) -> None:
        self._console_size = (max(20, int(cols)), max(5, int(rows)))
        w = self._worker
        if w is not None:
            w.set_console_size(*self._console_size)

    def _reset_console_idle(self, *, clear: bool = False) -> None:
        self._console_carry = []
        self.log_console.set_interactive(False)
        self.log_console.set_partial_line("")
        self.log_console.set_session_status("idle")
        if clear:
            self.log_console.clear_log()

    def _finish_console_session(self, *, error: bool = False) -> None:
        self._flush_console_partial()
        self.log_console.set_interactive(False)
        self.log_console.set_session_status("error" if error else "exited")

    def _flush_console_partial(self) -> None:
        leftover = (self._console_carry[0] if self._console_carry else "").strip()
        self._console_carry = []
        if leftover:
            self.log_console.append_log(leftover)
        self.log_console.set_partial_line("")

    def _on_console_started(self, command: str) -> None:
        if not self._ui_alive():
            return
        self._flush_console_partial()
        text = (command or "").strip()
        if text:
            self.log_console.append_log(text)

    def _on_console_chunk(self, chunk: str) -> None:
        if not self._ui_alive():
            return
        _emit_conpty_text(
            chunk,
            prefix="",
            on_line=self.log_console.append_log,
            carry=self._console_carry,
            on_partial=self.log_console.set_partial_line,
        )

    def _on_console_active(self, active: bool) -> None:
        if not self._ui_alive():
            return
        if active:
            self.log_console.set_interactive(True)
            self.log_console.set_session_status("running")
            return
        self._flush_console_partial()
        self.log_console.set_interactive(False)
        self.log_console.append_log("")

    def _current_exe(self) -> str:
        try:
            path = self._exe_provider() or ""
        except Exception:
            path = ""
        return str(path).strip()

    def refresh_product_hint(self) -> None:
        if not self._ui_alive():
            return
        exe = self._current_exe()
        if not exe or not os.path.isfile(exe):
            self.product_lbl.setText("")
            self.product_lbl.setVisible(False)
            self.version_edit.setPlaceholderText(
                self.tr("Opcional — vazio usa a versão do instalador")
            )
            self.install_card.updateGeometry()
            return
        meta = read_exe_metadata(exe)
        identity = identify_product(exe, meta)
        self._identity_label = identity.label
        ver = identity.installer_version
        if ver:
            self.version_edit.setPlaceholderText(
                self.tr(
                    f"Opcional — versão do instalador: {ver} "
                    "(hosts nessa versão são ignorados)"
                )
            )
        bits = [self.tr(f"Produto: {identity.label}")]
        if meta.product_name and meta.product_name.casefold() != identity.label.casefold():
            bits.append(self.tr(f"ProductName: {meta.product_name}"))
        self.product_lbl.setText("  ·  ".join(bits))
        self.product_lbl.setVisible(True)
        self.install_card.updateGeometry()

    def on_exe_changed(self, _path: str = "") -> None:
        if self._is_busy():
            self.stop_install()
        self._invalidate_scan()
        self.refresh_product_hint()

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
        self.install_card.updateGeometry()

    def _set_phase_message(self, text: str = "") -> None:
        msg = (text or "").strip()
        self.phase_lbl.setText(msg)
        self.phase_lbl.setVisible(bool(msg))
        self.install_card.updateGeometry()

    def _refresh_progress_ui(self) -> None:
        if self._from_network:
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
        previous = self._hosts_source_sig
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
            sig = ("network", cfg.start_ip, cfg.end_ip, int(ip_count or 0))
        elif mode == "invalid":
            self._hosts_path = ""
            self._set_hosts_status("invalid", err or self.tr("Faixa de IP inválida"))
            sig = ("invalid", err or "")
        else:
            path, origin = resolve_configured_hosts_path()
            if origin == "missing" or not path or not os.path.isfile(path):
                self._hosts_path = ""
                self._set_hosts_status("err", self.tr("Não encontrado"))
                sig = ("missing", "")
            else:
                p = os.path.normpath(path)
                if len(p) >= 2 and p[1] == ":":
                    p = p[0].upper() + p[1:]
                try:
                    hosts = load_hosts_file(p)
                except Exception:
                    self._hosts_path = ""
                    self._set_hosts_status("invalid", self.tr("Arquivo inválido"))
                    hosts = None
                    sig = ("invalid", p)
                else:
                    self._hosts_path = p
                    if not hosts:
                        self._set_hosts_status("warn", self.tr("Encontrado — lista vazia"))
                    else:
                        self._set_hosts_status(
                            "ok", self.tr(f"Encontrado — {len(hosts)} host(s)")
                        )
                    sig = ("json", p, len(hosts))
        self._hosts_source_sig = sig
        if previous is not None and previous != sig:
            self._invalidate_scan()

    def _is_busy(self) -> bool:
        w = self._worker
        s = self._scan_worker
        return bool(
            (w is not None and w.isRunning()) or (s is not None and s.isRunning())
        )

    def _invalidate_scan(self) -> None:
        self._scan_ready = False
        if not self._is_busy():
            self.start_btn.setEnabled(False)

    def _set_busy(self, busy: bool) -> None:
        if not busy:
            self._busy_kind = ""
        self.scan_btn.setEnabled(not busy)
        self.start_btn.setEnabled(not busy and self._scan_ready)
        self.stop_btn.setEnabled(busy)
        self.version_edit.setEnabled(not busy)

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

    def _disconnect_worker_signals(self, w: _BatchInstallWorker) -> None:
        for signal, slot in (
            (w.progress, self._on_progress),
            (w.rowUpsert, self._on_row_upsert),
            (w.logLine, self._on_log_line),
            (w.consoleStarted, self._on_console_started),
            (w.consoleChunk, self._on_console_chunk),
            (w.consoleActive, self._on_console_active),
            (w.finished_ok, self._on_ok),
            (w.finished_aborted, self._on_aborted),
            (w.finished_err, self._on_err),
            (w.finished, self._on_worker_finished),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass

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

    def _abort_worker(self, _destroyed: object = None) -> None:
        self._abort_scan_worker()
        w = self._worker
        if w is None:
            return
        self._worker = None
        self._accepting = False
        self._disconnect_worker_signals(w)
        w.abort()
        if w.isRunning():
            w.wait(max(3000, int(get_remote_registry_timeout() * 1000) // 3))
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._accepting = False
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

    def stop_install(self) -> None:
        scan = self._scan_worker
        worker = self._worker
        scanning = scan is not None and scan.isRunning()
        running = worker is not None and worker.isRunning()
        if not scanning and not running:
            return
        self._accepting = False
        if scanning:
            scan.abort()
        if running:
            worker.abort()
        elif scanning and worker is not None:
            worker.abort()
        self.stop_btn.setEnabled(False)
        if self._busy_kind == "install":
            self._set_phase_message(self.tr("Interrompendo..."))
            self._log(
                "AVISO",
                self.tr(
                    "Interrupção solicitada. Novas instalações não serão iniciadas. "
                    "A instalação já enviada via PsExec pode continuar no computador remoto."
                ),
            )
        else:
            self._set_phase_message(self.tr("Interrompendo..."))
            self._log(
                "AVISO",
                self.tr("Interrupção solicitada. A varredura será encerrada."),
            )

    def _check_exe_and_version(self) -> Optional[tuple]:
        exe = self._current_exe()
        if not exe or not os.path.isfile(exe) or not exe.lower().endswith(".exe"):
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
                self.tr("Selecione um arquivo .exe no seletor principal."),
            )
            return None
        desired = (self.version_edit.text() or "").strip()
        if desired and parse_version_key(desired) is None:
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
                self.tr(
                    "Versão Desejada inválida. Use um valor numérico "
                    "(ex.: 7.23 ou 4.10.0) ou deixe o campo vazio."
                ),
            )
            return None
        return exe, desired

    def start_scan(self) -> None:
        if self._is_busy():
            return
        checked = self._check_exe_and_version()
        if checked is None:
            return
        exe, desired = checked

        self.refresh_hosts_status()
        mode, err, _ip_count = network_range_search_mode()
        if mode == "invalid":
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
                self.tr(
                    err
                    or "Faixa de IP inválida. Corrija em Configurações ou limpe os campos."
                ),
            )
            return
        if mode == "network":
            self._start_network_scan(exe, desired)
            return
        if not self._hosts_path or not os.path.isfile(self._hosts_path):
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
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
                self.tr("Instalação em Lote"),
                self.tr(f"Não foi possível ler o arquivo de hosts:\n{exc}"),
            )
            return
        if not hosts:
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
                self.tr("A lista de hosts está vazia."),
            )
            return
        self._begin_scan(hosts, exe, desired, from_network=False)

    def start_install(self) -> None:
        if self._is_busy():
            return
        self.refresh_hosts_status()
        if not self._scan_ready:
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
                self.tr("Varra os hosts antes de instalar."),
            )
            return
        checked = self._check_exe_and_version()
        if checked is None:
            return
        exe, desired = checked
        pending = [row for row in self._rows.values() if row.needs_install]
        if not pending:
            QMessageBox.information(
                self,
                self.tr("Instalação em Lote"),
                self.tr(
                    "Nenhum host da varredura precisa de instalação ou atualização."
                ),
            )
            return
        self._begin_pending_install(pending, exe, desired)

    def _reset_results(self) -> None:
        self._rows = {}
        self.table.setRowCount(0)
        self.summary_lbl.setText(self.tr("Em andamento..."))

    def reset_to_startup(self) -> None:
        """Volta a aba Lote ao estado em que o EXE ainda não foi escolhido."""
        if self._is_busy():
            self.stop_install()
        self.version_edit.clear()
        self._scan_ready = False
        self._busy_kind = ""
        self._reset_results()
        self.summary_lbl.setText("")
        self._set_progress_rows_visible(False)
        self._set_busy(False)
        self.log_messages.clear_log()
        self.log_messages.set_session_status("idle")
        self._reset_console_idle(clear=True)
        self._show_output_tab(0)
        self.refresh_product_hint()

    def _start_network_scan(self, exe: str, desired: str) -> None:
        cfg = get_network_range_config()
        ips, err = ips_for_config(cfg)
        if err or not ips:
            QMessageBox.warning(
                self,
                self.tr("Instalação em Lote"),
                self.tr(err or "Nenhum IP para varrer."),
            )
            return
        self._from_network = True
        self._scan_ready = False
        self._busy_kind = "scan"
        self._scan_ips_done = 0
        self._scan_ips_total = len(ips)
        self._scan_hosts_found = 0
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = 0
        self._generation += 1
        generation = self._generation
        self._accepting = True
        self._reset_results()
        self._set_busy(True)
        self._set_progress_rows_visible(True, network=True)
        self._set_phase_message(self.tr("Varrendo hosts..."))
        self._refresh_progress_ui()
        self._reset_console_idle(clear=True)
        self._show_output_tab(0)
        self._begin_streaming_scan(exe, desired, generation)
        self._scan_worker = _NetworkScanWorker(
            ips, cfg.scan_threads, generation=generation
        )
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.hostFound.connect(self._on_scan_host_found)
        self._scan_worker.finished_ok.connect(self._on_scan_ok)
        self._scan_worker.finished_aborted.connect(self._on_scan_aborted)
        self._scan_worker.finished_err.connect(self._on_scan_err)
        self._scan_worker.start()
        self._log(
            "INFO",
            self.tr(
                f"Varrendo {cfg.start_ip}–{cfg.end_ip} "
                f"({len(ips)} IP(s), {cfg.scan_threads} threads)..."
            ),
        )

    def _creds_and_params(self):
        try:
            user, password = self._creds_provider()
        except Exception:
            user, password = "", ""
        try:
            params = dict(self._psexec_params_provider() or {})
        except Exception:
            params = {}
        return str(user or ""), str(password or ""), params

    def _log_identity(self, identity, desired: str) -> None:
        self._log(
            "INFO",
            self.tr(
                f"Produto: {identity.label}. "
                f"Needles: {', '.join(identity.needles[:6]) or '—'}"
            ),
        )
        if desired:
            self._log("INFO", self.tr(f"Versão desejada: {desired}"))
        else:
            installer_ver = (getattr(identity, "installer_version", None) or "").strip()
            if installer_ver:
                self._log(
                    "INFO",
                    self.tr(
                        f"Versão desejada vazia: usando a versão do "
                        f"instalador ({installer_ver}). Hosts já nessa versão "
                        "serão ignorados."
                    ),
                )
            else:
                self._log(
                    "AVISO",
                    self.tr(
                        "Versão desejada vazia e instalador sem versão: "
                        "só instala se o aplicativo não estiver presente."
                    ),
                )

    def _begin_streaming_scan(
        self, exe: str, desired: str, generation: int
    ) -> None:
        identity = identify_product(exe)
        user, password, params = self._creds_and_params()
        inbox: Queue = Queue()
        self._worker = _BatchInstallWorker(
            [],
            exe_path=exe,
            desired_version=desired,
            psexec_params=params,
            user=user,
            password=password,
            identity=identity,
            max_workers=get_search_max_workers(),
            generation=generation,
            timeout=get_remote_registry_timeout(),
            inbox=inbox,
            detect_only=True,
        )
        self._connect_worker(self._worker)
        self._worker.start()
        self._log_identity(identity, desired)

    def _begin_scan(
        self,
        hosts: List[str],
        exe: str,
        desired: str,
        *,
        from_network: bool,
    ) -> None:
        self._from_network = from_network
        self._scan_ready = False
        self._busy_kind = "scan"
        self._reset_results()
        self._generation += 1
        generation = self._generation
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = len(hosts)
        self._accepting = True
        self.summary_lbl.setText(self.tr("Em andamento..."))
        self._set_busy(True)
        self._set_progress_rows_visible(True, network=from_network)
        self._set_phase_message(self.tr("Varrendo hosts..."))
        self._refresh_progress_ui()
        self._reset_console_idle(clear=True)
        self._show_output_tab(0)

        identity = identify_product(exe)
        user, password, params = self._creds_and_params()
        workers = min(get_search_max_workers(), max(1, len(hosts)))
        self._worker = _BatchInstallWorker(
            hosts,
            exe_path=exe,
            desired_version=desired,
            psexec_params=params,
            user=user,
            password=password,
            identity=identity,
            max_workers=workers,
            generation=generation,
            timeout=get_remote_registry_timeout(),
            detect_only=True,
        )
        self._connect_worker(self._worker)
        self._worker.start()
        self._log(
            "INFO",
            self.tr(
                f"Consultando {len(hosts)} host(s) "
                f"({workers} consultas simultâneas)."
            ),
        )
        self._log_identity(identity, desired)

    def _begin_pending_install(
        self,
        pending: List[BatchHostRow],
        exe: str,
        desired: str,
    ) -> None:
        self._from_network = False
        self._busy_kind = "install"
        self._generation += 1
        generation = self._generation
        self._hosts_failed = 0
        self._hosts_done = 0
        self._hosts_total = len(pending)
        self._accepting = True
        self.summary_lbl.setText(self.tr("Em andamento..."))
        self._set_busy(True)
        self._set_progress_rows_visible(True, network=False)
        self._set_phase_message(self.tr("Instalando..."))
        self._refresh_progress_ui()
        self.log_console.clear_log()
        self._console_carry = []
        self.log_console.set_session_status("running")
        self.log_console.set_interactive(False)
        self._show_output_tab(1)

        identity = identify_product(exe)
        user, password, params = self._creds_and_params()
        cols, rows = self._console_size
        self._worker = _BatchInstallWorker(
            [],
            exe_path=exe,
            desired_version=desired,
            psexec_params=params,
            user=user,
            password=password,
            identity=identity,
            max_workers=1,
            generation=generation,
            timeout=get_remote_registry_timeout(),
            pending_rows=pending,
            console_cols=cols,
            console_rows=rows,
        )
        self._connect_worker(self._worker)
        self._worker.start()
        self._log(
            "INFO",
            self.tr(
                f"Instalando em {len(pending)} host(s) da varredura."
            ),
        )
        self._log_identity(identity, desired)

    def _connect_worker(self, w: _BatchInstallWorker) -> None:
        w.progress.connect(self._on_progress)
        w.rowUpsert.connect(self._on_row_upsert)
        w.logLine.connect(self._on_log_line)
        w.consoleStarted.connect(self._on_console_started)
        w.consoleChunk.connect(self._on_console_chunk)
        w.consoleActive.connect(self._on_console_active)
        w.finished_ok.connect(self._on_ok)
        w.finished_aborted.connect(self._on_aborted)
        w.finished_err.connect(self._on_err)
        w.finished.connect(self._on_worker_finished)

    def _on_scan_progress(
        self,
        generation: int,
        done: int,
        total: int,
        _ip: str,
        found: int,
    ) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        self._scan_ips_done = done
        self._scan_ips_total = total
        self._scan_hosts_found = found
        self._refresh_progress_ui()

    def _on_scan_host_found(self, generation: int, host: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        name = (host or "").strip()
        if not name:
            return
        w = self._worker
        if w is not None:
            w.offer_host(name)

    def _on_scan_ok(self, generation: int, hosts: list) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        names = [str(h).strip() for h in hosts if str(h).strip()]
        w = self._worker
        if w is not None:
            w.close_intake()
        self._scan_hosts_found = len(names)
        self._hosts_total = len(names)
        self._scan_ips_done = self._scan_ips_total
        self._refresh_progress_ui()
        self._log(
            "INFO",
            self.tr(f"Varredura concluída: {len(names)} host(s) Windows."),
        )

    def _on_scan_aborted(self, generation: int) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        w = self._worker
        if w is not None:
            w.abort()
        if w is not None and w.isRunning():
            self._set_phase_message(self.tr("Interrompendo..."))
            return
        self._accepting = False
        self._set_busy(False)
        self._set_phase_message(self.tr("Varredura interrompida"))
        self.summary_lbl.setText(self.tr("Varredura de rede interrompida."))
        self._log("AVISO", self.tr("Varredura de rede interrompida."))

    def _on_scan_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        w = self._worker
        if w is not None:
            w.abort()
        if w is not None and w.isRunning():
            self._log("ERRO", msg)
            return
        self._accepting = False
        self._set_busy(False)
        self._set_phase_message(self.tr(f"Falha: {msg}"))
        self._log("ERRO", msg)

    def _on_progress(
        self,
        generation: int,
        done: int,
        failed: int,
        total: int,
        _host: str,
    ) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        self._hosts_done = done
        self._hosts_failed = failed
        self._hosts_total = total
        self._refresh_progress_ui()
        if self._accepting:
            self._update_summary(final=False)

    def _on_log_line(self, category: str, message: str) -> None:
        self._log(category, message)

    def _on_row_upsert(self, generation: int, row: object) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        if not isinstance(row, BatchHostRow):
            return
        key = row.host.casefold()
        self._rows[key] = row
        with pause_table_sorting(self.table):
            idx = self._visual_row_for_host(key)
            if idx < 0:
                idx = self.table.rowCount()
                self.table.insertRow(idx)
            self._fill_row(idx, row)
        if row.result == RESULT_UPDATING:
            idx = self._visual_row_for_host(key)
            if idx >= 0:
                self.table.selectRow(idx)
                item = self.table.item(idx, 5)
                if item is not None:
                    self.table.scrollToItem(
                        item, QAbstractItemView.ScrollHint.PositionAtCenter
                    )
        self._update_summary(final=False)

    def _visual_row_for_host(self, key: str) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and it.text().casefold() == key:
                return r
        return -1

    def _fill_row(self, index: int, row: BatchHostRow) -> None:
        values = row.as_tuple()
        for col, text in enumerate(values):
            item = self.table.item(index, col)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(index, col, item)
            item.setText(text)
            if col == 5:
                color = _RESULT_COLORS.get(text, COLOR_TEXT)
                item.setForeground(QBrush(QColor(color)))
            else:
                item.setForeground(QBrush(QColor(COLOR_TEXT)))

    def _on_worker_finished(self) -> None:
        if not self._ui_alive():
            return
        scan = self._scan_worker
        if scan is not None and scan.isRunning():
            return
        self._set_busy(False)

    def _on_ok(self, generation: int) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        kind = self._busy_kind
        self._accepting = False
        if kind == "scan":
            self._scan_ready = bool(self._rows)
            if self._from_network and not self._rows:
                self._refresh_progress_ui()
                self._set_phase_message(
                    self.tr("Nenhum host Windows encontrado na faixa.")
                )
                self.summary_lbl.setText(
                    self.tr("Nenhum computador encontrado na varredura.")
                )
                self._log("AVISO", self.tr("Varredura concluída sem hosts Windows."))
                return
            self._hosts_done = int(self._hosts_total or self._hosts_done)
            self._refresh_progress_ui()
            self._set_phase_message("")
            self._update_summary(final=True)
            summary = summarize_rows(list(self._rows.values()))
            self._log("OK", self.tr(f"Varredura concluída. {summary.as_text()}"))
            pending = sum(1 for row in self._rows.values() if row.needs_install)
            if pending:
                self._log(
                    "OK",
                    self.tr(
                        f"{pending} host(s) prontos para instalar. Use o botão Play."
                    ),
                )
            else:
                self._log(
                    "AVISO",
                    self.tr(
                        "Nenhum host precisa de instalação ou atualização."
                    ),
                )
            return

        self._scan_ready = False
        self._hosts_done = int(self._hosts_total or self._hosts_done)
        self._refresh_progress_ui()
        self._set_phase_message("")
        self._update_summary(final=True)
        summary = summarize_rows(list(self._rows.values()))
        self._log("OK", self.tr(f"Concluída. {summary.as_text()}"))
        self._finish_console_session()

    def _on_aborted(self, generation: int) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        kind = self._busy_kind
        self._accepting = False
        self._scan_ready = False
        self._refresh_progress_ui()
        if kind == "scan":
            self._set_phase_message(self.tr("Varredura interrompida"))
            self._update_summary(final=True, interrupted=True)
            summary = summarize_rows(list(self._rows.values()))
            self._log(
                "AVISO",
                self.tr(f"Varredura interrompida. {summary.as_text()}"),
            )
            return
        self._set_phase_message(self.tr("Instalação em lote interrompida"))
        self._update_summary(final=True, interrupted=True)
        summary = summarize_rows(list(self._rows.values()))
        self._log("AVISO", self.tr(f"Interrompida. {summary.as_text()}"))
        self._finish_console_session()

    def _on_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        kind = self._busy_kind
        self._accepting = False
        self._scan_ready = False
        self._set_phase_message(self.tr(f"Falha: {msg}"))
        self._log("ERRO", msg)
        if kind == "install":
            self._finish_console_session(error=True)

    def _update_summary(self, final: bool = False, interrupted: bool = False) -> None:
        summary = summarize_rows(list(self._rows.values()))
        prefix = ""
        if interrupted:
            prefix = self.tr("Interrompida — ")
        elif not final:
            prefix = self.tr("Em andamento — ")
        self.summary_lbl.setText(self.tr(f"{prefix}{summary.as_text()}"))
