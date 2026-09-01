"""Aba Sessões e Arquivos — PsLoggedOn, PsFile e Handle (via PsExec)."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.console_codec import decode_best_effort
from remoteops.core.win_cmd import run_captured
from remoteops.services.ops import CredentialContext
from remoteops.ui.style import SIZE_UI_SMALL
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    bind_card_stack,
    grid_in_card,
    make_card_stack,
)
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.ui.widgets.status_dot import StatusDot
from remoteops.ui.widgets.table import (
    SortableTableItem,
    configure_standard_table,
    pause_table_sorting,
)
from remoteops.utils.handle import (
    HANDLE_CLOSE_TIMEOUT_SECONDS,
    HANDLE_TIMEOUT_SECONDS,
    RemoteHandle,
    build_remote_handle_argv,
    classify_handle_error,
    friendly_handle_error,
    handle_available,
    parse_handle_close_result,
    parse_handle_output,
    psexec_available,
    resolve_handle_exe,
)
from remoteops.utils.ipc_auth import connect_ipc, release_ipc
from remoteops.utils.psfile import (
    PSFILE_CLOSE_TIMEOUT_SECONDS,
    PSFILE_TIMEOUT_SECONDS,
    RemoteOpenFile,
    build_psfile_argv,
    classify_psfile_error,
    filter_open_files,
    friendly_psfile_error,
    parse_psfile_close_result,
    parse_psfile_output,
    psfile_available,
    resolve_psfile_exe,
)
from remoteops.utils.psloggedon import (
    PSLOGGEDON_TIMEOUT_SECONDS,
    RemoteLoggedOnUser,
    build_psloggedon_argv,
    classify_psloggedon_error,
    friendly_psloggedon_error,
    parse_psloggedon_output,
    psloggedon_available,
    resolve_psloggedon_exe,
)
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import redact_command_text

_EMPTY = "—"
_USER_ROLE_ROW = Qt.ItemDataRole.UserRole + 1


def _safe_argv_text(args: Sequence[str], password: str = "") -> str:
    safe = [a if a != password else "********" for a in args]
    return redact_command_text(
        " ".join(safe),
        passwords=[password] if password else None,
    )


def _decode_proc(proc: subprocess.CompletedProcess) -> str:
    out = decode_best_effort(proc.stdout or b"").strip()
    err = decode_best_effort(proc.stderr or b"").strip()
    if out and err:
        return f"{out}\n{err}".strip()
    return out if out else err


def _muted_label(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: palette(windowText); opacity: 0.75; font-size: {SIZE_UI_SMALL}pt;"
    )
    lbl.setWordWrap(True)
    return lbl


# ── Workers ──────────────────────────────────────────────────────────────────


class _LoggedOnWorker(QThread):
    """Consulta usuários via PsLoggedOn (+ IPC$ se houver credencial)."""

    finished_ok = pyqtSignal(str, int, object)
    finished_err = pyqtSignal(str, int, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        generation: int,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.generation = int(generation)
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        auth = None
        try:
            exe = resolve_psloggedon_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psloggedon_error("tool_missing"),
                )
                return

            args = build_psloggedon_argv(exe, self.host)
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "Não foi possível montar o comando PsLoggedOn.",
                )
                return

            if self.creds.user.strip():
                auth = connect_ipc(
                    self.host, self.creds.user, self.creds.password
                )
                if not auth.connected:
                    self.finished_err.emit(
                        self.host,
                        self.generation,
                        auth.error
                        or "Falha ao autenticar em IPC$ para PsLoggedOn.",
                    )
                    return
                if auth.created:
                    self.log_line.emit(
                        f"[PSLOGGEDON] IPC$ autenticado em {self.host}."
                    )

            self.log_line.emit(
                f"[PSLOGGEDON] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=PSLOGGEDON_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psloggedon_error("timeout"),
                )
                return
            except FileNotFoundError:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psloggedon_error("tool_missing"),
                )
                return
            except OSError as exc:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psloggedon_error("failed", str(exc)),
                )
                return

            if self._abort:
                return

            text = _decode_proc(proc)
            kind = classify_psloggedon_error(text, proc.returncode)
            if kind != "ok":
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psloggedon_error(kind, text.splitlines()[0] if text else ""),
                )
                return

            rows = parse_psloggedon_output(text)
            if self._abort:
                return
            self.finished_ok.emit(self.host, self.generation, rows)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psloggedon_error("failed", str(exc)),
                )
        finally:
            if auth is not None:
                release_ipc(auth)
            self.creds.clear()


class _PsFileListWorker(QThread):
    """Lista arquivos abertos via compartilhamento (PsFile)."""

    finished_ok = pyqtSignal(str, int, object)
    finished_err = pyqtSignal(str, int, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        generation: int,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.generation = int(generation)
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psfile_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("tool_missing"),
                )
                return

            args = build_psfile_argv(
                exe,
                self.host,
                user=self.creds.user,
                password=self.creds.password,
            )
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "Não foi possível montar o comando PsFile.",
                )
                return

            self.log_line.emit(
                f"[PSFILE] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=PSFILE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("timeout"),
                )
                return
            except FileNotFoundError:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("tool_missing"),
                )
                return
            except OSError as exc:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("failed", str(exc)),
                )
                return

            if self._abort:
                return

            text = _decode_proc(proc)
            kind = classify_psfile_error(text, proc.returncode)
            if kind != "ok":
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error(kind, text.splitlines()[0] if text else ""),
                )
                return

            rows = parse_psfile_output(text)
            if self._abort:
                return
            self.finished_ok.emit(self.host, self.generation, rows)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("failed", str(exc)),
                )
        finally:
            self.creds.clear()


class _PsFileCloseWorker(QThread):
    """Fecha um arquivo aberto na rede (PsFile -c)."""

    finished_ok = pyqtSignal(str, int, str, str)  # host, gen, file_id, msg
    finished_err = pyqtSignal(str, int, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        generation: int,
        file_id: str,
        *,
        path: str = "",
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.generation = int(generation)
        self.file_id = (file_id or "").strip()
        self.path = path or ""
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psfile_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("tool_missing"),
                )
                return

            args = build_psfile_argv(
                exe,
                self.host,
                file_id=self.file_id,
                close=True,
                user=self.creds.user,
                password=self.creds.password,
            )
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "Não foi possível montar o comando de fechamento PsFile.",
                )
                return

            self.log_line.emit(
                f"[PSFILE] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=PSFILE_CLOSE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("timeout"),
                )
                return
            except OSError as exc:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("failed", str(exc)),
                )
                return

            if self._abort:
                return

            text = _decode_proc(proc)
            ok, detail = parse_psfile_close_result(text)
            if not ok and proc.returncode != 0:
                kind = classify_psfile_error(text, proc.returncode)
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error(kind, detail),
                )
                return
            if not ok:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    detail or friendly_psfile_error("failed"),
                )
                return
            self.finished_ok.emit(
                self.host, self.generation, self.file_id, detail or self.path
            )
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_psfile_error("failed", str(exc)),
                )
        finally:
            self.creds.clear()


class _HandleSearchWorker(QThread):
    """Pesquisa handles remotos via PsExec + Handle."""

    finished_ok = pyqtSignal(str, int, object)
    finished_err = pyqtSignal(str, int, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        generation: int,
        *,
        search: str = "",
        list_all: bool = False,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.generation = int(generation)
        self.search = (search or "").strip()
        self.list_all = bool(list_all)
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            if not self.search and not self.list_all:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "Informe um termo de pesquisa ou marque "
                    "«Listar todos os handles».",
                )
                return

            pstools = self.pstools_dir or get_pstools_dir()
            handle_exe = resolve_handle_exe()
            if not handle_exe or not os.path.isfile(handle_exe):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("tool_missing"),
                )
                return
            if not psexec_available(pstools):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("psexec_missing"),
                )
                return

            args = build_remote_handle_argv(
                handle_exe,
                self.host,
                search=self.search,
                list_all=self.list_all,
                user=self.creds.user,
                password=self.creds.password,
                pstools_dir=pstools,
            )
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "Não foi possível montar o comando Handle remoto.",
                )
                return

            self.log_line.emit(
                f"[HANDLE] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=HANDLE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("timeout"),
                )
                return
            except FileNotFoundError:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("psexec_missing"),
                )
                return
            except OSError as exc:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("failed", str(exc)),
                )
                return

            if self._abort:
                return

            text = _decode_proc(proc)
            rows = parse_handle_output(text)
            if self._abort:
                return
            # Handle exit 1 + "No matching handles found" é pesquisa vazia, não falha.
            # Se o CSV veio preenchido, mostra mesmo com exit code != 0 (avisos).
            if rows:
                self.finished_ok.emit(self.host, self.generation, rows)
                return
            kind = classify_handle_error(text, proc.returncode)
            if kind == "ok" or proc.returncode == 0:
                self.finished_ok.emit(self.host, self.generation, [])
                return
            if kind != "ok":
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error(kind, text.splitlines()[0] if text else ""),
                )
                return
            self.finished_ok.emit(self.host, self.generation, [])
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("failed", str(exc)),
                )
        finally:
            self.creds.clear()


class _HandleCloseWorker(QThread):
    """Fecha um handle remoto (PsExec + Handle -c)."""

    finished_ok = pyqtSignal(str, int, int, str, str)  # host, gen, pid, handle, msg
    finished_err = pyqtSignal(str, int, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        generation: int,
        pid: int,
        handle_id: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.generation = int(generation)
        self.pid = int(pid)
        self.handle_id = (handle_id or "").strip()
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            pstools = self.pstools_dir or get_pstools_dir()
            handle_exe = resolve_handle_exe()
            if not handle_exe or not os.path.isfile(handle_exe):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("tool_missing"),
                )
                return
            if not psexec_available(pstools):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("psexec_missing"),
                )
                return

            args = build_remote_handle_argv(
                handle_exe,
                self.host,
                close_handle=self.handle_id,
                close_pid=self.pid,
                user=self.creds.user,
                password=self.creds.password,
                pstools_dir=pstools,
            )
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "Não foi possível montar o comando de fechamento Handle.",
                )
                return

            self.log_line.emit(
                f"[HANDLE] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=HANDLE_CLOSE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("timeout"),
                )
                return
            except OSError as exc:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("failed", str(exc)),
                )
                return

            if self._abort:
                return

            text = _decode_proc(proc)
            ok, detail = parse_handle_close_result(text)
            if not ok:
                kind = classify_handle_error(text, proc.returncode)
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error(kind, detail)
                    if kind != "ok"
                    else detail,
                )
                return
            self.finished_ok.emit(
                self.host,
                self.generation,
                self.pid,
                self.handle_id,
                detail or "Handle fechado.",
            )
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    friendly_handle_error("failed", str(exc)),
                )
        finally:
            self.creds.clear()


# ── Aba ──────────────────────────────────────────────────────────────────────


class SessoesArquivosTab(QWidget):
    """Usuários conectados, arquivos de rede e handles de processo no host remoto."""

    openProcessPidRequested = pyqtSignal(int)

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

        self._loggedon_worker: Optional[_LoggedOnWorker] = None
        self._psfile_worker: Optional[_PsFileListWorker] = None
        self._psfile_close_worker: Optional[_PsFileCloseWorker] = None
        self._handle_worker: Optional[_HandleSearchWorker] = None
        self._handle_close_worker: Optional[_HandleCloseWorker] = None

        self._loggedon_gen = 0
        self._psfile_gen = 0
        self._handle_gen = 0

        self._data_host = ""
        self._loggedon_rows: List[RemoteLoggedOnUser] = []
        self._psfile_rows: List[RemoteOpenFile] = []
        self._handle_rows: List[RemoteHandle] = []
        self._last_handle_search = ""
        self._last_handle_list_all = False
        self._handles_listed_host = ""

        self._loading_loggedon = False
        self._loading_psfile = False
        self._loading_handle = False
        self._closing = False
        self._host_online = True
        self._can_open_process = False
        self._can_close_handle = False
        self._can_close_file = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)

        self.dest_card = self._build_destination_card()
        self.users_card = self._build_users_card()
        self.arquivos_card = self._build_arquivos_card()

        root.addWidget(self.dest_card, 0)
        root.addWidget(self.users_card, 1)
        root.addWidget(self.arquivos_card, 2)

        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(1)
        root.addWidget(self.log_output, 1)

        # Destino fica fora do bind: stretch 0 / altura = conteúdo.
        # Se entrar no bind, recebe stretch≥1 e “incha” ao trocar as
        # subabas de Arquivos (sizeHint do QTabWidget muda).
        bind_card_stack(
            root,
            (
                self.users_card,
                self.arquivos_card,
                self.log_output,
            ),
        )

        if self._host_source is not None and hasattr(
            self._host_source, "textChanged"
        ):
            self._host_source.textChanged.connect(self.sync_from_host)

        self.destroyed.connect(self._on_destroyed)
        self.sync_from_host()
        self._refresh_action_buttons()

    # ── Cards ─────────────────────────────────────────────────────────────

    def _build_destination_card(self) -> CardWidget:
        card = CardWidget("\uE968", self.tr("Destino"))
        card.set_collapsible(True, collapsed=False)
        # Formulário compacto: não absorve sobra do stack.
        card.set_layout_stretch(0)
        g = grid_in_card(card)

        self.host_label = QLabel(_EMPTY)
        self.host_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.host_label.setWordWrap(True)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.host_status_dot = StatusDot()
        self.host_status_label = QLabel(self.tr("Aguardando host"))
        self.host_status_label.setStyleSheet(
            f"color: palette(mid); font-size: {SIZE_UI_SMALL}pt;"
        )
        status_row.addWidget(self.host_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(
            self.host_status_label, 0, Qt.AlignmentFlag.AlignVCenter
        )
        status_row.addStretch()
        status_wrap = QWidget()
        status_wrap.setLayout(status_row)

        add_row(g, 0, self.tr("Host"), self.host_label)
        add_row(g, 1, self.tr("Estado"), status_wrap)
        return card

    def _build_users_card(self) -> CardWidget:
        card = CardWidget("\uE77B", self.tr("Usuários conectados"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(1)

        self.users_refresh_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar usuários (PsLoggedOn)")
        )
        self.users_refresh_btn.clicked.connect(self.refresh_loggedon)
        card.add_header_button(self.users_refresh_btn)

        self.users_status_lbl = _muted_label()
        card.content_layout.addWidget(self.users_status_lbl, 0)

        self._users_spinner = DotsSpinner()
        self._users_spinner.setVisible(False)
        spin = QHBoxLayout()
        spin.setContentsMargins(0, 2, 0, 0)
        spin.addStretch()
        spin.addWidget(self._users_spinner)
        spin.addStretch()
        self._users_spin_wrap = QWidget()
        self._users_spin_wrap.setLayout(spin)
        self._users_spin_wrap.setVisible(False)
        card.content_layout.addWidget(self._users_spin_wrap, 0)

        self.users_table = QTableWidget()
        self.users_table.setColumnCount(4)
        self.users_table.setHorizontalHeaderLabels(
            [
                self.tr("Usuário"),
                self.tr("Tipo"),
                self.tr("Horário"),
                self.tr("Origem"),
            ]
        )
        configure_standard_table(self.users_table, stretch_columns=(0, 3))
        self.users_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.users_table.customContextMenuRequested.connect(
            self._users_context_menu
        )
        card.content_layout.addWidget(self.users_table, 1)
        return card

    def _build_arquivos_card(self) -> CardWidget:
        """Card único com abas: arquivos de rede (PsFile) e handles (Handle)."""
        card = CardWidget("\uE8B7", self.tr("Arquivos"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(2)

        # Ícones dinâmicos no título — visíveis conforme a subaba ativa.
        # Ordem de inserção (cada um entra à esquerda): busca → renovar handles → PsFile.
        self.arquivos_psfile_refresh_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar arquivos abertos pela rede (PsFile)")
        )
        self.arquivos_psfile_refresh_btn.clicked.connect(self.refresh_psfile)
        card.add_header_button(self.arquivos_psfile_refresh_btn)

        self.arquivos_handle_refresh_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar lista completa de handles")
        )
        self.arquivos_handle_refresh_btn.clicked.connect(
            self.refresh_handles_list_all
        )
        card.add_header_button(self.arquivos_handle_refresh_btn)

        self.arquivos_handle_search_btn = card.make_header_button(
            "\uE721", self.tr("Pesquisar handles no host remoto")
        )
        self.arquivos_handle_search_btn.clicked.connect(self.refresh_handles_search)
        card.add_header_button(self.arquivos_handle_search_btn)

        self._arquivos_tabs = QTabWidget()
        inner_bar = Mdl2TabBar(self._arquivos_tabs)
        inner_bar.setExpanding(False)
        self._arquivos_tabs.setTabBar(inner_bar)
        self._arquivos_tabs.setDocumentMode(True)
        self._arquivos_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        files_page = self._build_files_page()
        handles_page = self._build_handles_page()
        for page in (files_page, handles_page):
            page.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )

        idx_files = self._arquivos_tabs.addTab(
            files_page, self.tr("Arquivos abertos pela rede")
        )
        inner_bar.set_tab_meta(idx_files, "\uE8B7")
        idx_handles = self._arquivos_tabs.addTab(
            handles_page, self.tr("Arquivos em uso por processos")
        )
        inner_bar.set_tab_meta(idx_handles, "\uE8A5")
        self._arquivos_tabs.setCurrentIndex(0)
        self._arquivos_tabs.currentChanged.connect(self._on_arquivos_tab_changed)
        self._equalize_arquivos_tab_heights()
        self._sync_arquivos_header_buttons()

        card.content_layout.addWidget(self._arquivos_tabs, 1)
        return card

    def _arquivos_on_handles_tab(self) -> bool:
        tabs = getattr(self, "_arquivos_tabs", None)
        return tabs is not None and tabs.currentIndex() == 1

    def _sync_arquivos_header_buttons(self) -> None:
        """Mostra só os ícones da subaba ativa."""
        on_handles = self._arquivos_on_handles_tab()
        self.arquivos_psfile_refresh_btn.setVisible(not on_handles)
        self.arquivos_handle_search_btn.setVisible(on_handles)
        self.arquivos_handle_refresh_btn.setVisible(on_handles)

    def _on_arquivos_tab_changed(self, index: int) -> None:
        if not self._ui_alive():
            return
        self._sync_arquivos_header_buttons()
        self._refresh_action_buttons()
        if index != 1:
            return
        # Ao abrir a subaba de handles: listar todos (uma vez por host).
        host = self._get_host()
        if not host:
            return
        if host.casefold() == (self._handles_listed_host or "").casefold():
            return
        if not handle_available() or not psexec_available():
            return
        if not self._is_online():
            return
        self.refresh_handles_list_all()

    def _equalize_arquivos_tab_heights(self) -> None:
        """Mesma altura mínima nas subabas — evita redistribuir o stack ao trocar."""
        tabs = getattr(self, "_arquivos_tabs", None)
        if tabs is None:
            return
        pages = [tabs.widget(i) for i in range(tabs.count())]
        pages = [p for p in pages if p is not None]
        if len(pages) < 2:
            return
        target = 0
        for page in pages:
            target = max(
                target,
                page.sizeHint().height(),
                page.minimumSizeHint().height(),
            )
        if target <= 0:
            return
        for page in pages:
            page.setMinimumHeight(target)

    def _build_files_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.files_filter = QLineEdit()
        self.files_filter.setPlaceholderText(
            self.tr("Filtrar por usuário, caminho ou ID…")
        )
        self.files_filter.textChanged.connect(self._apply_psfile_filter)
        self.files_count_lbl = _muted_label()
        top.addWidget(self.files_filter, 1)
        top.addWidget(self.files_count_lbl, 0)
        lay.addLayout(top, 0)

        self.files_status_lbl = _muted_label()
        lay.addWidget(self.files_status_lbl, 0)

        self._files_spinner = DotsSpinner()
        self._files_spinner.setVisible(False)
        spin = QHBoxLayout()
        spin.setContentsMargins(0, 2, 0, 0)
        spin.addStretch()
        spin.addWidget(self._files_spinner)
        spin.addStretch()
        self._files_spin_wrap = QWidget()
        self._files_spin_wrap.setLayout(spin)
        self._files_spin_wrap.setVisible(False)
        lay.addWidget(self._files_spin_wrap, 0)

        self.files_table = QTableWidget()
        self.files_table.setColumnCount(5)
        self.files_table.setHorizontalHeaderLabels(
            [
                self.tr("ID"),
                self.tr("Usuário"),
                self.tr("Arquivo"),
                self.tr("Locks"),
                self.tr("Acesso"),
            ]
        )
        configure_standard_table(self.files_table, stretch_columns=(2,))
        self.files_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.files_table.customContextMenuRequested.connect(
            self._files_context_menu
        )
        self.files_table.itemSelectionChanged.connect(
            self._refresh_action_buttons
        )
        lay.addWidget(self.files_table, 1)
        return page

    def _build_handles_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(6)

        self.handle_search = QLineEdit()
        self.handle_search.setPlaceholderText(
            self.tr("Pesquisar arquivo, pasta ou objeto…")
        )
        self.handle_search.returnPressed.connect(self.refresh_handles_search)
        self.handle_search.setToolTip(
            self.tr(
                "Digite um termo e use o ícone de busca no título do card, "
                "ou pressione Enter. O ícone de atualizar lista todos os handles."
            )
        )
        lay.addWidget(self.handle_search, 0)

        self.handles_status_lbl = _muted_label()
        lay.addWidget(self.handles_status_lbl, 0)

        self._handles_spinner = DotsSpinner()
        self._handles_spinner.setVisible(False)
        spin = QHBoxLayout()
        spin.setContentsMargins(0, 2, 0, 0)
        spin.addStretch()
        spin.addWidget(self._handles_spinner)
        spin.addStretch()
        self._handles_spin_wrap = QWidget()
        self._handles_spin_wrap.setLayout(spin)
        self._handles_spin_wrap.setVisible(False)
        lay.addWidget(self._handles_spin_wrap, 0)

        self.handles_table = QTableWidget()
        self.handles_table.setColumnCount(6)
        self.handles_table.setHorizontalHeaderLabels(
            [
                self.tr("Processo"),
                self.tr("PID"),
                self.tr("Handle"),
                self.tr("Tipo"),
                self.tr("Caminho"),
                self.tr("Usuário"),
            ]
        )
        configure_standard_table(self.handles_table, stretch_columns=(0, 4))
        self.handles_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.handles_table.customContextMenuRequested.connect(
            self._handles_context_menu
        )
        self.handles_table.itemSelectionChanged.connect(
            self._refresh_action_buttons
        )
        lay.addWidget(self.handles_table, 1)
        return page

    # ── Ciclo de vida / host ──────────────────────────────────────────────

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

    def _is_online(self) -> bool:
        if self._online_provider is not None:
            try:
                return bool(self._online_provider())
            except Exception:
                pass
        return bool(self._host_online)

    def set_host_online(self, online: bool) -> None:
        self._host_online = bool(online)
        self._update_destination_status()
        self._refresh_action_buttons()

    def sync_from_host(self) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        self.host_label.setText(host or _EMPTY)
        self._update_destination_status()
        if host.casefold() != (self._data_host or "").casefold():
            self._invalidate_data()
        self._refresh_action_buttons()

    def refresh_all(self) -> None:
        """Carga inicial ao abrir a aba: usuários + arquivos de rede."""
        if not self._ui_alive():
            return
        self.sync_from_host()
        self.refresh_loggedon()
        self.refresh_psfile()

    def refresh_tool_capabilities(self) -> None:
        self._refresh_action_buttons()

    def _invalidate_data(self) -> None:
        self._loggedon_gen += 1
        self._psfile_gen += 1
        self._handle_gen += 1
        self._data_host = ""
        self._loggedon_rows = []
        self._psfile_rows = []
        self._handle_rows = []
        self._last_handle_search = ""
        self._last_handle_list_all = False
        self._handles_listed_host = ""
        with pause_table_sorting(self.users_table):
            self.users_table.setRowCount(0)
        with pause_table_sorting(self.files_table):
            self.files_table.setRowCount(0)
        with pause_table_sorting(self.handles_table):
            self.handles_table.setRowCount(0)
        self.users_status_lbl.setText("")
        self.files_status_lbl.setText("")
        self.files_count_lbl.setText("")
        self.handles_status_lbl.setText("")
        self._refresh_action_buttons()

    def _update_destination_status(self) -> None:
        host = self._get_host()
        if not host:
            self.host_status_dot.set_state("idle")
            self.host_status_label.setText(self.tr("Aguardando host"))
            return
        if self._is_online():
            self.host_status_dot.set_state("online")
            self.host_status_label.setText(self.tr("Online"))
        else:
            self.host_status_dot.set_state("offline")
            self.host_status_label.setText(self.tr("Offline"))

    def _on_destroyed(self, _destroyed: object = None) -> None:
        self._closing = True
        self._abort_workers(wait=False)

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._closing = True
        self._abort_workers(wait=True, wait_ms=wait_ms)

    def _abort_workers(self, *, wait: bool, wait_ms: int = 8000) -> None:
        for attr in (
            "_loggedon_worker",
            "_psfile_worker",
            "_psfile_close_worker",
            "_handle_worker",
            "_handle_close_worker",
        ):
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

    # ── Log ───────────────────────────────────────────────────────────────

    def _append_log(self, line: str) -> None:
        if not self._ui_alive():
            return
        safe = redact_command_text(line or "", passwords=None)
        self.log_output.append_log(safe)

    # ── PsLoggedOn ────────────────────────────────────────────────────────

    def refresh_loggedon(self) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host:
            self._append_log(
                self.tr("[PSLOGGEDON] Preencha o Host remoto na aba PsExec.")
            )
            self.users_status_lbl.setText(self.tr("Host remoto não informado"))
            return
        if not psloggedon_available():
            self._append_log(
                self.tr(
                    "[PSLOGGEDON] PsLoggedOn não encontrado na pasta PSTools."
                )
            )
            self.users_status_lbl.setText(self.tr("PsLoggedOn ausente"))
            return
        if not self._is_online():
            self._append_log(
                self.tr(f"[PSLOGGEDON] Host {host} offline — consulta adiada.")
            )
            self.users_status_lbl.setText(self.tr("Host offline"))
            return
        if self._loggedon_worker is not None and self._loggedon_worker.isRunning():
            return

        user, password = self._creds()
        self._loggedon_gen += 1
        gen = self._loggedon_gen
        self._set_loggedon_loading(True, host)
        self._loggedon_worker = _LoggedOnWorker(
            host,
            gen,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._loggedon_worker.log_line.connect(self._append_log)
        self._loggedon_worker.finished_ok.connect(self._on_loggedon_ok)
        self._loggedon_worker.finished_err.connect(self._on_loggedon_err)
        self._loggedon_worker.finished.connect(self._on_loggedon_finished)
        self._loggedon_worker.start()
        self._append_log(
            self.tr(f"[PSLOGGEDON] Consultando usuários em {host}…")
        )

    def _set_loggedon_loading(self, loading: bool, host: str = "") -> None:
        self._loading_loggedon = loading
        self._users_spinner.setVisible(loading)
        self._users_spin_wrap.setVisible(loading)
        if loading:
            self.users_status_lbl.setText(
                self.tr(f"Carregando usuários de {host}…")
                if host
                else self.tr("Carregando usuários…")
            )
        self._refresh_action_buttons()

    def _on_loggedon_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_loggedon_loading(False)

    def _on_loggedon_err(self, host: str, generation: int, message: str) -> None:
        if not self._ui_alive():
            return
        if generation != self._loggedon_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self.users_status_lbl.setText(self.tr("Falha ao listar usuários"))
        self._append_log(self.tr(f"[PSLOGGEDON] {message}"))

    def _on_loggedon_ok(self, host: str, generation: int, rows: object) -> None:
        if not self._ui_alive():
            return
        if generation != self._loggedon_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        list_rows = list(rows or [])
        self._data_host = host
        self._loggedon_rows = list_rows
        self._populate_users_table()
        n = len(list_rows)
        if n == 0:
            self.users_status_lbl.setText(
                self.tr(f"Nenhum usuário reportado em {host}")
            )
        else:
            self.users_status_lbl.setText(
                self.tr(f"{n} usuário(s) em {host}")
            )
        self._append_log(
            self.tr(f"[PSLOGGEDON] {n} usuário(s) em {host}.")
        )
        self._refresh_action_buttons()

    def _populate_users_table(self) -> None:
        with pause_table_sorting(self.users_table):
            self.users_table.setRowCount(0)
            self.users_table.setRowCount(len(self._loggedon_rows))
            for i, row in enumerate(self._loggedon_rows):
                values = [
                    (row.display_user, row.display_user.casefold()),
                    (row.logon_type or _EMPTY, (row.logon_type or "").casefold()),
                    (row.logon_time or _EMPTY, row.logon_time or ""),
                    (row.source or _EMPTY, (row.source or "").casefold()),
                ]
                for col, (text, sort_val) in enumerate(values):
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, sort_val)
                    if col == 0:
                        item.setData(_USER_ROLE_ROW, row)
                    self.users_table.setItem(i, col, item)

    def _selected_loggedon(self) -> Optional[RemoteLoggedOnUser]:
        items = self.users_table.selectedItems()
        if not items:
            return None
        item = self.users_table.item(items[0].row(), 0)
        if item is None:
            return None
        data = item.data(_USER_ROLE_ROW)
        return data if isinstance(data, RemoteLoggedOnUser) else None

    def _users_context_menu(self, pos) -> None:
        idx = self.users_table.indexAt(pos)
        if idx.isValid():
            self.users_table.selectRow(idx.row())
        row = self._selected_loggedon()
        menu = QMenu(self)
        act_copy_row = menu.addAction(self.tr("Copiar linha"))
        act_copy_user = menu.addAction(self.tr("Copiar usuário"))
        act_copy_row.setEnabled(row is not None)
        act_copy_user.setEnabled(row is not None)
        chosen = menu.exec(self.users_table.viewport().mapToGlobal(pos))
        if row is None:
            return
        if chosen is act_copy_row:
            self._copy_text(
                "\t".join(
                    [
                        row.display_user,
                        row.logon_type,
                        row.logon_time,
                        row.source,
                    ]
                )
            )
        elif chosen is act_copy_user:
            self._copy_text(row.display_user)

    # ── PsFile ────────────────────────────────────────────────────────────

    def refresh_psfile(self) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host:
            self._append_log(
                self.tr("[PSFILE] Preencha o Host remoto na aba PsExec.")
            )
            self.files_status_lbl.setText(self.tr("Host remoto não informado"))
            return
        if not psfile_available():
            self._append_log(
                self.tr("[PSFILE] PsFile não encontrado na pasta PSTools.")
            )
            self.files_status_lbl.setText(self.tr("PsFile ausente"))
            return
        if not self._is_online():
            self._append_log(
                self.tr(f"[PSFILE] Host {host} offline — consulta adiada.")
            )
            self.files_status_lbl.setText(self.tr("Host offline"))
            return
        if self._psfile_worker is not None and self._psfile_worker.isRunning():
            return
        if (
            self._psfile_close_worker is not None
            and self._psfile_close_worker.isRunning()
        ):
            return

        user, password = self._creds()
        self._psfile_gen += 1
        gen = self._psfile_gen
        self._set_psfile_loading(True, host)
        self._psfile_worker = _PsFileListWorker(
            host,
            gen,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._psfile_worker.log_line.connect(self._append_log)
        self._psfile_worker.finished_ok.connect(self._on_psfile_ok)
        self._psfile_worker.finished_err.connect(self._on_psfile_err)
        self._psfile_worker.finished.connect(self._on_psfile_finished)
        self._psfile_worker.start()
        self._append_log(
            self.tr(f"[PSFILE] Consultando arquivos abertos em {host}…")
        )

    def _set_psfile_loading(self, loading: bool, host: str = "") -> None:
        self._loading_psfile = loading
        self._files_spinner.setVisible(loading)
        self._files_spin_wrap.setVisible(loading)
        if loading:
            self.files_status_lbl.setText(
                self.tr(f"Carregando arquivos de {host}…")
                if host
                else self.tr("Carregando arquivos…")
            )
        self._refresh_action_buttons()

    def _on_psfile_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_psfile_loading(False)

    def _on_psfile_err(self, host: str, generation: int, message: str) -> None:
        if not self._ui_alive():
            return
        if generation != self._psfile_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self.files_status_lbl.setText(self.tr("Falha ao listar arquivos"))
        self._append_log(self.tr(f"[PSFILE] {message}"))

    def _on_psfile_ok(self, host: str, generation: int, rows: object) -> None:
        if not self._ui_alive():
            return
        if generation != self._psfile_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        list_rows = list(rows or [])
        self._data_host = host
        self._psfile_rows = list_rows
        self._populate_files_table()
        self._apply_psfile_filter()
        n = len(list_rows)
        if n == 0:
            self.files_status_lbl.setText(
                self.tr(f"Nenhum arquivo aberto pela rede em {host}")
            )
        else:
            self.files_status_lbl.setText(
                self.tr(f"{n} arquivo(s) aberto(s) em {host}")
            )
        self._append_log(self.tr(f"[PSFILE] {n} arquivo(s) em {host}."))
        self._refresh_action_buttons()

    def _populate_files_table(self) -> None:
        with pause_table_sorting(self.files_table):
            self.files_table.setRowCount(0)
            self.files_table.setRowCount(len(self._psfile_rows))
            for i, row in enumerate(self._psfile_rows):
                locks_txt = (
                    str(row.locks) if row.locks is not None else _EMPTY
                )
                locks_sort = row.locks if row.locks is not None else -1
                try:
                    id_sort = int(row.id)
                except ValueError:
                    id_sort = row.id
                values = [
                    (row.id, id_sort),
                    (row.username or _EMPTY, (row.username or "").casefold()),
                    (row.path or _EMPTY, (row.path or "").casefold()),
                    (locks_txt, locks_sort),
                    (
                        row.permissions or _EMPTY,
                        (row.permissions or "").casefold(),
                    ),
                ]
                for col, (text, sort_val) in enumerate(values):
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, sort_val)
                    if col == 0:
                        item.setData(_USER_ROLE_ROW, row)
                    self.files_table.setItem(i, col, item)

    def _apply_psfile_filter(self, *_args) -> None:
        needle = (self.files_filter.text() or "").strip()
        filtered = filter_open_files(self._psfile_rows, needle)
        keep = {id(r) for r in filtered}
        visible = 0
        for i, row in enumerate(self._psfile_rows):
            match = id(row) in keep
            self.files_table.setRowHidden(i, not match)
            if match:
                visible += 1
        self.files_count_lbl.setText(
            self.tr(f"{visible} / {len(self._psfile_rows)}")
        )

    def _selected_open_file(self) -> Optional[RemoteOpenFile]:
        items = self.files_table.selectedItems()
        if not items:
            return None
        item = self.files_table.item(items[0].row(), 0)
        if item is None:
            return None
        data = item.data(_USER_ROLE_ROW)
        return data if isinstance(data, RemoteOpenFile) else None

    def _confirm_close_file(self) -> None:
        row = self._selected_open_file()
        if row is None:
            return
        host = self._get_host()
        reply = QMessageBox.question(
            self,
            self.tr("Fechar arquivo na rede"),
            self.tr(
                f"Fechar o arquivo aberto em {host}?\n\n"
                f"ID: {row.id}\n"
                f"Usuário: {row.username or _EMPTY}\n"
                f"Arquivo: {row.path or _EMPTY}\n\n"
                "O processo remoto pode perder dados não salvos."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._close_file(row)

    def _close_file(self, row: RemoteOpenFile) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host or not row.id:
            return
        if (
            self._psfile_close_worker is not None
            and self._psfile_close_worker.isRunning()
        ):
            return
        user, password = self._creds()
        gen = self._psfile_gen
        self._psfile_close_worker = _PsFileCloseWorker(
            host,
            gen,
            row.id,
            path=row.path,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._psfile_close_worker.log_line.connect(self._append_log)
        self._psfile_close_worker.finished_ok.connect(self._on_psfile_close_ok)
        self._psfile_close_worker.finished_err.connect(self._on_psfile_close_err)
        self._psfile_close_worker.finished.connect(self._refresh_action_buttons)
        self._psfile_close_worker.start()
        self._append_log(
            self.tr(f"[PSFILE] Fechando arquivo ID {row.id} em {host}…")
        )
        self._refresh_action_buttons()

    def _on_psfile_close_ok(
        self, host: str, generation: int, file_id: str, detail: str
    ) -> None:
        if not self._ui_alive():
            return
        if generation != self._psfile_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self._append_log(
            self.tr(f"[PSFILE] Arquivo fechado (ID {file_id}): {detail}")
        )
        self.refresh_psfile()

    def _on_psfile_close_err(
        self, host: str, generation: int, message: str
    ) -> None:
        if not self._ui_alive():
            return
        if generation != self._psfile_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self._append_log(self.tr(f"[PSFILE] {message}"))

    def _files_context_menu(self, pos) -> None:
        idx = self.files_table.indexAt(pos)
        if idx.isValid():
            self.files_table.selectRow(idx.row())
        row = self._selected_open_file()
        menu = QMenu(self)
        act_copy_row = menu.addAction(self.tr("Copiar linha"))
        act_copy_path = menu.addAction(self.tr("Copiar caminho"))
        act_copy_user = menu.addAction(self.tr("Copiar usuário"))
        act_copy_id = menu.addAction(self.tr("Copiar ID"))
        menu.addSeparator()
        act_close = menu.addAction(self.tr("Fechar arquivo"))
        enabled = row is not None
        for a in (act_copy_row, act_copy_path, act_copy_user, act_copy_id):
            a.setEnabled(enabled)
        act_close.setEnabled(self._can_close_file)
        chosen = menu.exec(self.files_table.viewport().mapToGlobal(pos))
        if row is None:
            return
        if chosen is act_copy_row:
            self._copy_text(
                "\t".join(
                    [
                        row.id,
                        row.username,
                        row.path,
                        "" if row.locks is None else str(row.locks),
                        row.permissions,
                    ]
                )
            )
        elif chosen is act_copy_path:
            self._copy_text(row.path)
        elif chosen is act_copy_user:
            self._copy_text(row.username)
        elif chosen is act_copy_id:
            self._copy_text(row.id)
        elif chosen is act_close:
            self._confirm_close_file()

    # ── Handle ────────────────────────────────────────────────────────────

    def refresh_handles_list_all(self) -> None:
        """Lista todos os handles (abertura da aba / ícone atualizar)."""
        self.refresh_handles(list_all=True)

    def refresh_handles_search(self) -> None:
        """Pesquisa pelo termo do campo (ícone busca / Enter)."""
        self.refresh_handles(list_all=False)

    def refresh_handles(self, *, list_all: bool = False) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        search = "" if list_all else (self.handle_search.text() or "").strip()
        if not list_all and not search:
            QMessageBox.warning(
                self,
                self.tr("Pesquisa de handles"),
                self.tr(
                    "Informe um termo de pesquisa no campo abaixo "
                    "(nome de arquivo, pasta ou objeto)."
                ),
            )
            self.handle_search.setFocus()
            return
        if not host:
            self._append_log(
                self.tr("[HANDLE] Preencha o Host remoto na aba PsExec.")
            )
            self.handles_status_lbl.setText(self.tr("Host remoto não informado"))
            return
        if not handle_available():
            self._append_log(
                self.tr(
                    "[HANDLE] Handle não encontrado (Configurações → Handle)."
                )
            )
            self.handles_status_lbl.setText(self.tr("Handle ausente"))
            return
        if not psexec_available():
            self._append_log(
                self.tr(
                    "[HANDLE] PsExec não encontrado — necessário para Handle remoto."
                )
            )
            self.handles_status_lbl.setText(self.tr("PsExec ausente"))
            return
        if not self._is_online():
            self._append_log(
                self.tr(f"[HANDLE] Host {host} offline — consulta adiada.")
            )
            self.handles_status_lbl.setText(self.tr("Host offline"))
            return
        if self._handle_worker is not None and self._handle_worker.isRunning():
            return
        if (
            self._handle_close_worker is not None
            and self._handle_close_worker.isRunning()
        ):
            return

        user, password = self._creds()
        self._handle_gen += 1
        gen = self._handle_gen
        self._last_handle_search = search
        self._last_handle_list_all = bool(list_all)
        if list_all:
            self._handles_listed_host = host
        self._set_handle_loading(True, host)
        self._handle_worker = _HandleSearchWorker(
            host,
            gen,
            search=search,
            list_all=list_all,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._handle_worker.log_line.connect(self._append_log)
        self._handle_worker.finished_ok.connect(self._on_handle_ok)
        self._handle_worker.finished_err.connect(self._on_handle_err)
        self._handle_worker.finished.connect(self._on_handle_finished)
        self._handle_worker.start()
        kind = (
            self.tr("lista completa")
            if list_all
            else self.tr(f"pesquisa «{search}»")
        )
        self._append_log(
            self.tr(f"[HANDLE] Consultando handles em {host} ({kind})…")
        )

    def _rerun_last_handle_search(self) -> None:
        if self._last_handle_list_all:
            self.refresh_handles(list_all=True)
        elif self._last_handle_search:
            self.handle_search.setText(self._last_handle_search)
            self.refresh_handles(list_all=False)

    def _set_handle_loading(self, loading: bool, host: str = "") -> None:
        self._loading_handle = loading
        self._handles_spinner.setVisible(loading)
        self._handles_spin_wrap.setVisible(loading)
        if loading:
            self.handles_status_lbl.setText(
                self.tr(f"Pesquisando handles em {host}…")
                if host
                else self.tr("Pesquisando handles…")
            )
        self._refresh_action_buttons()

    def _on_handle_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_handle_loading(False)

    def _on_handle_err(self, host: str, generation: int, message: str) -> None:
        if not self._ui_alive():
            return
        if generation != self._handle_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self.handles_status_lbl.setText(self.tr("Falha na pesquisa de handles"))
        self._append_log(self.tr(f"[HANDLE] {message}"))

    def _on_handle_ok(self, host: str, generation: int, rows: object) -> None:
        if not self._ui_alive():
            return
        if generation != self._handle_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        list_rows = list(rows or [])
        self._data_host = host
        self._handle_rows = list_rows
        self._populate_handles_table()
        n = len(list_rows)
        if n == 0:
            self.handles_status_lbl.setText(
                self.tr(f"Nenhum handle correspondente em {host}")
            )
        else:
            self.handles_status_lbl.setText(
                self.tr(f"{n} handle(s) em {host}")
            )
        self._append_log(self.tr(f"[HANDLE] {n} handle(s) em {host}."))
        self._refresh_action_buttons()

    def _populate_handles_table(self) -> None:
        with pause_table_sorting(self.handles_table):
            self.handles_table.setRowCount(0)
            self.handles_table.setRowCount(len(self._handle_rows))
            for i, row in enumerate(self._handle_rows):
                values = [
                    (row.process_name, row.process_name.casefold()),
                    (str(row.pid), row.pid),
                    (row.handle, row.handle),
                    (row.object_type or _EMPTY, (row.object_type or "").casefold()),
                    (row.path or _EMPTY, (row.path or "").casefold()),
                    (row.user or _EMPTY, (row.user or "").casefold()),
                ]
                for col, (text, sort_val) in enumerate(values):
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, sort_val)
                    if col == 0:
                        item.setData(_USER_ROLE_ROW, row)
                    self.handles_table.setItem(i, col, item)

    def _selected_handle(self) -> Optional[RemoteHandle]:
        items = self.handles_table.selectedItems()
        if not items:
            return None
        item = self.handles_table.item(items[0].row(), 0)
        if item is None:
            return None
        data = item.data(_USER_ROLE_ROW)
        return data if isinstance(data, RemoteHandle) else None

    def _open_in_processos(self) -> None:
        row = self._selected_handle()
        if row is None:
            return
        self.openProcessPidRequested.emit(int(row.pid))

    def _confirm_close_handle(self) -> None:
        row = self._selected_handle()
        if row is None:
            return
        host = self._get_host()
        reply = QMessageBox.question(
            self,
            self.tr("Fechar handle"),
            self.tr(
                f"Fechar o handle no host {host}?\n\n"
                f"Processo: {row.process_name} (PID {row.pid})\n"
                f"Handle: {row.handle}\n"
                f"Tipo: {row.object_type or _EMPTY}\n"
                f"Caminho: {row.path or _EMPTY}\n\n"
                "Atenção: fechar handles pode corromper arquivos ou "
                "derrubar o processo. Use apenas se souber o impacto."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._close_handle(row)

    def _close_handle(self, row: RemoteHandle) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host:
            return
        if (
            self._handle_close_worker is not None
            and self._handle_close_worker.isRunning()
        ):
            return
        user, password = self._creds()
        gen = self._handle_gen
        self._handle_close_worker = _HandleCloseWorker(
            host,
            gen,
            row.pid,
            row.handle,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._handle_close_worker.log_line.connect(self._append_log)
        self._handle_close_worker.finished_ok.connect(self._on_handle_close_ok)
        self._handle_close_worker.finished_err.connect(self._on_handle_close_err)
        self._handle_close_worker.finished.connect(self._refresh_action_buttons)
        self._handle_close_worker.start()
        self._append_log(
            self.tr(
                f"[HANDLE] Fechando handle {row.handle} "
                f"(PID {row.pid}) em {host}…"
            )
        )
        self._refresh_action_buttons()

    def _on_handle_close_ok(
        self,
        host: str,
        generation: int,
        pid: int,
        handle_id: str,
        detail: str,
    ) -> None:
        if not self._ui_alive():
            return
        if generation != self._handle_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self._append_log(
            self.tr(
                f"[HANDLE] Handle {handle_id} (PID {pid}) fechado: {detail}"
            )
        )
        self._rerun_last_handle_search()

    def _on_handle_close_err(
        self, host: str, generation: int, message: str
    ) -> None:
        if not self._ui_alive():
            return
        if generation != self._handle_gen:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self._append_log(self.tr(f"[HANDLE] {message}"))

    def _handles_context_menu(self, pos) -> None:
        idx = self.handles_table.indexAt(pos)
        if idx.isValid():
            self.handles_table.selectRow(idx.row())
        row = self._selected_handle()
        menu = QMenu(self)
        act_copy_row = menu.addAction(self.tr("Copiar linha"))
        act_copy_path = menu.addAction(self.tr("Copiar caminho"))
        act_copy_pid = menu.addAction(self.tr("Copiar PID"))
        act_copy_handle = menu.addAction(self.tr("Copiar handle"))
        menu.addSeparator()
        act_open = menu.addAction(self.tr("Abrir em Processos"))
        act_close = menu.addAction(self.tr("Fechar handle"))
        enabled = row is not None
        for a in (act_copy_row, act_copy_path, act_copy_pid, act_copy_handle):
            a.setEnabled(enabled)
        act_open.setEnabled(self._can_open_process)
        act_close.setEnabled(self._can_close_handle)
        chosen = menu.exec(self.handles_table.viewport().mapToGlobal(pos))
        if row is None:
            return
        if chosen is act_copy_row:
            self._copy_text(
                "\t".join(
                    [
                        row.process_name,
                        str(row.pid),
                        row.handle,
                        row.object_type,
                        row.path,
                        row.user,
                    ]
                )
            )
        elif chosen is act_copy_path:
            self._copy_text(row.path)
        elif chosen is act_copy_pid:
            self._copy_text(str(row.pid))
        elif chosen is act_copy_handle:
            self._copy_text(row.handle)
        elif chosen is act_open:
            self._open_in_processos()
        elif chosen is act_close:
            self._confirm_close_handle()

    # ── Estado dos botões ─────────────────────────────────────────────────

    def _busy_psfile(self) -> bool:
        return bool(
            self._loading_psfile
            or (
                self._psfile_close_worker is not None
                and self._psfile_close_worker.isRunning()
            )
        )

    def _busy_handle(self) -> bool:
        return bool(
            self._loading_handle
            or (
                self._handle_close_worker is not None
                and self._handle_close_worker.isRunning()
            )
        )

    def _refresh_action_buttons(self) -> None:
        if not self._ui_alive():
            return
        online = self._is_online() and bool(self._get_host())
        loggedon_ok = psloggedon_available()
        psfile_ok = psfile_available()
        handle_ok = handle_available() and psexec_available()

        self.users_refresh_btn.setEnabled(
            online and loggedon_ok and not self._loading_loggedon
        )
        self.arquivos_psfile_refresh_btn.setEnabled(
            online and psfile_ok and not self._busy_psfile()
        )
        handle_actions_ok = online and handle_ok and not self._busy_handle()
        self.arquivos_handle_search_btn.setEnabled(handle_actions_ok)
        self.arquivos_handle_refresh_btn.setEnabled(handle_actions_ok)
        self.handle_search.setEnabled(handle_ok)

        file_sel = self._selected_open_file()
        self._can_close_file = bool(
            online
            and psfile_ok
            and file_sel is not None
            and not self._busy_psfile()
        )

        handle_sel = self._selected_handle()
        self._can_open_process = bool(
            handle_sel is not None and handle_sel.pid > 0
        )
        self._can_close_handle = bool(
            online
            and handle_ok
            and handle_sel is not None
            and not self._busy_handle()
        )

        # Degradação graciosa: dicas quando ferramenta falta
        if not loggedon_ok and not self._loading_loggedon:
            if not (self.users_status_lbl.text() or "").strip():
                self.users_status_lbl.setText(self.tr("PsLoggedOn ausente"))
        if not psfile_ok and not self._loading_psfile:
            if not (self.files_status_lbl.text() or "").strip():
                self.files_status_lbl.setText(self.tr("PsFile ausente"))
        if not handle_ok and not self._loading_handle:
            if not (self.handles_status_lbl.text() or "").strip():
                if not handle_available():
                    self.handles_status_lbl.setText(self.tr("Handle ausente"))
                else:
                    self.handles_status_lbl.setText(self.tr("PsExec ausente"))

    @staticmethod
    def _copy_text(text: str) -> None:
        clip = QGuiApplication.clipboard()
        if clip is not None:
            clip.setText(text or "")
