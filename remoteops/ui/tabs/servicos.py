"""Aba Serviços — PsService no host remoto."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.console_codec import decode_best_effort
from remoteops.core.win_cmd import run_captured
from remoteops.services.ops import CredentialContext
from remoteops.ui.style import make_icon_button
from remoteops.ui.widgets.card import CardWidget, make_card_stack
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.ui.widgets.table import (
    SortableTableItem,
    configure_standard_table,
    pause_table_sorting,
)
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import redact_command_text
from remoteops.utils.servicos import (
    PENDING_STATES,
    PSSERVICE_ACTION_TIMEOUT_SECONDS,
    PSSERVICE_FIND_TIMEOUT_SECONDS,
    PSSERVICE_QUERY_TIMEOUT_SECONDS,
    SETCONFIG_AUTO,
    SETCONFIG_DEMAND,
    SETCONFIG_DISABLED,
    SETCONFIG_TO_LABEL,
    SUPPORTED_SETCONFIG,
    RemoteService,
    ServiceFindHit,
    ServiceSecurityInfo,
    build_psservice_argv,
    classify_psservice_error,
    friendly_error_message,
    is_psservice_usage_text,
    merge_query_and_config,
    parse_psservice_config,
    parse_psservice_depend,
    parse_psservice_find,
    parse_psservice_query,
    parse_psservice_security,
    psservice_available,
    resolve_psservice_exe,
    translate_state,
)

_ACTION_POLL_SECONDS = 45.0
_ACTION_POLL_INTERVAL = 1.5

_EXPECTED_STATE = {
    "start": "RUNNING",
    "stop": "STOPPED",
    "restart": "RUNNING",
    "pause": "PAUSED",
    "cont": "RUNNING",
}


def _safe_argv_text(args: Sequence[str], password: str = "") -> str:
    safe = [a if a != password else "********" for a in args]
    return redact_command_text(
        " ".join(safe),
        passwords=[password] if password else None,
    )


def _decode_proc(proc: subprocess.CompletedProcess) -> str:
    out = decode_best_effort(proc.stdout or b"").strip()
    err = decode_best_effort(proc.stderr or b"").strip()
    return out if out else err


class _RefreshWorker(QThread):
    """Consulta query (+ config no refresh completo) e emite lista mesclada."""

    finished_ok = pyqtSignal(str, int, object)
    finished_err = pyqtSignal(str, int, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        generation: int,
        *,
        full_refresh: bool = True,
        cached_services: Optional[Sequence[RemoteService]] = None,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.generation = int(generation)
        self.full_refresh = bool(full_refresh)
        self.cached_services = list(cached_services or [])
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psservice_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    "PsService não encontrado na pasta PSTools configurada.",
                )
                return

            query_text = self._run_cmd(
                exe,
                "query",
                timeout=PSSERVICE_QUERY_TIMEOUT_SECONDS,
                label="query",
            )
            if self._abort or query_text is None:
                return
            query_rows = parse_psservice_query(query_text)

            if self.full_refresh:
                config_text = self._run_cmd(
                    exe,
                    "config",
                    timeout=PSSERVICE_QUERY_TIMEOUT_SECONDS,
                    label="config",
                )
                if self._abort or config_text is None:
                    return
                config_rows = parse_psservice_config(config_text)
                merged = merge_query_and_config(
                    query_rows, config_rows, host=self.host
                )
            else:
                merged = merge_query_and_config(
                    query_rows, self.cached_services, host=self.host
                )

            if self._abort:
                return
            self.finished_ok.emit(self.host, self.generation, merged)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.generation,
                    f"Erro ao listar serviços: {exc}",
                )
        finally:
            self.creds.clear()

    def _run_cmd(
        self,
        exe: str,
        command: str,
        *,
        timeout: float,
        label: str,
        service_name: str = "",
        extra: Optional[Sequence[str]] = None,
    ) -> Optional[str]:
        args = build_psservice_argv(
            exe,
            command,
            host=self.host,
            service_name=service_name,
            extra=extra,
            user=self.creds.user,
            password=self.creds.password,
        )
        if not args:
            self.finished_err.emit(
                self.host,
                self.generation,
                f"Não foi possível montar o comando PsService ({label}).",
            )
            return None

        self.log_line.emit(
            f"[PSSERVICE] {_safe_argv_text(args, self.creds.password)}"
        )
        try:
            proc = run_captured(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.finished_err.emit(
                self.host,
                self.generation,
                f"PsService ({label}) excedeu o tempo limite ({int(timeout)}s).",
            )
            return None
        except FileNotFoundError:
            self.finished_err.emit(
                self.host,
                self.generation,
                f"PsService não encontrado: {args[0]}.",
            )
            return None
        except OSError as exc:
            self.finished_err.emit(
                self.host,
                self.generation,
                f"Falha ao iniciar PsService ({label}): {exc}",
            )
            return None

        if self._abort:
            return None

        combined = _decode_proc(proc)
        if is_psservice_usage_text(combined):
            self.finished_err.emit(
                self.host,
                self.generation,
                "PsService devolveu a tela de Usage. "
                f"Argv: {_safe_argv_text(args, self.creds.password)}",
            )
            return None
        if proc.returncode != 0 and not combined:
            kind = classify_psservice_error(combined, proc.returncode)
            self.finished_err.emit(
                self.host,
                self.generation,
                friendly_error_message(kind, combined),
            )
            return None
        if proc.returncode != 0 and not parse_psservice_query(combined):
            kind = classify_psservice_error(combined, proc.returncode)
            self.finished_err.emit(
                self.host,
                self.generation,
                friendly_error_message(kind, combined),
            )
            return None
        return combined


class _ActionWorker(QThread):
    """start/stop/restart/pause/cont/setconfig + poll até estado estável."""

    finished_ok = pyqtSignal(str, str, str, object)
    finished_err = pyqtSignal(str, str, str, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        action: str,
        host: str,
        service_name: str,
        *,
        setconfig_token: str = "",
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.action = (action or "").strip().lower()
        self.host = host
        self.service_name = service_name
        self.setconfig_token = (setconfig_token or "").strip().lower()
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psservice_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    self.action,
                    "PsService não encontrado na pasta PSTools configurada.",
                )
                return

            extra: Optional[List[str]] = None
            cmd = self.action
            if self.action == "setconfig":
                if self.setconfig_token not in SUPPORTED_SETCONFIG:
                    self.finished_err.emit(
                        self.host,
                        self.service_name,
                        self.action,
                        "Tipo de inicialização não suportado.",
                    )
                    return
                extra = [self.setconfig_token]

            text = self._run_psservice(
                exe,
                cmd,
                timeout=PSSERVICE_ACTION_TIMEOUT_SECONDS,
                extra=extra,
            )
            if text is None or self._abort:
                return

            expected = _EXPECTED_STATE.get(self.action)
            svc = self._poll_until_stable(exe, expected)
            if self._abort:
                return
            if svc is None and expected is not None:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    self.action,
                    friendly_error_message(
                        "timeout",
                        f"Estado esperado: {translate_state(expected)}.",
                    ),
                )
                return
            self.finished_ok.emit(
                self.host, self.service_name, self.action, svc
            )
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    self.action,
                    f"Erro: {exc}",
                )
        finally:
            self.creds.clear()

    def _run_psservice(
        self,
        exe: str,
        command: str,
        *,
        timeout: float,
        extra: Optional[Sequence[str]] = None,
        treat_as_error: bool = True,
    ) -> Optional[str]:
        args = build_psservice_argv(
            exe,
            command,
            host=self.host,
            service_name=self.service_name,
            extra=extra,
            user=self.creds.user,
            password=self.creds.password,
        )
        if not args:
            self.finished_err.emit(
                self.host,
                self.service_name,
                self.action,
                f"Não foi possível montar PsService ({command}).",
            )
            return None

        self.log_line.emit(
            f"[PSSERVICE] {_safe_argv_text(args, self.creds.password)}"
        )
        try:
            proc = run_captured(args, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.finished_err.emit(
                self.host,
                self.service_name,
                self.action,
                f"PsService ({command}) excedeu o tempo limite ({int(timeout)}s).",
            )
            return None
        except FileNotFoundError:
            self.finished_err.emit(
                self.host,
                self.service_name,
                self.action,
                f"PsService não encontrado: {args[0]}.",
            )
            return None
        except OSError as exc:
            self.finished_err.emit(
                self.host,
                self.service_name,
                self.action,
                f"Falha ao iniciar PsService ({command}): {exc}",
            )
            return None

        if self._abort:
            return None

        combined = _decode_proc(proc)
        if is_psservice_usage_text(combined):
            self.finished_err.emit(
                self.host,
                self.service_name,
                self.action,
                "PsService rejeitou o comando (Usage).",
            )
            return None
        if treat_as_error and proc.returncode != 0:
            kind = classify_psservice_error(combined, proc.returncode)
            # Alguns estados "já está ..." ainda são aceitáveis
            if kind in {"ja_executando", "ja_parado"} and self.action in {
                "start",
                "stop",
                "restart",
            }:
                return combined or ""
            self.finished_err.emit(
                self.host,
                self.service_name,
                self.action,
                friendly_error_message(kind, combined),
            )
            return None
        return combined if combined else ""

    def _poll_until_stable(
        self, exe: str, expected: Optional[str]
    ) -> Optional[RemoteService]:
        if self.action == "setconfig":
            text = self._run_psservice(
                exe,
                "query",
                timeout=PSSERVICE_QUERY_TIMEOUT_SECONDS,
                treat_as_error=False,
            )
            if text is None:
                return None
            rows = parse_psservice_query(text)
            return rows[0] if rows else RemoteService(service_name=self.service_name)

        if not expected:
            return RemoteService(service_name=self.service_name)

        deadline = time.monotonic() + _ACTION_POLL_SECONDS
        last: Optional[RemoteService] = None
        while time.monotonic() < deadline:
            if self._abort:
                return None
            text = self._run_psservice(
                exe,
                "query",
                timeout=min(30.0, PSSERVICE_QUERY_TIMEOUT_SECONDS),
                treat_as_error=False,
            )
            if text is None:
                return None
            rows = parse_psservice_query(text)
            if rows:
                last = rows[0]
                state = (last.state or "").upper()
                if state == expected:
                    return last
                if state not in PENDING_STATES and state != expected:
                    # Estado estável diferente do esperado — ainda reporta o serviço
                    if state in {"RUNNING", "STOPPED", "PAUSED"}:
                        if self.action == "restart" and state == "STOPPED":
                            pass
                        else:
                            return last
            time.sleep(_ACTION_POLL_INTERVAL)
        return last if last and (last.state or "").upper() == expected else None


class _DetailWorker(QThread):
    """query + config de um serviço."""

    finished_ok = pyqtSignal(str, str, object)
    finished_err = pyqtSignal(str, str, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        service_name: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        fallback: Optional[RemoteService] = None,
    ):
        super().__init__()
        self.host = host
        self.service_name = service_name
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self.fallback = fallback
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psservice_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService não encontrado na pasta PSTools configurada.",
                )
                return

            query_text = self._run(exe, "query")
            if query_text is None or self._abort:
                return
            config_text = self._run(exe, "config")
            if config_text is None or self._abort:
                return

            q_rows = parse_psservice_query(query_text)
            c_rows = parse_psservice_config(config_text)
            merged = merge_query_and_config(q_rows, c_rows, host=self.host)
            svc = merged[0] if merged else None
            if svc is None and self.fallback is not None:
                svc = replace(self.fallback, host=self.host)
            if svc is None:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "Não foi possível interpretar os detalhes do serviço.",
                )
                return
            self.finished_ok.emit(self.host, self.service_name, svc)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host, self.service_name, f"Erro: {exc}"
                )
        finally:
            self.creds.clear()

    def _run(self, exe: str, command: str) -> Optional[str]:
        args = build_psservice_argv(
            exe,
            command,
            host=self.host,
            service_name=self.service_name,
            user=self.creds.user,
            password=self.creds.password,
        )
        if not args:
            self.finished_err.emit(
                self.host,
                self.service_name,
                f"Não foi possível montar PsService ({command}).",
            )
            return None
        self.log_line.emit(
            f"[PSSERVICE] {_safe_argv_text(args, self.creds.password)}"
        )
        try:
            proc = run_captured(args, timeout=PSSERVICE_QUERY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.finished_err.emit(
                self.host,
                self.service_name,
                f"PsService ({command}) excedeu o tempo limite.",
            )
            return None
        except Exception as exc:
            self.finished_err.emit(
                self.host,
                self.service_name,
                f"Falha ao iniciar PsService ({command}): {exc}",
            )
            return None
        if self._abort:
            return None
        combined = _decode_proc(proc)
        if is_psservice_usage_text(combined):
            self.finished_err.emit(
                self.host,
                self.service_name,
                "PsService rejeitou o comando (Usage).",
            )
            return None
        if proc.returncode != 0 and not combined:
            kind = classify_psservice_error(combined, proc.returncode)
            self.finished_err.emit(
                self.host,
                self.service_name,
                friendly_error_message(kind, combined),
            )
            return None
        return combined


class _DependWorker(QThread):
    """Lista serviços dependentes."""

    finished_ok = pyqtSignal(str, str, object, bool)
    finished_err = pyqtSignal(str, str, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        service_name: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        purpose: str = "view",
    ):
        super().__init__()
        self.host = host
        self.service_name = service_name
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self.purpose = purpose
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psservice_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService não encontrado na pasta PSTools configurada.",
                )
                return
            args = build_psservice_argv(
                exe,
                "depend",
                host=self.host,
                service_name=self.service_name,
                user=self.creds.user,
                password=self.creds.password,
            )
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "Não foi possível montar PsService (depend).",
                )
                return
            self.log_line.emit(
                f"[PSSERVICE] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=PSSERVICE_QUERY_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService (depend) excedeu o tempo limite.",
                )
                return
            except Exception as exc:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    f"Falha ao iniciar PsService (depend): {exc}",
                )
                return
            if self._abort:
                return
            combined = _decode_proc(proc)
            if is_psservice_usage_text(combined):
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService rejeitou o comando (Usage).",
                )
                return
            if proc.returncode != 0 and not combined:
                kind = classify_psservice_error(combined, proc.returncode)
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    friendly_error_message(kind, combined),
                )
                return
            deps, empty_confirmed = parse_psservice_depend(combined)
            self.finished_ok.emit(
                self.host, self.service_name, deps, empty_confirmed
            )
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host, self.service_name, f"Erro: {exc}"
                )
        finally:
            self.creds.clear()


class _SecurityWorker(QThread):
    """Consulta security de um serviço."""

    finished_ok = pyqtSignal(str, str, object)
    finished_err = pyqtSignal(str, str, str)
    log_line = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        service_name: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ):
        super().__init__()
        self.host = host
        self.service_name = service_name
        self.creds = CredentialContext(user=user or "", password=password or "")
        self.pstools_dir = pstools_dir
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            exe = resolve_psservice_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService não encontrado na pasta PSTools configurada.",
                )
                return
            args = build_psservice_argv(
                exe,
                "security",
                host=self.host,
                service_name=self.service_name,
                user=self.creds.user,
                password=self.creds.password,
            )
            if not args:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "Não foi possível montar PsService (security).",
                )
                return
            self.log_line.emit(
                f"[PSSERVICE] {_safe_argv_text(args, self.creds.password)}"
            )
            try:
                proc = run_captured(args, timeout=PSSERVICE_QUERY_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService (security) excedeu o tempo limite.",
                )
                return
            except Exception as exc:
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    f"Falha ao iniciar PsService (security): {exc}",
                )
                return
            if self._abort:
                return
            combined = _decode_proc(proc)
            if is_psservice_usage_text(combined):
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    "PsService rejeitou o comando (Usage).",
                )
                return
            if proc.returncode != 0 and not combined:
                kind = classify_psservice_error(combined, proc.returncode)
                self.finished_err.emit(
                    self.host,
                    self.service_name,
                    friendly_error_message(kind, combined),
                )
                return
            info = parse_psservice_security(combined)
            if not info.service_name:
                info.service_name = self.service_name
            self.finished_ok.emit(self.host, self.service_name, info)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    self.host, self.service_name, f"Erro: {exc}"
                )
        finally:
            self.creds.clear()


class _FindWorker(QThread):
    """Localiza instâncias do serviço na rede (abortável)."""

    finished_ok = pyqtSignal(str, object)
    finished_err = pyqtSignal(str, str)
    log_line = pyqtSignal(str)

    def __init__(self, service_name: str, *, pstools_dir: str = ""):
        super().__init__()
        self.service_name = service_name
        self.pstools_dir = pstools_dir
        self._abort = False
        self._proc: Optional[subprocess.Popen] = None

    def abort(self) -> None:
        self._abort = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self) -> None:
        try:
            exe = resolve_psservice_exe(self.pstools_dir or get_pstools_dir())
            if not exe or not os.path.isfile(exe):
                self.finished_err.emit(
                    self.service_name,
                    "PsService não encontrado na pasta PSTools configurada.",
                )
                return
            args = build_psservice_argv(
                exe, "find", service_name=self.service_name
            )
            if not args:
                self.finished_err.emit(
                    self.service_name,
                    "Não foi possível montar PsService (find).",
                )
                return
            self.log_line.emit(f"[PSSERVICE] {_safe_argv_text(args)}")
            try:
                self._proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                )
                try:
                    out_b, err_b = self._proc.communicate(
                        timeout=PSSERVICE_FIND_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                    self._proc.communicate()
                    if not self._abort:
                        self.finished_err.emit(
                            self.service_name,
                            f"PsService (find) excedeu o tempo limite "
                            f"({int(PSSERVICE_FIND_TIMEOUT_SECONDS)}s).",
                        )
                    return
            except FileNotFoundError:
                self.finished_err.emit(
                    self.service_name,
                    f"PsService não encontrado: {args[0]}.",
                )
                return
            except OSError as exc:
                self.finished_err.emit(
                    self.service_name,
                    f"Falha ao iniciar PsService (find): {exc}",
                )
                return

            if self._abort:
                return

            out = decode_best_effort(out_b or b"").strip()
            err = decode_best_effort(err_b or b"").strip()
            combined = out if out else err
            rc = self._proc.returncode if self._proc else 1
            if is_psservice_usage_text(combined):
                self.finished_err.emit(
                    self.service_name,
                    "PsService rejeitou o comando (Usage).",
                )
                return
            if rc not in (0, None) and not combined:
                kind = classify_psservice_error(combined, rc or 1)
                self.finished_err.emit(
                    self.service_name,
                    friendly_error_message(kind, combined),
                )
                return
            hits = parse_psservice_find(combined)
            self.finished_ok.emit(self.service_name, hits)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(self.service_name, f"Erro: {exc}")
        finally:
            self._proc = None


class _InfoDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
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


def _show_service_details(parent: QWidget, svc: RemoteService) -> None:
    title = parent.tr(
        f"Detalhes — {svc.display_name or svc.service_name}"
    )
    dlg = _InfoDialog(title, parent)
    form_wrap = QWidget()
    form = QFormLayout(form_wrap)
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(6)

    fields = [
        ("Nome de exibição", svc.display_name),
        ("Nome do serviço", svc.service_name),
        ("Descrição", svc.description),
        ("Status", svc.state_label),
        ("Tipo", svc.service_type),
        ("Inicialização", svc.start_type_label),
        ("Conta", svc.account),
        ("Caminho do binário", svc.binary_path),
        ("Grupo de carga", svc.load_order_group),
        ("Tag", svc.tag),
        ("Controles aceitos", ", ".join(svc.controls_accepted)),
        ("Dependências", ", ".join(svc.dependencies)),
        ("WIN32_EXIT_CODE", svc.win32_exit_code),
        ("SERVICE_EXIT_CODE", svc.service_exit_code),
        ("CHECKPOINT", svc.checkpoint),
        ("WAIT_HINT", svc.wait_hint),
        ("ERROR_CONTROL", svc.error_control),
        ("Host", svc.host),
    ]
    for label, value in fields:
        text = (value or "").strip()
        if not text:
            continue
        form.addRow(parent.tr(label) + ":", QLabel(text))

    dlg.set_body(form_wrap)
    dlg.exec()


def _show_dependents_dialog(
    parent: QWidget, service_name: str, deps: List[RemoteService]
) -> None:
    dlg = _InfoDialog(
        parent.tr(f"Dependentes — {service_name}"), parent
    )
    if not deps:
        body = QLabel(parent.tr("Nenhum serviço dependente encontrado."))
        dlg.set_body(body)
        dlg.exec()
        return

    table = QTableWidget()
    table.setColumnCount(3)
    table.setHorizontalHeaderLabels(
        [
            parent.tr("Nome de exibição"),
            parent.tr("Nome do serviço"),
            parent.tr("Status"),
        ]
    )
    configure_standard_table(table, stretch_columns=(0,))
    table.setRowCount(len(deps))
    for i, row in enumerate(deps):
        cells = [
            (row.display_name or row.service_name, (row.display_name or "").casefold()),
            (row.service_name, row.service_name.casefold()),
            (row.state_label, (row.state or "").casefold()),
        ]
        for col, (text, sort_val) in enumerate(cells):
            item = SortableTableItem(text)
            item.setData(Qt.ItemDataRole.UserRole, sort_val)
            table.setItem(i, col, item)
    dlg.set_body(table)
    dlg.resize(640, 360)
    dlg.exec()


def _show_security_dialog(parent: QWidget, info: ServiceSecurityInfo) -> None:
    title = parent.tr(
        f"Segurança — {info.display_name or info.service_name}"
    )
    dlg = _InfoDialog(title, parent)
    wrap = QWidget()
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)

    form = QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setSpacing(6)
    if info.service_name:
        form.addRow(
            parent.tr("Nome do serviço") + ":", QLabel(info.service_name)
        )
    if info.display_name:
        form.addRow(
            parent.tr("Nome de exibição") + ":", QLabel(info.display_name)
        )
    if info.account:
        form.addRow(parent.tr("Conta") + ":", QLabel(info.account))
    lay.addLayout(form)

    if info.entries:
        lay.addWidget(QLabel(parent.tr("Entradas:")))
        entries = QTextEdit()
        entries.setReadOnly(True)
        entries.setPlainText("\n\n".join(info.entries))
        entries.setMaximumHeight(140)
        lay.addWidget(entries)

    lay.addWidget(QLabel(parent.tr("Saída bruta:")))
    raw = QTextEdit()
    raw.setReadOnly(True)
    mono = QFont("Consolas")
    mono.setStyleHint(QFont.StyleHint.Monospace)
    mono.setPointSize(9)
    raw.setFont(mono)
    raw.setPlainText(info.raw_text or "")
    lay.addWidget(raw, 1)

    copy_btn = QPushButton(parent.tr("Copiar"))
    copy_btn.clicked.connect(
        lambda: QApplication.clipboard().setText(info.raw_text or "")
    )
    btn_row = QHBoxLayout()
    btn_row.addWidget(copy_btn)
    btn_row.addStretch()
    lay.addLayout(btn_row)

    dlg.set_body(wrap)
    dlg.resize(680, 520)
    dlg.exec()


class _FindDialog(QDialog):
    """Diálogo para localizar serviço na rede."""

    def __init__(self, parent: QWidget, initial_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle(parent.tr("Localizar na rede"))
        self.setMinimumWidth(560)
        self.setMinimumHeight(380)
        self._worker: Optional[_FindWorker] = None
        self._parent_tab = parent

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        row = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(
            parent.tr("Nome do serviço (ex.: Spooler)")
        )
        self.name_edit.setText(initial_name or "")
        self.search_btn = QPushButton(parent.tr("Pesquisar"))
        self.cancel_btn = QPushButton(parent.tr("Cancelar pesquisa"))
        self.cancel_btn.setEnabled(False)
        row.addWidget(self.name_edit, 1)
        row.addWidget(self.search_btn)
        row.addWidget(self.cancel_btn)
        root.addLayout(row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(
            "color: palette(windowText); opacity: 0.75;"
        )
        root.addWidget(self.status_lbl)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(
            [parent.tr("Computador"), parent.tr("Detalhe")]
        )
        configure_standard_table(self.table, stretch_columns=(1,))
        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.accept)
        root.addWidget(buttons)

        self.search_btn.clicked.connect(self._start_search)
        self.cancel_btn.clicked.connect(self._cancel_search)
        self.name_edit.returnPressed.connect(self._start_search)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._cancel_search()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)

    def _start_search(self) -> None:
        name = (self.name_edit.text() or "").strip()
        if not name:
            self.status_lbl.setText(
                self.tr("Informe o nome do serviço.")
            )
            return
        if self._worker is not None and self._worker.isRunning():
            return
        with pause_table_sorting(self.table):
            self.table.setRowCount(0)
        self.status_lbl.setText(self.tr(f"Pesquisando '{name}' na rede..."))
        self.search_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._worker = _FindWorker(name, pstools_dir=get_pstools_dir())
        self._worker.log_line.connect(self._on_log)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.finished_err.connect(self._on_err)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel_search(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.abort()
            self.status_lbl.setText(self.tr("Cancelando..."))

    def _on_log(self, line: str) -> None:
        parent = self._parent_tab
        if hasattr(parent, "_append_psservice_log"):
            parent._append_psservice_log(line)

    def _on_ok(self, service_name: str, hits: object) -> None:
        rows = list(hits or [])
        with pause_table_sorting(self.table):
            self.table.setRowCount(len(rows))
            for i, hit in enumerate(rows):
                if not isinstance(hit, ServiceFindHit):
                    continue
                cells = [
                    (hit.computer, hit.computer.casefold()),
                    (hit.detail or "", (hit.detail or "").casefold()),
                ]
                for col, (text, sort_val) in enumerate(cells):
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, sort_val)
                    self.table.setItem(i, col, item)
        self.status_lbl.setText(
            self.tr(
                f"{len(rows)} resultado(s) para '{service_name}'."
            )
        )

    def _on_err(self, service_name: str, message: str) -> None:
        safe = redact_command_text(message or "", passwords=None)
        self.status_lbl.setText(self.tr(f"Falha: {safe}"))
        parent = self._parent_tab
        if hasattr(parent, "log_output"):
            parent.log_output.append_log(
                self.tr(f"[SERVIÇOS] Find '{service_name}': {safe}")
            )

    def _on_finished(self) -> None:
        self.search_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)


class ServicosTab(QWidget):
    """Lista e controla serviços remotos via PsService."""

    AUTO_INTERVALS_MS = {5: 5000, 10: 10000, 30: 30000, 60: 60000}

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

        self._refresh_worker: Optional[_RefreshWorker] = None
        self._action_worker: Optional[_ActionWorker] = None
        self._detail_worker: Optional[_DetailWorker] = None
        self._depend_worker: Optional[_DependWorker] = None
        self._security_worker: Optional[_SecurityWorker] = None
        self._generation = 0
        self._data_host = ""
        self._services: List[RemoteService] = []
        self._by_name: Dict[str, RemoteService] = {}
        self._selected_name: Optional[str] = None
        self._loading = False
        self._closing = False
        self._pending_action: Optional[str] = None
        self._host_online = True
        self._can_details = False
        self._can_depend = False
        self._can_security = False
        self._can_find = False
        self._can_setconfig = False
        self._can_start = False
        self._can_stop = False
        self._can_restart = False
        self._can_pause = False
        self._can_cont = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)
        self._bottom_stretch_idx = None

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            "color: palette(windowText); opacity: 0.75;"
        )
        self.auto_check = QCheckBox(self.tr("Atualização automática"))
        self.auto_check.setChecked(False)
        self.interval_combo = FluentComboBox()
        for secs in (5, 10, 30, 60):
            self.interval_combo.addItem(self.tr(f"{secs} s"), secs)
        self.interval_combo.setCurrentIndex(1)  # 10 s
        self.interval_combo.setEnabled(False)
        self.refresh_btn = make_icon_button("\uE72C", self.tr("Atualizar"), size=28)
        self.refresh_btn.clicked.connect(lambda: self.refresh_services(full=True))
        self.auto_check.toggled.connect(self._on_auto_toggled)
        self.interval_combo.currentIndexChanged.connect(self._restart_auto_timer)
        toolbar.addWidget(self._status_lbl, 1)
        toolbar.addWidget(self.auto_check, 0)
        toolbar.addWidget(self.interval_combo, 0)
        toolbar.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignRight)
        toolbar_wrap = QWidget()
        toolbar_wrap.setLayout(toolbar)
        root.addWidget(toolbar_wrap, 0)

        self.svc_card = CardWidget("\uE90F", self.tr("Serviços"))
        self.svc_card.set_collapsible(True, collapsed=False)
        self.svc_card.set_expanding(True)
        self.svc_card.set_layout_stretch(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Pesquisar serviço..."))
        self.status_filter = FluentComboBox()
        for label, data in (
            (self.tr("Todos"), "all"),
            (self.tr("Executando"), "running"),
            (self.tr("Parados"), "stopped"),
            (self.tr("Pausados"), "paused"),
            (self.tr("Pendentes"), "pending"),
        ):
            self.status_filter.addItem(label, data)
        self.status_filter.setCurrentIndex(0)
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(
            "color: palette(windowText); opacity: 0.75;"
        )
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.status_filter, 0)
        top.addWidget(self.count_lbl)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        self.svc_card.content_layout.addWidget(top_wrap, 0)

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
        self.svc_card.content_layout.addWidget(self._spin_wrap, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Nome de exibição"),
                self.tr("Nome do serviço"),
                self.tr("Status"),
                self.tr("Inicialização"),
                self.tr("Conta"),
            ]
        )
        configure_standard_table(self.table, stretch_columns=(0,))
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemDoubleClicked.connect(lambda *_: self._request_details())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.svc_card.content_layout.addWidget(self.table, 1)

        root.addWidget(self.svc_card, 2)

        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(1)
        root.addWidget(self.log_output, 1)

        self.svc_card.collapsedChanged.connect(self._redistribute_expandable_space)
        self.log_output.collapsedChanged.connect(self._redistribute_expandable_space)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        self._redistribute_expandable_space()

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(False)
        self._auto_timer.timeout.connect(
            lambda: self.refresh_services(full=False)
        )

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

    def _is_online(self) -> bool:
        if self._online_provider is not None:
            try:
                return bool(self._online_provider())
            except Exception:
                pass
        return bool(self._host_online)

    def set_host_online(self, online: bool) -> None:
        self._host_online = bool(online)
        if not online and self._auto_timer.isActive():
            self._auto_timer.stop()
        self._refresh_action_buttons()

    def sync_from_host(self) -> None:
        host = self._get_host()
        if host.casefold() != (self._data_host or "").casefold():
            self._invalidate_data()
        self.refresh_services(full=True)

    def _invalidate_data(self) -> None:
        self._generation += 1
        self._data_host = ""
        self._services = []
        self._by_name = {}
        self._selected_name = None
        with pause_table_sorting(self.table):
            self.table.setRowCount(0)
        self._apply_filter()
        self._refresh_action_buttons()

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is None:
            return
        open_cards = []
        for w, stretch in ((self.svc_card, 2), (self.log_output, 1)):
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
        for attr in (
            "_refresh_worker",
            "_action_worker",
            "_detail_worker",
            "_depend_worker",
            "_security_worker",
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

    def _on_auto_toggled(self, checked: bool) -> None:
        self.interval_combo.setEnabled(bool(checked))
        if checked:
            self._restart_auto_timer()
            self.refresh_services(full=False)
        else:
            self._auto_timer.stop()

    def _restart_auto_timer(self, *_args) -> None:
        self._auto_timer.stop()
        if not self.auto_check.isChecked():
            return
        secs = self.interval_combo.currentData()
        ms = self.AUTO_INTERVALS_MS.get(int(secs or 10), 10000)
        self._auto_timer.start(ms)

    def _append_psservice_log(self, line: str) -> None:
        if not self._ui_alive():
            return
        safe = redact_command_text(line or "", passwords=None)
        self.log_output.append_log(safe)

    def refresh_services(self, full: bool = True) -> None:
        """Carrega serviços. full=True faz query+config; False só query (auto)."""
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host:
            self.log_output.append_log(
                self.tr("[SERVIÇOS] Preencha o Host remoto na aba PsExec.")
            )
            self._status_lbl.setText(self.tr("Host remoto não informado"))
            return
        if not psservice_available():
            self.log_output.append_log(
                self.tr(
                    "[SERVIÇOS] PsService não encontrado na pasta PSTools configurada."
                )
            )
            self._status_lbl.setText(self.tr("PsService ausente"))
            return
        if self._refresh_worker is not None and self._refresh_worker.isRunning():
            return

        if host.casefold() != (self._data_host or "").casefold() and self._data_host:
            self._invalidate_data()

        user, password = self._creds()
        self._generation += 1
        gen = self._generation
        same_host = host.casefold() == (self._data_host or "").casefold()
        show_spinner = not (same_host and self._services)
        if show_spinner:
            self._set_loading(True, host)
        else:
            self._status_lbl.setText(
                self.tr(f"Atualizando status em {host}...")
            )

        self._refresh_worker = _RefreshWorker(
            host,
            gen,
            full_refresh=bool(full),
            cached_services=self._services,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._refresh_worker.log_line.connect(self._append_psservice_log)
        self._refresh_worker.finished_ok.connect(self._on_refresh_ok)
        self._refresh_worker.finished_err.connect(self._on_refresh_err)
        self._refresh_worker.finished.connect(self._on_refresh_finished)
        self._refresh_worker.start()
        if show_spinner:
            kind = "query+config" if full else "query"
            self.log_output.append_log(
                self.tr(
                    f"[SERVIÇOS] Consultando serviços em {host} via PsService ({kind})..."
                )
            )

    def _set_loading(self, loading: bool, host: str = "") -> None:
        self._loading = loading
        self.refresh_btn.setEnabled(not loading)
        self._spinner.setVisible(loading)
        self._spin_wrap.setVisible(loading)
        if loading:
            self._status_lbl.setText(
                self.tr(f"Carregando serviços de {host}...")
                if host
                else self.tr("Carregando serviços...")
            )
        self._refresh_action_buttons()

    def _on_refresh_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_loading(False)

    def _on_refresh_err(self, host: str, generation: int, message: str) -> None:
        if not self._ui_alive():
            return
        if generation != self._generation:
            return
        if host.casefold() != self._get_host().casefold():
            return
        self._status_lbl.setText(self.tr("Falha ao listar serviços"))
        safe = redact_command_text(message or "", passwords=None)
        self.log_output.append_log(self.tr(f"[SERVIÇOS] {safe}"))

    def _on_refresh_ok(
        self, host: str, generation: int, services: object
    ) -> None:
        if not self._ui_alive():
            return
        if generation != self._generation:
            return
        current = self._get_host()
        if host.casefold() != current.casefold():
            return
        rows = list(services or [])
        self._data_host = host
        self._services = rows
        self._by_name = {
            (r.service_name or "").casefold(): r
            for r in rows
            if r.service_name
        }
        prev = self._selected_name
        self._populate_table()
        self._apply_filter()
        if prev and prev.casefold() in self._by_name:
            self._select_service(prev)
        elif prev:
            self._selected_name = None
            self._refresh_action_buttons()
        self._status_lbl.setText(
            self.tr(f"{len(rows)} serviço(s) em {host}")
        )
        self.log_output.append_log(
            self.tr(f"[SERVIÇOS] {len(rows)} serviço(s) em {host}.")
        )

    def _populate_table(self) -> None:
        with pause_table_sorting(self.table):
            self.table.setRowCount(0)
            self.table.setRowCount(len(self._services))
            for i, row in enumerate(self._services):
                values = [
                    (
                        row.display_name or row.service_name,
                        (row.display_name or row.service_name or "").casefold(),
                    ),
                    (row.service_name, (row.service_name or "").casefold()),
                    (row.state_label, (row.state or "").casefold()),
                    (row.start_type_label, (row.start_type or "").casefold()),
                    (row.account or "", (row.account or "").casefold()),
                ]
                for col, (text, sort_val) in enumerate(values):
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, sort_val)
                    if col == 1:
                        item.setData(
                            Qt.ItemDataRole.UserRole + 1, row.service_name
                        )
                    self.table.setItem(i, col, item)

    def _service_matches_status(
        self, row: RemoteService, filter_key: str
    ) -> bool:
        state = (row.state or "").upper()
        if filter_key == "all" or not filter_key:
            return True
        if filter_key == "running":
            return state == "RUNNING"
        if filter_key == "stopped":
            return state == "STOPPED"
        if filter_key == "paused":
            return state == "PAUSED"
        if filter_key == "pending":
            return state in PENDING_STATES
        return True

    def _apply_filter(self, *_args) -> None:
        query = (self.filter_edit.text() or "").strip().casefold()
        status_key = self.status_filter.currentData() or "all"
        visible = 0
        for i, row in enumerate(self._services):
            if query:
                hay = " ".join(
                    [
                        row.display_name or "",
                        row.service_name or "",
                        row.account or "",
                        row.state_label or "",
                    ]
                ).casefold()
                text_ok = query in hay
            else:
                text_ok = True
            status_ok = self._service_matches_status(row, str(status_key))
            match = text_ok and status_ok
            self.table.setRowHidden(i, not match)
            if match:
                visible += 1
        self.count_lbl.setText(
            self.tr(f"{visible} / {len(self._services)}")
        )

    def _on_selection(self) -> None:
        items = self.table.selectedItems()
        if not items:
            self._selected_name = None
            self._refresh_action_buttons()
            return
        row = items[0].row()
        name_item = self.table.item(row, 1)
        if name_item is None:
            self._selected_name = None
        else:
            stored = name_item.data(Qt.ItemDataRole.UserRole + 1)
            self._selected_name = str(stored or name_item.text() or "") or None
        self._refresh_action_buttons()

    def _show_context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if idx.isValid():
            self.table.selectRow(idx.row())
        global_pos = self.table.viewport().mapToGlobal(pos)

        menu = QMenu(self)
        act_details = menu.addAction(self.tr("Detalhes"))
        act_depend = menu.addAction(self.tr("Dependentes"))
        act_security = menu.addAction(self.tr("Segurança"))
        menu.addSeparator()
        act_start = menu.addAction(self.tr("Iniciar"))
        act_stop = menu.addAction(self.tr("Parar"))
        act_restart = menu.addAction(self.tr("Reiniciar"))
        menu.addSeparator()
        act_pause = menu.addAction(self.tr("Pausar"))
        act_cont = menu.addAction(self.tr("Continuar"))
        menu.addSeparator()
        act_setconfig = menu.addAction(self.tr("Alterar inicialização"))
        act_find = menu.addAction(self.tr("Localizar na rede"))

        act_details.setEnabled(self._can_details)
        act_depend.setEnabled(self._can_depend)
        act_security.setEnabled(self._can_security)
        act_start.setEnabled(self._can_start)
        act_stop.setEnabled(self._can_stop)
        act_restart.setEnabled(self._can_restart)
        act_pause.setEnabled(self._can_pause)
        act_cont.setEnabled(self._can_cont)
        act_setconfig.setEnabled(self._can_setconfig)
        act_find.setEnabled(self._can_find)

        chosen = menu.exec(global_pos)
        if chosen is act_details:
            self._request_details()
        elif chosen is act_depend:
            self._request_dependents()
        elif chosen is act_security:
            self._request_security()
        elif chosen is act_start:
            self._run_action("start")
        elif chosen is act_stop:
            self._confirm_stop_or_restart("stop")
        elif chosen is act_restart:
            self._confirm_stop_or_restart("restart")
        elif chosen is act_pause:
            self._run_action("pause")
        elif chosen is act_cont:
            self._run_action("cont")
        elif chosen is act_setconfig:
            self._change_start_type()
        elif chosen is act_find:
            self._open_find_dialog()

    def _select_service(self, service_name: str) -> None:
        key = (service_name or "").casefold()
        self._selected_name = service_name
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 1)
                if item is None:
                    continue
                stored = item.data(Qt.ItemDataRole.UserRole + 1)
                name = str(stored or item.text() or "")
                if name.casefold() == key:
                    self.table.selectRow(row)
                    self.table.scrollToItem(item)
                    break
        finally:
            self.table.blockSignals(False)
        self._refresh_action_buttons()

    def _selected_service(self) -> Optional[RemoteService]:
        if not self._selected_name:
            return None
        return self._by_name.get(self._selected_name.casefold())

    def refresh_tool_capabilities(self) -> None:
        self._refresh_action_buttons()

    def _busy(self) -> bool:
        return bool(
            self._loading
            or (
                self._action_worker is not None
                and self._action_worker.isRunning()
            )
            or (
                self._depend_worker is not None
                and self._depend_worker.isRunning()
            )
            or (
                self._security_worker is not None
                and self._security_worker.isRunning()
            )
            or (
                self._detail_worker is not None
                and self._detail_worker.isRunning()
            )
        )

    def _refresh_action_buttons(self) -> None:
        tool_ok = psservice_available()
        svc = self._selected_service()
        has_sel = svc is not None
        busy = self._busy()
        state = (svc.state or "").upper() if svc else ""
        pending = state in PENDING_STATES
        if svc is None:
            disabled_start = False
        else:
            start_u = (svc.start_type or "").upper()
            disabled_start = start_u == "DISABLED" or (
                svc.setconfig_token == SETCONFIG_DISABLED
            )

        self.refresh_btn.setEnabled(not self._loading and tool_ok)

        self._can_details = bool(has_sel and tool_ok and not busy)
        self._can_depend = self._can_details
        self._can_security = self._can_details
        self._can_find = bool(tool_ok and not busy)
        self._can_setconfig = bool(has_sel and tool_ok and not busy)

        self._can_start = bool(
            has_sel
            and tool_ok
            and not busy
            and not pending
            and state == "STOPPED"
            and not disabled_start
        )
        self._can_stop = bool(
            has_sel
            and tool_ok
            and not busy
            and not pending
            and state == "RUNNING"
            and (svc.accepts_stop if svc else True)
        )
        self._can_restart = self._can_stop
        self._can_pause = bool(
            has_sel
            and tool_ok
            and not busy
            and not pending
            and state == "RUNNING"
            and (svc.accepts_pause if svc else False)
        )
        self._can_cont = bool(
            has_sel
            and tool_ok
            and not busy
            and not pending
            and state == "PAUSED"
        )

        if not tool_ok:
            self.refresh_btn.setToolTip(
                self.tr("PsService não encontrado na pasta PSTools configurada.")
            )
        else:
            self.refresh_btn.setToolTip("")

    def _request_details(self) -> None:
        svc = self._selected_service()
        if svc is None or not self._ui_alive():
            return
        if self._detail_worker is not None and self._detail_worker.isRunning():
            return
        host = self._get_host()
        user, password = self._creds()
        self._detail_worker = _DetailWorker(
            host,
            svc.service_name,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
            fallback=svc,
        )
        self._detail_worker.log_line.connect(self._append_psservice_log)
        self._detail_worker.finished_ok.connect(self._on_detail_ok)
        self._detail_worker.finished_err.connect(self._on_detail_err)
        self._detail_worker.finished.connect(self._refresh_action_buttons)
        self._status_lbl.setText(
            self.tr(f"Consultando detalhes de {svc.service_name}...")
        )
        self._detail_worker.start()
        self._refresh_action_buttons()

    def _on_detail_ok(self, host: str, service_name: str, payload: object) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        if isinstance(payload, RemoteService):
            _show_service_details(self, payload)
            # Atualiza cache local com dados mais ricos
            key = service_name.casefold()
            if key in self._by_name:
                self._by_name[key] = payload
                for i, row in enumerate(self._services):
                    if (row.service_name or "").casefold() == key:
                        self._services[i] = payload
                        break
        self._restore_status_label()

    def _on_detail_err(self, host: str, service_name: str, message: str) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        safe = redact_command_text(message or "", passwords=None)
        self.log_output.append_log(
            self.tr(f"[SERVIÇOS] Detalhes {service_name}: {safe}")
        )
        self._status_lbl.setText(self.tr("Falha ao obter detalhes"))

    def _restore_status_label(self) -> None:
        if self._data_host:
            self._status_lbl.setText(
                self.tr(
                    f"{len(self._services)} serviço(s) em {self._data_host}"
                )
            )
        else:
            self._status_lbl.setText("")

    def _request_dependents(self) -> None:
        svc = self._selected_service()
        if svc is None:
            return
        self._start_depend_worker(svc.service_name, purpose="view")

    def _confirm_stop_or_restart(self, action: str) -> None:
        svc = self._selected_service()
        if svc is None:
            return
        self._pending_action = action
        self._start_depend_worker(svc.service_name, purpose=action)

    def _start_depend_worker(self, service_name: str, *, purpose: str) -> None:
        if not self._ui_alive():
            return
        if self._depend_worker is not None and self._depend_worker.isRunning():
            return
        host = self._get_host()
        user, password = self._creds()
        self._depend_worker = _DependWorker(
            host,
            service_name,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
            purpose=purpose,
        )
        self._depend_worker.log_line.connect(self._append_psservice_log)
        self._depend_worker.finished_ok.connect(self._on_depend_ok)
        self._depend_worker.finished_err.connect(self._on_depend_err)
        self._depend_worker.finished.connect(self._refresh_action_buttons)
        self._status_lbl.setText(
            self.tr(f"Consultando dependentes de {service_name}...")
        )
        self.log_output.append_log(
            self.tr(
                f"[SERVIÇOS] Consultando dependentes de {service_name} @ {host}..."
            )
        )
        self._depend_worker.start()
        self._refresh_action_buttons()

    def _on_depend_ok(
        self,
        host: str,
        service_name: str,
        deps: object,
        empty_confirmed: bool,
    ) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        dep_list = list(deps or [])
        purpose = ""
        if self._depend_worker is not None:
            purpose = getattr(self._depend_worker, "purpose", "view") or "view"

        if purpose == "view":
            _show_dependents_dialog(self, service_name, dep_list)
            self._restore_status_label()
            return

        # Confirmação para stop/restart
        action = purpose
        self._pending_action = None
        svc = self._by_name.get(service_name.casefold())
        display = (
            svc.display_name
            if svc and svc.display_name
            else service_name
        )
        if dep_list:
            lines = [
                f"• {d.display_name or d.service_name} ({d.state_label})"
                for d in dep_list[:30]
            ]
            more = ""
            if len(dep_list) > 30:
                more = self.tr(f"\n… e mais {len(dep_list) - 30}.")
            body = self.tr(
                f"O serviço '{display}' possui {len(dep_list)} dependente(s):\n\n"
                + "\n".join(lines)
                + more
                + "\n\nDeseja continuar mesmo assim?"
            )
        elif empty_confirmed:
            body = self.tr(
                f"Nenhum serviço dependente de '{display}'.\n\n"
                f"Confirmar ação '{action}'?"
            )
        else:
            body = self.tr(
                f"Não foi possível listar dependentes de forma conclusiva.\n\n"
                f"Confirmar ação '{action}' em '{display}'?"
            )

        title = (
            self.tr("Parar serviço?")
            if action == "stop"
            else self.tr("Reiniciar serviço?")
        )
        reply = QMessageBox.question(
            self,
            title,
            f"{display}\nHost: {host}\n\n{body}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        self._restore_status_label()
        if reply == QMessageBox.StandardButton.Yes:
            self._run_action(action)

    def _on_depend_err(self, host: str, service_name: str, message: str) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        safe = redact_command_text(message or "", passwords=None)
        self.log_output.append_log(
            self.tr(f"[SERVIÇOS] Dependentes {service_name}: {safe}")
        )
        purpose = ""
        if self._depend_worker is not None:
            purpose = getattr(self._depend_worker, "purpose", "view") or "view"
        self._pending_action = None

        if purpose in {"stop", "restart"}:
            reply = QMessageBox.question(
                self,
                self.tr("Falha ao consultar dependentes"),
                self.tr(
                    f"Não foi possível listar os dependentes de '{service_name}'.\n"
                    f"{safe}\n\nDeseja continuar com a ação '{purpose}' mesmo assim?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            self._restore_status_label()
            if reply == QMessageBox.StandardButton.Yes:
                self._run_action(purpose)
            return

        self._status_lbl.setText(self.tr("Falha ao listar dependentes"))
        QMessageBox.warning(
            self,
            self.tr("Dependentes"),
            self.tr(
                f"Falha ao consultar dependentes de '{service_name}'.\n{safe}"
            ),
        )
        self._restore_status_label()

    def _request_security(self) -> None:
        svc = self._selected_service()
        if svc is None or not self._ui_alive():
            return
        if self._security_worker is not None and self._security_worker.isRunning():
            return
        host = self._get_host()
        user, password = self._creds()
        self._security_worker = _SecurityWorker(
            host,
            svc.service_name,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._security_worker.log_line.connect(self._append_psservice_log)
        self._security_worker.finished_ok.connect(self._on_security_ok)
        self._security_worker.finished_err.connect(self._on_security_err)
        self._security_worker.finished.connect(self._refresh_action_buttons)
        self._status_lbl.setText(
            self.tr(f"Consultando segurança de {svc.service_name}...")
        )
        self._security_worker.start()
        self._refresh_action_buttons()

    def _on_security_ok(
        self, host: str, service_name: str, payload: object
    ) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        if isinstance(payload, ServiceSecurityInfo):
            _show_security_dialog(self, payload)
        self._restore_status_label()

    def _on_security_err(
        self, host: str, service_name: str, message: str
    ) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        safe = redact_command_text(message or "", passwords=None)
        self.log_output.append_log(
            self.tr(f"[SERVIÇOS] Segurança {service_name}: {safe}")
        )
        self._status_lbl.setText(self.tr("Falha ao obter segurança"))

    def _open_find_dialog(self) -> None:
        svc = self._selected_service()
        initial = svc.service_name if svc else ""
        dlg = _FindDialog(self, initial_name=initial)
        dlg.exec()

    def _change_start_type(self) -> None:
        svc = self._selected_service()
        if svc is None or not self._ui_alive():
            return
        labels = [SETCONFIG_TO_LABEL[t] for t in SUPPORTED_SETCONFIG]
        current = svc.setconfig_token or SETCONFIG_DEMAND
        try:
            current_idx = list(SUPPORTED_SETCONFIG).index(current)
        except ValueError:
            current_idx = 1
        choice, ok = QInputDialog.getItem(
            self,
            self.tr("Alterar inicialização"),
            self.tr(
                f"Tipo de inicialização para '{svc.display_name or svc.service_name}':"
            ),
            labels,
            current_idx,
            False,
        )
        if not ok or not choice:
            return
        token = None
        for t in SUPPORTED_SETCONFIG:
            if SETCONFIG_TO_LABEL[t] == choice:
                token = t
                break
        if token is None or token not in SUPPORTED_SETCONFIG:
            return
        reply = QMessageBox.question(
            self,
            self.tr("Confirmar alteração"),
            self.tr(
                f"Alterar a inicialização de "
                f"'{svc.display_name or svc.service_name}' para '{choice}'?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._run_action("setconfig", setconfig_token=token)

    def _run_action(
        self, action: str, *, setconfig_token: str = ""
    ) -> None:
        svc = self._selected_service()
        if svc is None or not self._ui_alive():
            return
        if self._action_worker is not None and self._action_worker.isRunning():
            return
        host = self._get_host()
        user, password = self._creds()
        self._action_worker = _ActionWorker(
            action,
            host,
            svc.service_name,
            setconfig_token=setconfig_token,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        self._action_worker.log_line.connect(self._append_psservice_log)
        self._action_worker.finished_ok.connect(self._on_action_ok)
        self._action_worker.finished_err.connect(self._on_action_err)
        self._action_worker.finished.connect(self._refresh_action_buttons)
        labels = {
            "start": "iniciar",
            "stop": "parar",
            "restart": "reiniciar",
            "pause": "pausar",
            "cont": "continuar",
            "setconfig": "alterar inicialização",
        }
        extra = ""
        if action == "setconfig" and setconfig_token:
            extra = f" → {SETCONFIG_TO_LABEL.get(setconfig_token, setconfig_token)}"
        self.log_output.append_log(
            self.tr(
                f"[SERVIÇOS] Ação '{labels.get(action, action)}'{extra} em "
                f"{svc.service_name} @ {host}..."
            )
        )
        self._status_lbl.setText(
            self.tr(f"Executando '{action}' em {svc.service_name}...")
        )
        self._action_worker.start()
        self._refresh_action_buttons()

    def _on_action_ok(
        self, host: str, service_name: str, action: str, payload: object
    ) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        self.log_output.append_log(
            self.tr(
                f"[SERVIÇOS] Ação '{action}' concluída em {service_name}."
            )
        )
        if isinstance(payload, RemoteService) and payload.service_name:
            key = service_name.casefold()
            existing = self._by_name.get(key)
            if existing is not None:
                updated = replace(
                    existing,
                    state=payload.state or existing.state,
                    state_code=(
                        payload.state_code
                        if payload.state_code is not None
                        else existing.state_code
                    ),
                    controls_accepted=(
                        payload.controls_accepted or existing.controls_accepted
                    ),
                    start_type=payload.start_type or existing.start_type,
                    start_type_code=(
                        payload.start_type_code
                        if payload.start_type_code is not None
                        else existing.start_type_code
                    ),
                    account=payload.account or existing.account,
                )
                if action == "setconfig" and self._action_worker is not None:
                    token = getattr(self._action_worker, "setconfig_token", "")
                    if token == SETCONFIG_AUTO:
                        updated = replace(updated, start_type="AUTO_START")
                    elif token == SETCONFIG_DEMAND:
                        updated = replace(updated, start_type="DEMAND_START")
                    elif token == SETCONFIG_DISABLED:
                        updated = replace(updated, start_type="DISABLED")
                self._by_name[key] = updated
                for i, row in enumerate(self._services):
                    if (row.service_name or "").casefold() == key:
                        self._services[i] = updated
                        break
                self._populate_table()
                self._apply_filter()
                self._select_service(service_name)
            else:
                self.refresh_services(full=True)
        else:
            self.refresh_services(full=action == "setconfig")
        self._restore_status_label()

    def _on_action_err(
        self, host: str, service_name: str, action: str, message: str
    ) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        safe = redact_command_text(message or "", passwords=None)
        self.log_output.append_log(
            self.tr(
                f"[SERVIÇOS] Falha ({action}) {service_name}: {safe}"
            )
        )
        self._status_lbl.setText(self.tr("Falha na ação"))
        # Atualiza estado mesmo após falha parcial
        self.refresh_services(full=False)
