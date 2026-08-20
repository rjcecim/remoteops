"""Aba Instalação em Lote — instala o EXE do FileSelector em vários hosts."""

from __future__ import annotations

import os
import threading
from queue import Queue
from typing import Dict, List, Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from remoteops.services.batch_install import (
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
    table_frame_qss,
)
from remoteops.ui.tabs.appsearch import _NetworkScanWorker
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
from remoteops.ui.widgets.table import enable_header_sorting, pause_table_sorting
from remoteops.utils.hosts import load_hosts_file
from remoteops.utils.network_range import (
    get_network_range_config,
    ips_for_config,
    network_range_search_mode,
)
from remoteops.utils.ping import ping_host
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
    logLine = pyqtSignal(str)
    finished_ok = pyqtSignal(int)
    finished_aborted = pyqtSignal(int)
    finished_err = pyqtSignal(int, str)

    def __init__(
        self,
        hosts: List[str],
        *,
        exe_path: str,
        extra_args: str,
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
    ):
        super().__init__()
        self.hosts = list(hosts)
        self.exe_path = exe_path
        self.extra_args = extra_args or ""
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

    def _run_installs(self, pending_installs: List[BatchHostRow]) -> None:
        pending_installs.sort(key=lambda item: (item.order, item.host.casefold()))
        if pending_installs and not self._abort:
            self.logLine.emit(
                f"[LOTE] Instalando em {len(pending_installs)} computador(es) "
                "na ordem da tabela..."
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
                row.reason = "Operação interrompida"
                row.needs_install = False
                self._upsert_row(row)
                self._mark_failed()
                continue
            row.result = RESULT_UPDATING
            row.reason = REASON_IN_PROGRESS
            self._upsert_row(row)
            self.logLine.emit(f"[LOTE] {row.host}: Atualizando...")
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
        """Ping antes do inventário; offline não entra no Remote Registry."""

        def _feed() -> None:
            remaining = list(self.hosts)
            intake = self._inbox

            def _handle(raw: str) -> None:
                host = (raw or "").strip().strip("\\")
                if not host or self._abort:
                    return
                online, _ = ping_host(host)
                if not online:
                    row = decide_host_action(
                        host=host,
                        desired_version=self.desired_version,
                        online=False,
                        inventory=None,
                        identity=self.identity,
                    )
                    self._upsert_row(row)
                    self._bump_progress(host, failed=False)
                    self.logLine.emit(f"[LOTE] {host}: computador offline")
                    return
                ping_q.put(host)

            for host in remaining:
                if self._abort:
                    break
                _handle(host)
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
            extra_args=self.extra_args,
            psexec_params=self.psexec_params,
            pstools_path=pstools,
            has_password=bool(self._password),
        )
        if spec.display_command:
            self.logLine.emit(f"[LOTE] {row.host}: {spec.display_command}")
        outcome = run_remote_installer(
            spec,
            password=self._password,
            should_cancel=lambda: self._install_proc_cancel,
            on_line=lambda line: self.logLine.emit(f"[LOTE] {row.host}: {line}"),
        )
        apply_install_outcome(row, outcome)
        if outcome.ok:
            self.logLine.emit(
                f"[LOTE] {row.host}: {row.result} (código {outcome.return_code})"
            )
        else:
            detail = outcome.message or row.reason
            self.logLine.emit(f"[LOTE] {row.host}: {row.result} — {detail}")

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
        self.logLine.emit(f"[LOTE] {status.host}: falha na detecção ({label})")


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

        root = make_card_stack(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bottom_stretch_idx = None

        install_card = CardWidget("\uE118", self.tr("Instalação em Lote"))
        install_card.set_collapsible(True, collapsed=False)
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

        self.extras_edit = QLineEdit()
        self.extras_edit.setPlaceholderText(
            self.tr("Ex.: /S   /quiet   /norestart")
        )
        self.extras_edit.setToolTip(
            self.tr("Argumentos passados ao instalador remoto (após o EXE).")
        )
        add_row(grid, 1, self.tr("Parâmetros extras"), self.extras_edit)

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
        add_row(grid, 2, self.tr("Status"), status_wrap)

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
        grid.addWidget(self._scan_row_label, 3, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(scan_wrap, 3, 1, Qt.AlignmentFlag.AlignVCenter)
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
        grid.addWidget(self._hosts_row_label, 4, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(hosts_wrap, 4, 1, Qt.AlignmentFlag.AlignVCenter)
        self._hosts_row_wrap = hosts_wrap

        self.phase_lbl = QLabel("")
        self.phase_lbl.setObjectName("lotePhase")
        self.phase_lbl.setWordWrap(True)
        self.phase_lbl.setStyleSheet(
            f"QLabel#lotePhase {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        grid.addWidget(self.phase_lbl, 5, 0, 1, 2)
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
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        for col in (2, 3, 4, 5):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.setStyleSheet(
            table_frame_qss() + "QTableWidget::item { padding: 4px 6px; }"
        )
        enable_header_sorting(self.table)
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.table.setMinimumHeight(80)
        self.results_card.content_layout.addWidget(self.table, 1)
        root.addWidget(self.results_card, 2)

        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(1)
        root.addWidget(self.log_output, 1)

        self.results_card.collapsedChanged.connect(self._redistribute_expandable_space)
        self.log_output.collapsedChanged.connect(self._redistribute_expandable_space)
        self._redistribute_expandable_space()
        self.destroyed.connect(self._abort_worker)
        self.refresh_hosts_status()
        self.refresh_product_hint()

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
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
        self.refresh_product_hint()

    def _ui_alive(self) -> bool:
        return not sip.isdeleted(self)

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

    def _set_phase_message(self, text: str = "") -> None:
        msg = (text or "").strip()
        self.phase_lbl.setText(msg)
        self.phase_lbl.setVisible(bool(msg))

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
        self.extras_edit.setEnabled(not busy)

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
            self.log_output.append_log(
                self.tr(
                    "[LOTE] Interrupção solicitada. Novas instalações não serão iniciadas. "
                    "A instalação já enviada via PsExec pode continuar no computador remoto."
                )
            )
        else:
            self._set_phase_message(self.tr("Interrompendo..."))
            self.log_output.append_log(
                self.tr("[LOTE] Interrupção solicitada. A varredura será encerrada.")
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
        self.extras_edit.clear()
        self._scan_ready = False
        self._busy_kind = ""
        self._reset_results()
        self.summary_lbl.setText("")
        self._set_progress_rows_visible(False)
        self._set_busy(False)
        self.log_output.clear_log()
        self.log_output.set_session_status("idle")
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
        self.log_output.append_log(
            self.tr(
                f"[LOTE] Varrendo {cfg.start_ip}–{cfg.end_ip} "
                f"({len(ips)} IP(s), {cfg.scan_threads} threads)..."
            )
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

    def _log_identity(self, identity, desired: str, extras: str) -> None:
        self.log_output.append_log(
            self.tr(
                f"[LOTE] Produto: {identity.label}. "
                f"Needles: {', '.join(identity.needles[:6]) or '—'}"
            )
        )
        if desired:
            self.log_output.append_log(self.tr(f"[LOTE] Versão desejada: {desired}"))
        else:
            installer_ver = (getattr(identity, "installer_version", None) or "").strip()
            if installer_ver:
                self.log_output.append_log(
                    self.tr(
                        f"[LOTE] Versão desejada vazia: usando a versão do "
                        f"instalador ({installer_ver}). Hosts já nessa versão "
                        "serão ignorados."
                    )
                )
            else:
                self.log_output.append_log(
                    self.tr(
                        "[LOTE] Versão desejada vazia e instalador sem versão: "
                        "só instala se o aplicativo não estiver presente."
                    )
                )
        if extras:
            self.log_output.append_log(
                self.tr(f"[LOTE] Parâmetros extras: {extras}")
            )

    def _begin_streaming_scan(
        self, exe: str, desired: str, generation: int
    ) -> None:
        identity = identify_product(exe)
        user, password, params = self._creds_and_params()
        extras = (self.extras_edit.text() or "").strip()
        inbox: Queue = Queue()
        self._worker = _BatchInstallWorker(
            [],
            exe_path=exe,
            extra_args=extras,
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
        self._log_identity(identity, desired, extras)

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

        identity = identify_product(exe)
        user, password, params = self._creds_and_params()
        extras = (self.extras_edit.text() or "").strip()
        workers = min(get_search_max_workers(), max(1, len(hosts)))
        self._worker = _BatchInstallWorker(
            hosts,
            exe_path=exe,
            extra_args=extras,
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
        self.log_output.append_log(
            self.tr(
                f"[LOTE] Consultando {len(hosts)} host(s) "
                f"({workers} consultas simultâneas)."
            )
        )
        self._log_identity(identity, desired, extras)

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

        identity = identify_product(exe)
        user, password, params = self._creds_and_params()
        extras = (self.extras_edit.text() or "").strip()
        self._worker = _BatchInstallWorker(
            [],
            exe_path=exe,
            extra_args=extras,
            desired_version=desired,
            psexec_params=params,
            user=user,
            password=password,
            identity=identity,
            max_workers=1,
            generation=generation,
            timeout=get_remote_registry_timeout(),
            pending_rows=pending,
        )
        self._connect_worker(self._worker)
        self._worker.start()
        self.log_output.append_log(
            self.tr(
                f"[LOTE] Instalando em {len(pending)} host(s) da varredura."
            )
        )
        self._log_identity(identity, desired, extras)

    def _connect_worker(self, w: _BatchInstallWorker) -> None:
        w.progress.connect(self._on_progress)
        w.rowUpsert.connect(self._on_row_upsert)
        w.logLine.connect(self._on_log_line)
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
        self.log_output.append_log(
            self.tr(f"[LOTE] Varredura concluída: {len(names)} host(s) Windows.")
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
        self.log_output.append_log(self.tr("[LOTE] Varredura de rede interrompida."))

    def _on_scan_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        w = self._worker
        if w is not None:
            w.abort()
        if w is not None and w.isRunning():
            self.log_output.append_log(self.tr(f"[LOTE] {msg}"))
            return
        self._accepting = False
        self._set_busy(False)
        self._set_phase_message(self.tr(f"Falha: {msg}"))
        self.log_output.append_log(self.tr(f"[LOTE] {msg}"))

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

    def _on_log_line(self, line: str) -> None:
        if not self._ui_alive():
            return
        self.log_output.append_log(line)

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
                self.log_output.append_log(
                    self.tr("[LOTE] Varredura concluída sem hosts Windows.")
                )
                return
            self._hosts_done = int(self._hosts_total or self._hosts_done)
            self._refresh_progress_ui()
            self._set_phase_message("")
            self._update_summary(final=True)
            summary = summarize_rows(list(self._rows.values()))
            self.log_output.append_log(
                self.tr(f"[LOTE] Varredura concluída. {summary.as_text()}")
            )
            pending = sum(1 for row in self._rows.values() if row.needs_install)
            if pending:
                self.log_output.append_log(
                    self.tr(
                        f"[LOTE] {pending} host(s) prontos para instalar. Use o botão Play."
                    )
                )
            else:
                self.log_output.append_log(
                    self.tr(
                        "[LOTE] Nenhum host precisa de instalação ou atualização."
                    )
                )
            return

        self._scan_ready = False
        self._hosts_done = int(self._hosts_total or self._hosts_done)
        self._refresh_progress_ui()
        self._set_phase_message("")
        self._update_summary(final=True)
        summary = summarize_rows(list(self._rows.values()))
        self.log_output.append_log(self.tr(f"[LOTE] Concluída. {summary.as_text()}"))

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
            self.log_output.append_log(
                self.tr(f"[LOTE] Varredura interrompida. {summary.as_text()}")
            )
            return
        self._set_phase_message(self.tr("Instalação em lote interrompida"))
        self._update_summary(final=True, interrupted=True)
        summary = summarize_rows(list(self._rows.values()))
        self.log_output.append_log(
            self.tr(f"[LOTE] Interrompida. {summary.as_text()}")
        )

    def _on_err(self, generation: int, msg: str) -> None:
        if not self._ui_alive() or int(generation) != int(self._generation):
            return
        self._accepting = False
        self._scan_ready = False
        self._set_phase_message(self.tr(f"Falha: {msg}"))
        self.log_output.append_log(self.tr(f"[LOTE] {msg}"))

    def _update_summary(self, final: bool = False, interrupted: bool = False) -> None:
        summary = summarize_rows(list(self._rows.values()))
        prefix = ""
        if interrupted:
            prefix = self.tr("Interrompida — ")
        elif not final:
            prefix = self.tr("Em andamento — ")
        self.summary_lbl.setText(self.tr(f"{prefix}{summary.as_text()}"))
