"""Aba Processos — PsList / PsKill / PsSuspend no host remoto."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.console_codec import decode_best_effort
from remoteops.core.win_cmd import run_captured
from remoteops.ui.style import make_icon_button, table_frame_qss
from remoteops.ui.widgets.card import CardWidget, bind_card_stack, make_card_stack
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.ui.widgets.table import (
    SortableTableItem,
    configure_standard_table,
    pause_table_sorting,
)
from remoteops.utils.processos import (
    DETAIL_FIELD_ORDER,
    MEMORY_FIELD_LABELS,
    PSKILL_TIMEOUT_SECONDS,
    PSLIST_TIMEOUT_SECONDS,
    PSSUSPEND_TIMEOUT_SECONDS,
    ProcessDetailInfo,
    ProcessMemoryInfo,
    ProcessRow,
    ProcessThreadRow,
    ProcessTreeNode,
    build_pskill_argv,
    build_pslist_argv,
    build_pssuspend_argv,
    format_memory_kb,
    is_pskill_usage_text,
    is_pslist_usage_text,
    is_pssuspend_usage_text,
    merge_ws_into_list,
    parse_pslist_detail,
    parse_pslist_list,
    parse_pslist_memory,
    parse_pslist_threads,
    parse_pslist_tree,
    pskill_available,
    pslist_available,
    pssuspend_available,
    resolve_pskill_exe,
    resolve_pslist_exe,
    resolve_pssuspend_exe,
)
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import redact_command_text


class _ListWorker(QThread):
    """Consulta lista padrão + árvore (-t) do PsList."""

    finished_ok = pyqtSignal(str, object, object, object)
    finished_err = pyqtSignal(str, str)

    def __init__(
        self,
        host: str,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        req_id: int = 0,
    ):
        super().__init__()
        self.host = host
        self.user = user or ""
        self.password = password or ""
        self.pstools_dir = pstools_dir
        self.req_id = req_id
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_pslist_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    "PsList não encontrado na pasta PSTools configurada.",
                )
                return

            list_args = build_pslist_argv(
                exe, self.host, nobanner=True, user=self.user, password=self.password
            )
            tree_args = build_pslist_argv(
                exe,
                self.host,
                tree=True,
                nobanner=True,
                user=self.user,
                password=self.password,
            )
            if not list_args or not tree_args:
                self.finished_err.emit(
                    self.host, "Não foi possível montar o comando PsList."
                )
                return

            list_text = self._run_pslist(list_args)
            if self._abort or list_text is None:
                return
            tree_text = self._run_pslist(tree_args)
            if self._abort or tree_text is None:
                return

            list_rows = parse_pslist_list(list_text)
            roots, tree_flat = parse_pslist_tree(tree_text)
            merged = merge_ws_into_list(list_rows, tree_flat)
            self.finished_ok.emit(self.host, merged, roots, tree_flat)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(self.host, f"Erro ao listar processos: {exc}")
        finally:
            self.password = ""

    def _run_pslist(self, args: List[str]) -> Optional[str]:
        try:
            proc = run_captured(args, timeout=PSLIST_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.finished_err.emit(
                self.host,
                f"PsList excedeu o tempo limite ({int(PSLIST_TIMEOUT_SECONDS)}s).",
            )
            return None
        except FileNotFoundError:
            self.finished_err.emit(
                self.host,
                f"PsList não encontrado: {args[0] if args else '?'}.",
            )
            return None
        except OSError as exc:
            self.finished_err.emit(self.host, f"Falha ao iniciar PsList: {exc}")
            return None

        if self._abort:
            return None

        out = decode_best_effort(proc.stdout or b"").strip()
        err = decode_best_effort(proc.stderr or b"").strip()
        combined = out if out else err
        if is_pslist_usage_text(combined):
            safe = [a if a != self.password else "********" for a in args]
            self.finished_err.emit(
                self.host,
                "PsList devolveu a tela de Usage. "
                f"Argv: {redact_command_text(' '.join(safe), passwords=[self.password] if self.password else None)}",
            )
            return None
        if proc.returncode != 0 and not out:
            self.finished_err.emit(
                self.host,
                err or f"Falha ao executar PsList (exit code {proc.returncode}).",
            )
            return None
        return combined


class _ActionWorker(QThread):
    """PsKill ou PsSuspend sobre um PID."""

    finished_ok = pyqtSignal(str, int, str)
    finished_err = pyqtSignal(str, int, str, str)

    def __init__(
        self,
        action: str,
        host: str,
        pid: int,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.action = action
        self.host = host
        self.pid = int(pid)
        self.user = user or ""
        self.password = password or ""
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            if self.action in ("kill", "kill_tree"):
                self._run_pskill()
            else:
                self._run_pssuspend()
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host, self.pid, self.action, f"Erro: {exc}"
                )
        finally:
            self.password = ""

    def _run_pskill(self) -> None:
        exe = resolve_pskill_exe(self.pstools_dir or get_pstools_dir())
        if not exe or not os.path.isfile(exe):
            self.finished_err.emit(
                self.host,
                self.pid,
                self.action,
                "PsKill não encontrado na pasta PSTools configurada.",
            )
            return
        args = build_pskill_argv(
            exe,
            self.host,
            self.pid,
            tree=self.action == "kill_tree",
            nobanner=True,
            user=self.user,
            password=self.password,
        )
        text = self._run_tool(args, PSKILL_TIMEOUT_SECONDS, "PsKill")
        if text is None or self._abort:
            return
        if is_pskill_usage_text(text):
            self.finished_err.emit(
                self.host, self.pid, self.action, "PsKill rejeitou o comando (Usage)."
            )
            return
        self.finished_ok.emit(self.host, self.pid, self.action)

    def _run_pssuspend(self) -> None:
        exe = resolve_pssuspend_exe(self.pstools_dir or get_pstools_dir())
        if not exe or not os.path.isfile(exe):
            self.finished_err.emit(
                self.host,
                self.pid,
                self.action,
                "PsSuspend não encontrado na pasta PSTools configurada.",
            )
            return
        args = build_pssuspend_argv(
            exe,
            self.host,
            self.pid,
            resume=self.action == "resume",
            nobanner=True,
            user=self.user,
            password=self.password,
        )
        text = self._run_tool(args, PSSUSPEND_TIMEOUT_SECONDS, "PsSuspend")
        if text is None or self._abort:
            return
        if is_pssuspend_usage_text(text):
            self.finished_err.emit(
                self.host,
                self.pid,
                self.action,
                "PsSuspend rejeitou o comando (Usage).",
            )
            return
        self.finished_ok.emit(self.host, self.pid, self.action)

    def _run_tool(
        self, args: List[str], timeout: float, label: str
    ) -> Optional[str]:
        if not args:
            self.finished_err.emit(
                self.host, self.pid, self.action, f"Não foi possível montar {label}."
            )
            return None
        try:
            proc = run_captured(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.finished_err.emit(
                self.host,
                self.pid,
                self.action,
                f"{label} excedeu o tempo limite ({int(timeout)}s).",
            )
            return None
        except FileNotFoundError:
            self.finished_err.emit(
                self.host,
                self.pid,
                self.action,
                f"{label} não encontrado: {args[0]}.",
            )
            return None
        except OSError as exc:
            self.finished_err.emit(
                self.host, self.pid, self.action, f"Falha ao iniciar {label}: {exc}"
            )
            return None

        out = decode_best_effort(proc.stdout or b"").strip()
        err = decode_best_effort(proc.stderr or b"").strip()
        combined = out if out else err
        if proc.returncode != 0:
            msg = combined or f"{label} falhou (exit code {proc.returncode})."
            self.finished_err.emit(self.host, self.pid, self.action, msg)
            return None
        return combined


class _DetailWorker(QThread):
    """PsList -x / -m / -d para um PID."""

    finished_ok = pyqtSignal(str, int, str, object)
    finished_err = pyqtSignal(str, int, str, str)

    def __init__(
        self,
        kind: str,
        host: str,
        pid: int,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        fallback: Optional[ProcessRow] = None,
    ):
        super().__init__()
        self.kind = kind
        self.host = host
        self.pid = int(pid)
        self.user = user or ""
        self.password = password or ""
        self.pstools_dir = pstools_dir
        self.fallback = fallback
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_pslist_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.pid,
                    self.kind,
                    "PsList não encontrado na pasta PSTools configurada.",
                )
                return
            kwargs = {
                "nobanner": True,
                "user": self.user,
                "password": self.password,
                "pid": self.pid,
            }
            if self.kind == "details":
                args = build_pslist_argv(exe, self.host, extended=True, **kwargs)
            elif self.kind == "memory":
                args = build_pslist_argv(exe, self.host, memory=True, **kwargs)
            else:
                args = build_pslist_argv(exe, self.host, threads=True, **kwargs)
            if not args:
                self.finished_err.emit(
                    self.host, self.pid, self.kind, "Não foi possível montar o PsList."
                )
                return

            try:
                proc = run_captured(args, timeout=PSLIST_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.pid,
                    self.kind,
                    f"PsList excedeu o tempo limite ({int(PSLIST_TIMEOUT_SECONDS)}s).",
                )
                return
            except Exception as exc:
                self.finished_err.emit(
                    self.host, self.pid, self.kind, f"Falha ao iniciar PsList: {exc}"
                )
                return

            if self._abort:
                return

            out = decode_best_effort(proc.stdout or b"").strip()
            err = decode_best_effort(proc.stderr or b"").strip()
            combined = out if out else err
            if is_pslist_usage_text(combined):
                self.finished_err.emit(
                    self.host, self.pid, self.kind, "PsList rejeitou o comando (Usage)."
                )
                return
            if proc.returncode != 0 and not out:
                self.finished_err.emit(
                    self.host,
                    self.pid,
                    self.kind,
                    err or f"PsList falhou (exit code {proc.returncode}).",
                )
                return

            if self.kind == "details":
                payload = parse_pslist_detail(combined, fallback=self.fallback)
            elif self.kind == "memory":
                payload = parse_pslist_memory(combined)
                if payload is None:
                    self.finished_err.emit(
                        self.host,
                        self.pid,
                        self.kind,
                        "Não foi possível interpretar a saída de memória.",
                    )
                    return
            else:
                name, pid, rows = parse_pslist_threads(combined)
                payload = (name, pid, rows)

            self.finished_ok.emit(self.host, self.pid, self.kind, payload)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host, self.pid, self.kind, f"Erro: {exc}"
                )
        finally:
            self.password = ""


def _style_tree(tree: QTreeWidget) -> None:
    tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
    tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
    tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
    tree.setAlternatingRowColors(True)
    tree.setUniformRowHeights(True)
    tree.setRootIsDecorated(True)
    tree.setItemsExpandable(True)
    tree.setAnimated(True)
    tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    tree.setStyleSheet(
        table_frame_qss("QTreeWidget") + "QTreeWidget::item { padding: 2px 4px; }"
    )


class _InfoDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.setMinimumHeight(320)
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(12, 12, 12, 12)
        self._root.setSpacing(8)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.accept)
        self._root.addWidget(buttons)

    def set_body(self, widget: QWidget) -> None:
        self._root.insertWidget(0, widget, 1)


def _show_details_dialog(parent: QWidget, info: ProcessDetailInfo) -> None:
    dlg = _InfoDialog(parent.tr(f"Detalhes — {info.name} ({info.pid})"), parent)
    form_wrap = QWidget()
    form = QFormLayout(form_wrap)
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(6)

    values: Dict[str, object] = {
        "name": info.name,
        "pid": info.pid,
        "pri": info.pri,
        "cpu_time": info.cpu_time,
        "threads": info.threads,
        "handles": info.handles,
    }
    mem = info.memory
    if mem is not None:
        values.update(
            {
                "ws_kb": mem.ws_kb,
                "vm_kb": mem.vm_kb,
                "priv_kb": mem.priv_kb,
                "priv_peak_kb": mem.priv_peak_kb,
                "faults": mem.faults,
                "nonpaged_kb": mem.nonpaged_kb,
                "paged_kb": mem.paged_kb,
            }
        )

    mem_keys = {
        "ws_kb",
        "vm_kb",
        "priv_kb",
        "priv_peak_kb",
        "nonpaged_kb",
        "paged_kb",
    }
    for key, label in DETAIL_FIELD_ORDER:
        if key not in values or values[key] is None or values[key] == "":
            continue
        raw = values[key]
        if key in mem_keys:
            text = format_memory_kb(raw if isinstance(raw, int) else None)
        else:
            text = str(raw)
        form.addRow(parent.tr(label) + ":", QLabel(text))

    dlg.set_body(form_wrap)
    dlg.exec()


def _show_memory_dialog(parent: QWidget, info: ProcessMemoryInfo) -> None:
    dlg = _InfoDialog(parent.tr(f"Memória — {info.name} ({info.pid})"), parent)
    form_wrap = QWidget()
    form = QFormLayout(form_wrap)
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(6)
    mapping = [
        ("vm_kb", info.vm_kb),
        ("ws_kb", info.ws_kb),
        ("priv_kb", info.priv_kb),
        ("priv_peak_kb", info.priv_peak_kb),
        ("faults", info.faults),
        ("nonpaged_kb", info.nonpaged_kb),
        ("paged_kb", info.paged_kb),
    ]
    for key, value in mapping:
        if value is None:
            continue
        label = MEMORY_FIELD_LABELS.get(key, key)
        if key == "faults":
            text = f"{value:,}".replace(",", ".")
        else:
            text = format_memory_kb(value)
        form.addRow(parent.tr(label) + ":", QLabel(text))
    dlg.set_body(form_wrap)
    dlg.exec()


def _show_threads_dialog(
    parent: QWidget,
    name: str,
    pid: int,
    rows: List[ProcessThreadRow],
) -> None:
    dlg = _InfoDialog(parent.tr(f"Threads — {name} ({pid})"), parent)
    table = QTableWidget()
    table.setColumnCount(7)
    table.setHorizontalHeaderLabels(
        [
            parent.tr("TID"),
            parent.tr("Pri"),
            parent.tr("Context Switches"),
            parent.tr("State"),
            parent.tr("User Time"),
            parent.tr("Kernel Time"),
            parent.tr("Elapsed Time"),
        ]
    )
    configure_standard_table(table, stretch_columns=(3,))
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        cells = [
            (str(row.tid), row.tid),
            ("" if row.pri is None else str(row.pri), row.pri if row.pri is not None else -1),
            (
                ""
                if row.context_switches is None
                else f"{row.context_switches:,}".replace(",", "."),
                row.context_switches if row.context_switches is not None else -1,
            ),
            (row.state, row.state),
            (row.user_time, row.user_time),
            (row.kernel_time, row.kernel_time),
            (row.elapsed_time, row.elapsed_time),
        ]
        for col, (text, sort_val) in enumerate(cells):
            item = SortableTableItem(text)
            item.setData(Qt.ItemDataRole.UserRole, sort_val)
            table.setItem(i, col, item)
    dlg.set_body(table)
    dlg.resize(720, 420)
    dlg.exec()


class ProcessosTab(QWidget):
    """Lista e controla processos remotos via PsList/PsKill/PsSuspend."""

    AUTO_INTERVALS_MS = {1: 1000, 2: 2000, 5: 5000, 10: 10000, 30: 30000}

    def __init__(
        self,
        parent=None,
        host_source: Optional[QLineEdit] = None,
        creds_provider: Optional[Callable[[], Tuple[str, str]]] = None,
        online_provider: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(parent)
        self._host_source = host_source
        self._creds_provider = creds_provider
        self._online_provider = online_provider

        self._list_worker: Optional[_ListWorker] = None
        self._action_worker: Optional[_ActionWorker] = None
        self._detail_worker: Optional[_DetailWorker] = None
        self._req_id = 0
        self._data_host = ""
        self._rows: List[ProcessRow] = []
        self._tree_roots: List[ProcessTreeNode] = []
        self._rows_by_pid: Dict[int, ProcessRow] = {}
        self._suspended_pids: Set[int] = set()
        self._loading = False
        self._closing = False
        self._selected_pid: Optional[int] = None
        self._can_details = False
        self._can_memory = False
        self._can_threads = False
        self._can_kill = False
        self._can_kill_tree = False
        self._can_suspend = False
        self._can_resume = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        self.auto_check = QCheckBox(self.tr("Atualização automática"))
        self.auto_check.setChecked(False)
        self.interval_combo = FluentComboBox()
        for secs in (1, 2, 5, 10, 30):
            self.interval_combo.addItem(self.tr(f"{secs} s"), secs)
        self.interval_combo.setCurrentIndex(2)
        self.interval_combo.setEnabled(False)
        self.refresh_btn = make_icon_button("\uE72C", self.tr("Atualizar"), size=28)
        self.refresh_btn.clicked.connect(self.refresh_processes)
        self.auto_check.toggled.connect(self._on_auto_toggled)
        self.interval_combo.currentIndexChanged.connect(self._restart_auto_timer)
        toolbar.addWidget(self._status_lbl, 1)
        toolbar.addWidget(self.auto_check, 0)
        toolbar.addWidget(self.interval_combo, 0)
        toolbar.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignRight)
        toolbar_wrap = QWidget()
        toolbar_wrap.setLayout(toolbar)
        root.addWidget(toolbar_wrap, 0)

        self.proc_card = CardWidget("\uE9D9", self.tr("Processos"))
        self.proc_card.set_collapsible(True, collapsed=False)
        self.proc_card.set_expanding(True)
        self.proc_card.set_layout_stretch(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Pesquisar processo ou PID"))
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.count_lbl)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        self.proc_card.content_layout.addWidget(top_wrap, 0)

        self._spinner = DotsSpinner()
        self._spinner.setVisible(False)
        spin_row = QHBoxLayout()
        spin_row.setContentsMargins(0, 2, 0, 0)
        spin_row.addStretch()
        spin_row.addWidget(self._spinner)
        spin_row.addStretch()
        self._spin_wrap = QWidget()
        self._spin_wrap.setLayout(spin_row)
        self._spin_wrap.setVisible(False)
        self.proc_card.content_layout.addWidget(self._spin_wrap, 0)

        self._views = QTabWidget()
        inner_bar = Mdl2TabBar(self._views)
        inner_bar.setExpanding(False)
        self._views.setTabBar(inner_bar)
        self._views.setDocumentMode(True)
        self._views.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Processo"),
                self.tr("PID"),
                self.tr("Pri"),
                self.tr("CPU Time"),
                self.tr("WS"),
                self.tr("Threads"),
                self.tr("Handles"),
            ]
        )
        configure_standard_table(self.table, stretch_columns=(0,))
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.itemDoubleClicked.connect(
            lambda *_: self._request_detail("details")
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            [
                self.tr("Processo"),
                self.tr("PID"),
                self.tr("Pri"),
                self.tr("WS"),
                self.tr("Threads"),
            ]
        )
        _style_tree(self.tree)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            self.tree.header().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        self.tree.itemDoubleClicked.connect(lambda *_: self._request_detail("details"))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)

        idx_list = self._views.addTab(self.table, self.tr("Lista"))
        inner_bar.set_tab_meta(idx_list, "\uE8FD")
        idx_tree = self._views.addTab(self.tree, self.tr("Árvore"))
        inner_bar.set_tab_meta(idx_tree, "\uE8A5")
        self._views.setCurrentIndex(0)
        self.proc_card.content_layout.addWidget(self._views, 1)

        root.addWidget(self.proc_card, 2)

        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(1)
        root.addWidget(self.log_output, 1)
        bind_card_stack(root, (self.proc_card, self.log_output))
        self.filter_edit.textChanged.connect(self._apply_filter)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(False)
        self._auto_timer.timeout.connect(self.refresh_processes)

        self.destroyed.connect(self._on_destroyed)
        self._refresh_action_buttons()

    def _ui_alive(self) -> bool:
        return not self._closing and not sip.isdeleted(self)

    def _get_host(self) -> str:
        if self._host_source is None or sip.isdeleted(self._host_source):
            return ""
        return (self._host_source.text() or "").strip().strip("\\")

    def _creds(self) -> Tuple[str, str]:
        if self._creds_provider is None:
            return "", ""
        try:
            user, password = self._creds_provider()
            return (user or "").strip(), password or ""
        except Exception:
            return "", ""

    def set_host_online(self, online: bool) -> None:
        if not online and self._auto_timer.isActive():
            self._auto_timer.stop()
        self._refresh_action_buttons()

    def sync_from_host(self) -> None:
        host = self._get_host()
        if host.casefold() != (self._data_host or "").casefold():
            self._invalidate_data()
        self.refresh_processes()

    def _invalidate_data(self) -> None:
        self._req_id += 1
        self._data_host = ""
        self._rows = []
        self._tree_roots = []
        self._rows_by_pid = {}
        self._selected_pid = None
        self._suspended_pids.clear()
        with pause_table_sorting(self.table):
            self.table.setRowCount(0)
        self.tree.clear()
        self._apply_filter()
        self._refresh_action_buttons()

    def _on_destroyed(self, _destroyed: object = None) -> None:
        self._closing = True
        self._abort_workers(wait=False)

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._closing = True
        try:
            self._auto_timer.stop()
        except Exception:
            pass
        self._abort_workers(wait=True, wait_ms=wait_ms)

    def _abort_workers(self, *, wait: bool, wait_ms: int = 8000) -> None:
        for attr in ("_list_worker", "_action_worker", "_detail_worker"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                w.abort()
            except Exception:
                pass
            if wait and w.isRunning():
                w.wait(max(0, int(wait_ms)))
            setattr(self, attr, None)

    def _on_auto_toggled(self, checked: bool) -> None:
        self.interval_combo.setEnabled(bool(checked))
        if checked:
            self._restart_auto_timer()
            self.refresh_processes()
        else:
            self._auto_timer.stop()

    def _restart_auto_timer(self, *_args) -> None:
        self._auto_timer.stop()
        if not self.auto_check.isChecked():
            return
        secs = self.interval_combo.currentData()
        ms = self.AUTO_INTERVALS_MS.get(int(secs or 5), 5000)
        self._auto_timer.start(ms)

    def refresh_processes(self) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host:
            self.log_output.append_log(
                self.tr("[PROCESSOS] Preencha o Host remoto na aba PsExec.")
            )
            self._status_lbl.setText(self.tr("Host remoto não informado"))
            return
        if not pslist_available():
            self.log_output.append_log(
                self.tr(
                    "[PROCESSOS] PsList não encontrado na pasta PSTools configurada."
                )
            )
            self._status_lbl.setText(self.tr("PsList ausente"))
            return
        if self._list_worker is not None and self._list_worker.isRunning():
            return

        if host.casefold() != (self._data_host or "").casefold() and self._data_host:
            self._invalidate_data()

        user, password = self._creds()
        self._req_id += 1
        req = self._req_id
        same_host = host.casefold() == (self._data_host or "").casefold()
        show_spinner = not (same_host and self._rows)
        if show_spinner:
            self._set_loading(True, host)
        else:
            self._status_lbl.setText(
                self.tr(f"Atualizando processos de {host}...")
            )
        self._list_worker = _ListWorker(
            host,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
            req_id=req,
        )
        self._list_worker.finished_ok.connect(self._on_list_ok)
        self._list_worker.finished_err.connect(self._on_list_err)
        self._list_worker.finished.connect(self._on_list_finished)
        self._list_worker.start()
        if show_spinner:
            self.log_output.append_log(
                self.tr(f"[PROCESSOS] Consultando processos em {host} via PsList...")
            )

    def _set_loading(self, loading: bool, host: str = "") -> None:
        self._loading = loading
        self.refresh_btn.setEnabled(not loading)
        self._spinner.setVisible(loading)
        self._spin_wrap.setVisible(loading)
        if loading:
            self._status_lbl.setText(
                self.tr(f"Carregando processos de {host}...")
                if host
                else self.tr("Carregando processos...")
            )
        self._refresh_action_buttons()

    def _on_list_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_loading(False)

    def _on_list_err(self, host: str, message: str) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        self._status_lbl.setText(self.tr("Falha ao listar processos"))
        safe = redact_command_text(message or "", passwords=None)
        self.log_output.append_log(self.tr(f"[PROCESSOS] {safe}"))

    def _on_list_ok(
        self,
        host: str,
        rows: object,
        roots: object,
        _tree_flat: object,
    ) -> None:
        if not self._ui_alive():
            return
        current = self._get_host()
        if host.casefold() != current.casefold():
            return
        list_rows = list(rows or [])
        tree_roots = list(roots or [])
        self._data_host = host
        self._rows = list_rows
        self._tree_roots = tree_roots
        self._rows_by_pid = {r.pid: r for r in list_rows}
        alive = set(self._rows_by_pid.keys())
        self._suspended_pids = {p for p in self._suspended_pids if p in alive}
        prev_pid = self._selected_pid
        self._populate_table()
        self._populate_tree()
        self._apply_filter()
        if prev_pid is not None and prev_pid in self._rows_by_pid:
            self._select_pid(prev_pid)
        elif prev_pid is not None:
            self._selected_pid = None
            self._refresh_action_buttons()
        self._status_lbl.setText(
            self.tr(f"{len(list_rows)} processo(s) em {host}")
        )
        self.log_output.append_log(
            self.tr(f"[PROCESSOS] {len(list_rows)} processo(s) em {host}.")
        )

    def _populate_table(self) -> None:
        with pause_table_sorting(self.table):
            self.table.setRowCount(0)
            self.table.setRowCount(len(self._rows))
            for i, row in enumerate(self._rows):
                values = [
                    (row.name, row.name.casefold()),
                    (str(row.pid), row.pid),
                    (
                        "" if row.pri is None else str(row.pri),
                        row.pri if row.pri is not None else -1,
                    ),
                    (row.cpu_time or "", row.cpu_time or ""),
                    (
                        format_memory_kb(row.ws_kb),
                        row.ws_kb if row.ws_kb is not None else -1,
                    ),
                    (
                        "" if row.threads is None else str(row.threads),
                        row.threads if row.threads is not None else -1,
                    ),
                    (
                        "" if row.handles is None else str(row.handles),
                        row.handles if row.handles is not None else -1,
                    ),
                ]
                for col, (text, sort_val) in enumerate(values):
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, sort_val)
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole + 1, row.pid)
                    self.table.setItem(i, col, item)

    def _populate_tree(self) -> None:
        self.tree.clear()

        def add_nodes(
            nodes: List[ProcessTreeNode], parent: Optional[QTreeWidgetItem]
        ) -> None:
            for node in nodes:
                row = node.process
                texts = [
                    row.name,
                    str(row.pid),
                    "" if row.pri is None else str(row.pri),
                    format_memory_kb(row.ws_kb),
                    "" if row.threads is None else str(row.threads),
                ]
                item = QTreeWidgetItem(texts)
                item.setData(0, Qt.ItemDataRole.UserRole, row.pid)
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                if node.children:
                    add_nodes(node.children, item)

        add_nodes(self._tree_roots, None)
        self.tree.expandToDepth(1)

    def _apply_filter(self, *_args) -> None:
        query = (self.filter_edit.text() or "").strip().casefold()
        visible = 0
        for i, row in enumerate(self._rows):
            if not query:
                match = True
            else:
                match = query in (row.name or "").casefold() or query in str(row.pid)
            self.table.setRowHidden(i, not match)
            if match:
                visible += 1
        self.count_lbl.setText(self.tr(f"{visible} / {len(self._rows)}"))

        if query and self._views.currentIndex() == 1:
            for row in self._rows:
                if query in (row.name or "").casefold() or query in str(row.pid):
                    self._select_pid_in_tree(row.pid)
                    break

    def _on_table_selection(self) -> None:
        items = self.table.selectedItems()
        if not items:
            self._selected_pid = None
            self._refresh_action_buttons()
            return
        row = items[0].row()
        pid_item = self.table.item(row, 1)
        if pid_item is None:
            self._selected_pid = None
        else:
            try:
                self._selected_pid = int(pid_item.text())
            except ValueError:
                self._selected_pid = None
        self._refresh_action_buttons()
        if self._selected_pid is not None:
            self._select_pid_in_tree(self._selected_pid, scroll=False)

    def _on_tree_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        pid = items[0].data(0, Qt.ItemDataRole.UserRole)
        try:
            self._selected_pid = int(pid)
        except (TypeError, ValueError):
            self._selected_pid = None
        self._refresh_action_buttons()
        if self._selected_pid is not None:
            self._select_pid_in_table(self._selected_pid, scroll=False)

    def _select_pid(self, pid: int) -> None:
        self._selected_pid = pid
        self._select_pid_in_table(pid)
        self._select_pid_in_tree(pid)
        self._refresh_action_buttons()

    def focus_pid(self, pid: int, *, refresh_if_missing: bool = True) -> bool:
        """Seleciona um PID (ex.: vindo de Sessões e Arquivos). Retorna se achou."""
        try:
            target = int(pid)
        except (TypeError, ValueError):
            return False
        if target in self._rows_by_pid:
            self._select_pid(target)
            return True
        self._selected_pid = target
        self._select_pid_in_table(target)
        self._select_pid_in_tree(target)
        self._refresh_action_buttons()
        if refresh_if_missing and self._ui_alive():
            self.refresh_processes()
        return target in self._rows_by_pid

    def _select_pid_in_table(self, pid: int, *, scroll: bool = True) -> None:
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 1)
                if item is None:
                    continue
                try:
                    if int(item.text()) == pid:
                        self.table.selectRow(row)
                        if scroll:
                            self.table.scrollToItem(item)
                        break
                except ValueError:
                    continue
        finally:
            self.table.blockSignals(False)

    def _select_pid_in_tree(self, pid: int, *, scroll: bool = True) -> None:
        self.tree.blockSignals(True)
        try:
            found = self.tree.findItems(
                str(pid),
                Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive,
                1,
            )
            if found:
                item = found[0]
                self.tree.setCurrentItem(item)
                if scroll:
                    self.tree.scrollToItem(item)
        finally:
            self.tree.blockSignals(False)

    def _selected_row(self) -> Optional[ProcessRow]:
        if self._selected_pid is None:
            return None
        return self._rows_by_pid.get(self._selected_pid)

    def refresh_tool_capabilities(self) -> None:
        self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        has_sel = (
            self._selected_pid is not None
            and self._selected_pid in self._rows_by_pid
        )
        list_ok = pslist_available()
        kill_ok = pskill_available()
        sus_ok = pssuspend_available()
        busy = self._loading or (
            self._action_worker is not None and self._action_worker.isRunning()
        )
        detail_busy = (
            self._detail_worker is not None and self._detail_worker.isRunning()
        )

        can_detail = bool(has_sel and list_ok and not detail_busy and not busy)
        self._can_details = can_detail
        self._can_memory = can_detail
        self._can_threads = can_detail
        self._can_kill = bool(has_sel and kill_ok and not busy)
        self._can_kill_tree = self._can_kill
        suspended = has_sel and self._selected_pid in self._suspended_pids
        self._can_suspend = bool(has_sel and sus_ok and not busy and not suspended)
        self._can_resume = bool(has_sel and sus_ok and not busy and suspended)

        self.refresh_btn.setEnabled(not self._loading and list_ok)

    def _show_context_menu(self, pos) -> None:
        sender = self.sender()
        if sender is self.table:
            idx = self.table.indexAt(pos)
            if idx.isValid():
                self.table.selectRow(idx.row())
            global_pos = self.table.viewport().mapToGlobal(pos)
        else:
            item = self.tree.itemAt(pos)
            if item is not None:
                self.tree.setCurrentItem(item)
                self._on_tree_selection()
            global_pos = self.tree.viewport().mapToGlobal(pos)

        menu = QMenu(self)
        act_details = menu.addAction(self.tr("Detalhes"))
        act_mem = menu.addAction(self.tr("Ver memória"))
        act_thr = menu.addAction(self.tr("Ver threads"))
        menu.addSeparator()
        act_kill = menu.addAction(self.tr("Encerrar processo"))
        act_kill_t = menu.addAction(self.tr("Encerrar processo e filhos"))
        menu.addSeparator()
        act_sus = menu.addAction(self.tr("Suspender"))
        act_res = menu.addAction(self.tr("Retomar"))

        act_details.setEnabled(self._can_details)
        act_mem.setEnabled(self._can_memory)
        act_thr.setEnabled(self._can_threads)
        act_kill.setEnabled(self._can_kill)
        act_kill_t.setEnabled(self._can_kill_tree)
        act_sus.setEnabled(self._can_suspend)
        act_res.setEnabled(self._can_resume)

        chosen = menu.exec(global_pos)
        if chosen is act_details:
            self._request_detail("details")
        elif chosen is act_mem:
            self._request_detail("memory")
        elif chosen is act_thr:
            self._request_detail("threads")
        elif chosen is act_kill:
            self._confirm_kill(False)
        elif chosen is act_kill_t:
            self._confirm_kill(True)
        elif chosen is act_sus:
            self._run_action("suspend")
        elif chosen is act_res:
            self._run_action("resume")

    def _request_detail(self, kind: str) -> None:
        row = self._selected_row()
        if row is None or not self._ui_alive():
            return
        if self._detail_worker is not None and self._detail_worker.isRunning():
            return
        host = self._get_host()
        user, password = self._creds()
        self._detail_worker = _DetailWorker(
            kind,
            host,
            row.pid,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
            fallback=row,
        )
        self._detail_worker.finished_ok.connect(self._on_detail_ok)
        self._detail_worker.finished_err.connect(self._on_detail_err)
        self._detail_worker.finished.connect(self._refresh_action_buttons)
        self._status_lbl.setText(self.tr(f"Consultando {kind} de PID {row.pid}..."))
        self._detail_worker.start()
        self._refresh_action_buttons()

    def _on_detail_ok(self, host: str, pid: int, kind: str, payload: object) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        if kind == "details" and isinstance(payload, ProcessDetailInfo):
            _show_details_dialog(self, payload)
        elif kind == "memory" and isinstance(payload, ProcessMemoryInfo):
            _show_memory_dialog(self, payload)
        elif kind == "threads" and isinstance(payload, tuple):
            name, _pid, rows = payload
            row = self._rows_by_pid.get(pid)
            _show_threads_dialog(
                self,
                name or (row.name if row else str(pid)),
                pid,
                list(rows or []),
            )
        self._status_lbl.setText(
            self.tr(f"{len(self._rows)} processo(s) em {self._data_host}")
            if self._data_host
            else ""
        )

    def _on_detail_err(self, host: str, pid: int, kind: str, message: str) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        self.log_output.append_log(
            self.tr(f"[PROCESSOS] {kind} PID {pid}: {message}")
        )
        self._status_lbl.setText(self.tr(f"Falha em {kind}"))

    def _confirm_kill(self, tree: bool) -> None:
        row = self._selected_row()
        if row is None:
            return
        host = self._get_host()
        if tree:
            body = self.tr(
                "O processo e seus processos descendentes serão encerrados."
            )
            title = self.tr("Encerrar processo e filhos?")
        else:
            body = self.tr(
                "O processo será encerrado imediatamente e dados não salvos podem ser perdidos."
            )
            title = self.tr("Encerrar processo?")
        text = f"{row.name}\nPID: {row.pid}\nHost: {host}\n\n{body}"
        reply = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._run_action("kill_tree" if tree else "kill")

    def _run_action(self, action: str) -> None:
        row = self._selected_row()
        if row is None or not self._ui_alive():
            return
        if self._action_worker is not None and self._action_worker.isRunning():
            return
        host = self._get_host()
        user, password = self._creds()
        self._action_worker = _ActionWorker(
            action,
            host,
            row.pid,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._action_worker.finished_ok.connect(self._on_action_ok)
        self._action_worker.finished_err.connect(self._on_action_err)
        self._action_worker.finished.connect(self._refresh_action_buttons)
        labels = {
            "kill": "encerrar",
            "kill_tree": "encerrar (com filhos)",
            "suspend": "suspender",
            "resume": "retomar",
        }
        self.log_output.append_log(
            self.tr(
                f"[PROCESSOS] Ação '{labels.get(action, action)}' em "
                f"{row.name} (PID {row.pid}) @ {host}..."
            )
        )
        self._action_worker.start()
        self._refresh_action_buttons()

    def _on_action_ok(self, host: str, pid: int, action: str) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        if action == "suspend":
            self._suspended_pids.add(pid)
            self.log_output.append_log(
                self.tr(f"[PROCESSOS] Processo PID {pid} suspenso.")
            )
            self._refresh_action_buttons()
        elif action == "resume":
            self._suspended_pids.discard(pid)
            self.log_output.append_log(
                self.tr(f"[PROCESSOS] Processo PID {pid} retomado.")
            )
            self._refresh_action_buttons()
        else:
            self._suspended_pids.discard(pid)
            self.log_output.append_log(
                self.tr(f"[PROCESSOS] Processo PID {pid} encerrado.")
            )
            self.refresh_processes()

    def _on_action_err(
        self, host: str, pid: int, action: str, message: str
    ) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        self.log_output.append_log(
            self.tr(f"[PROCESSOS] Falha ({action}) PID {pid}: {message}")
        )
        self._status_lbl.setText(self.tr("Falha na ação"))
