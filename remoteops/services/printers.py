"""Instalação remota silenciosa de impressoras de rede.

A listagem consulta o servidor configurado em Configurações a partir desta
máquina (PowerShell local / ``Get-Printer -ComputerName``), sem PsExec.

Driver, ``/ga`` e a tarefa do usuário rodam no host remoto via PsExec.
A conexão ``/in`` do usuário NÃO usa ``PsExec -i``: a identidade vem de uma
tarefa temporária com LogonType InteractiveToken.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from remoteops.core.process_runner import run_argv_captured
from remoteops.services.ops import CredentialContext, build_psexec_argv, resolve_psexec_exe
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.printer_settings import get_print_list_timeout, require_print_server
from remoteops.utils.printers import (
    SCOPE_ALL,
    SCOPE_USER,
    NetworkPrinter,
    build_artifact_cleanup_script,
    build_computer_connect_script,
    build_driver_prepare_script,
    build_driver_query_script,
    build_user_task_orchestrator_script,
    build_user_wrapper_script,
    can_proceed_to_connect,
    classify_printer_error,
    decode_console_bytes,
    driver_is_present,
    effective_set_default,
    elevated_psexec_flags,
    encode_powershell,
    is_session_active,
    local_list_printers_argv,
    new_operation_id,
    parse_json_payload,
    payload_ok,
    powershell_encoded_argv,
    print_server_host,
    printer_unc,
    qualify_interactive_user,
    read_printers_catalog_file,
    reject_psexec_interactive_identity,
    result_file_name,
    should_connect_active_user,
    should_prepare_driver,
)
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import redact_command_text
from remoteops.utils.sessions import RemoteSession

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]

DRIVER_TIMEOUT_S = 60
PREPARE_TIMEOUT_S = 120
CONNECT_TIMEOUT_S = 90
USER_TASK_TIMEOUT_S = 90
CLEANUP_TIMEOUT_S = 30


@dataclass
class CommandCapture:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 1
    cancelled: bool = False
    timed_out: bool = False
    payload: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class InstallPrinterRequest:
    host: str
    printer: NetworkPrinter
    scope: str
    session: Optional[RemoteSession] = None
    set_default: bool = False
    operation_id: str = ""
    domain_hint: str = ""


@dataclass
class InstallPrinterResult:
    ok: bool
    message: str
    host: str = ""
    unc: str = ""
    scope: str = ""
    driver_prepared: bool = False
    computer_connected: bool = False
    user_connected: bool = False
    operation_id: str = ""
    cancelled: bool = False


class PrinterService:
    """Regras de negócio da aba Impressoras — sem widgets Qt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass

    def list_server_printers(
        self,
        server: str = "",
        *,
        timeout_s: Optional[int] = None,
        should_cancel: Optional[CancelFn] = None,
    ) -> Tuple[List[NetworkPrinter], str]:
        """Lista impressoras via PowerShell local — não usa PsExec.

        O catálogo é gravado num JSON temporário e lido depois; o arquivo
        é sempre removido. Isso evita saturar o pipe e truncar a lista.
        Sem servidor nas configurações, devolve erro e não consulta.
        """
        try:
            host = print_server_host(server) or require_print_server()
        except ValueError as extra:
            return [], str(extra)
        limit = get_print_list_timeout() if timeout_s is None else int(timeout_s)
        handle, catalog_path = tempfile.mkstemp(
            prefix="RemoteOps_Printers_", suffix=".json"
        )
        os.close(handle)
        try:
            try:
                argv = local_list_printers_argv(host, catalog_path)
            except ValueError as exc:
                return [], str(exc)
            capture = self._run_argv(
                argv,
                timeout_s=limit,
                should_cancel=should_cancel,
                passwords=None,
            )
            if capture.cancelled:
                return [], "Consulta cancelada."
            combined = f"{capture.stdout}\n{capture.stderr}"
            if capture.timed_out:
                return [], classify_printer_error(
                    combined or "timeout consultando o servidor de impressão",
                    capture.exit_code,
                    server=host,
                )
            if capture.exit_code != 0:
                return [], classify_printer_error(
                    combined, capture.exit_code, server=host
                )
            try:
                if not os.path.isfile(catalog_path) or os.path.getsize(catalog_path) == 0:
                    return [], "Get-Printer não gravou o arquivo temporário."
                printers = read_printers_catalog_file(catalog_path)
            except OSError as extra:
                return [], str(extra) or "Falha ao ler o JSON temporário das impressoras."
            except Exception as extra:
                return [], str(extra) or "Falha ao interpretar a lista de impressoras."
            return printers, ""
        finally:
            try:
                os.remove(catalog_path)
            except OSError:
                pass

    def check_driver(
        self,
        host: str,
        driver_name: str,
        creds: CredentialContext,
        *,
        timeout_s: int = DRIVER_TIMEOUT_S,
        should_cancel: Optional[CancelFn] = None,
        as_system: bool = True,
    ) -> CommandCapture:
        script = build_driver_query_script(driver_name)
        capture = self.run_remote_script(
            host,
            script,
            creds,
            timeout_s=timeout_s,
            should_cancel=should_cancel,
            as_system=as_system,
        )
        capture.payload = parse_json_payload(capture.stdout)
        if capture.ok and not capture.payload:
            capture.ok = False
            capture.error = "Não foi possível verificar o driver no host remoto."
        return capture

    def prepare_driver(
        self,
        host: str,
        *,
        unc: str,
        driver_name: str,
        creds: CredentialContext,
        timeout_s: int = PREPARE_TIMEOUT_S,
        should_cancel: Optional[CancelFn] = None,
    ) -> CommandCapture:
        """Staging elevado silencioso (``/in`` temporário + ``/dn``). Sem alterar políticas."""
        script = build_driver_prepare_script(unc=unc, driver_name=driver_name)
        capture = self.run_remote_script(
            host,
            script,
            creds,
            timeout_s=timeout_s,
            should_cancel=should_cancel,
            as_system=True,
        )
        capture.payload = parse_json_payload(capture.stdout)
        if self._is_print_server_access_denied(capture) and (creds.user or "").strip():
            retry = self.run_remote_script(
                host,
                script,
                creds,
                timeout_s=timeout_s,
                should_cancel=should_cancel,
                as_system=False,
            )
            retry.payload = parse_json_payload(retry.stdout)
            capture = retry
        if capture.cancelled or capture.timed_out:
            return capture
        present = driver_is_present(capture.payload)
        capture.ok = bool(present)
        if not capture.ok and not capture.error:
            capture.error = self._driver_prepare_error(capture)
        return capture

    def connect_computer(
        self,
        host: str,
        unc: str,
        creds: CredentialContext,
        *,
        timeout_s: int = CONNECT_TIMEOUT_S,
        should_cancel: Optional[CancelFn] = None,
    ) -> CommandCapture:
        script = build_computer_connect_script(unc)
        capture = self.run_remote_script(
            host,
            script,
            creds,
            timeout_s=timeout_s,
            should_cancel=should_cancel,
            as_system=True,
        )
        capture.payload = parse_json_payload(capture.stdout)
        if capture.ok and capture.payload and not payload_ok(capture.payload):
            capture.ok = False
            capture.error = "PrintUIEntry retornou erro."
        if not capture.ok and not capture.error:
            capture.error = classify_printer_error(
                f"{capture.stdout}\n{capture.stderr}", capture.exit_code
            )
        return capture

    def connect_user(
        self,
        host: str,
        *,
        unc: str,
        username: str,
        creds: CredentialContext,
        set_default: bool = False,
        operation_id: str = "",
        timeout_s: int = USER_TASK_TIMEOUT_S,
        should_cancel: Optional[CancelFn] = None,
    ) -> CommandCapture:
        user = (username or "").strip()
        if not user:
            return CommandCapture(
                ok=False, error="Usuário não possui sessão interativa."
            )
        op = operation_id or new_operation_id()
        wrapper = build_user_wrapper_script(
            unc=unc,
            result_filename=result_file_name(op),
            set_default=bool(set_default),
        )
        orchestrator = build_user_task_orchestrator_script(
            username=user,
            operation_id=op,
            wrapper_encoded=encode_powershell(wrapper),
            timeout_s=timeout_s,
        )
        capture = self.run_remote_script(
            host,
            orchestrator,
            creds,
            timeout_s=timeout_s + 30,
            should_cancel=should_cancel,
            as_system=True,
        )
        capture.payload = parse_json_payload(capture.stdout)
        if capture.cancelled or capture.timed_out:
            if capture.timed_out and not capture.error:
                capture.error = "Timeout aguardando tarefa do usuário."
            return capture
        if capture.payload.get("error") == "timeout" or capture.exit_code == 2:
            capture.ok = False
            capture.error = "Timeout aguardando tarefa do usuário."
            return capture
        capture.ok = payload_ok(capture.payload)
        if not capture.ok and not capture.error:
            err = str(capture.payload.get("error") or "").strip()
            capture.error = err or classify_printer_error(
                f"{capture.stdout}\n{capture.stderr}", capture.exit_code
            )
            if not capture.error or capture.error == "Falha na operação de impressora.":
                capture.error = "PrintUIEntry retornou erro."
        return capture

    def cleanup_operation(
        self,
        host: str,
        operation_id: str,
        creds: Optional[CredentialContext] = None,
    ) -> None:
        op = (operation_id or "").strip()
        if not op or not (host or "").strip():
            return
        used = creds if creds is not None else CredentialContext()
        try:
            self.run_remote_script(
                host,
                build_artifact_cleanup_script(op),
                used,
                timeout_s=CLEANUP_TIMEOUT_S,
                as_system=True,
            )
        except Exception:
            pass

    def install(
        self,
        request: InstallPrinterRequest,
        creds: CredentialContext,
        *,
        progress: Optional[ProgressFn] = None,
        should_cancel: Optional[CancelFn] = None,
        passwords: Optional[Sequence[str]] = None,
    ) -> InstallPrinterResult:
        emit = progress or (lambda _m: None)
        secrets = list(passwords or [])
        if creds.password.strip() and creds.password not in secrets:
            secrets.append(creds.password)

        def log(message: str) -> None:
            emit(redact_command_text(message, passwords=secrets))

        host = normalize_host(request.host)
        op_id = request.operation_id or new_operation_id()
        result = InstallPrinterResult(
            ok=False,
            message="",
            host=host,
            scope=(request.scope or "").strip().lower(),
            operation_id=op_id,
        )
        remote_started = False
        try:
            if not host or not is_valid_host(host):
                result.message = "Host inválido."
                return result
            printer = request.printer
            if printer is None or not printer.is_shared:
                result.message = "Não compartilhada"
                return result
            try:
                unc = printer_unc(printer.share_name, require_print_server())
            except ValueError as exc:
                result.message = str(exc)
                return result
            result.unc = unc
            driver = (printer.driver_name or "").strip()
            if not driver:
                result.message = "DriverName vazio na impressora do servidor."
                return result

            scope = result.scope
            if scope not in {SCOPE_USER, SCOPE_ALL}:
                result.message = "Escopo inválido."
                return result
            if scope == SCOPE_USER and not is_session_active(request.session):
                result.message = "Usuário não possui sessão interativa."
                return result

            if self._cancelled(should_cancel):
                result.cancelled = True
                result.message = "Operação cancelada."
                return result

            remote_started = True
            log(f'Verificando driver "{driver}"...')
            check = self.check_driver(
                host, driver, creds, should_cancel=should_cancel
            )
            if self._cancelled(should_cancel) or check.cancelled:
                result.cancelled = True
                result.message = "Operação cancelada."
                return result
            present = driver_is_present(check.payload)
            if not present and check.ok is False and not check.payload:
                result.message = check.error or "Não foi possível verificar o driver no host remoto."
                return result

            if should_prepare_driver(present):
                log("Driver não encontrado. Preparando silenciosamente...")
                prep = self.prepare_driver(
                    host,
                    unc=unc,
                    driver_name=driver,
                    creds=creds,
                    should_cancel=should_cancel,
                )
                if self._cancelled(should_cancel) or prep.cancelled:
                    result.cancelled = True
                    result.message = "Operação cancelada."
                    return result
                if not prep.ok:
                    result.message = prep.error or "Driver não pôde ser preparado."
                    return result
                present = True
                result.driver_prepared = True
                log("Driver instalado com sucesso.")
            else:
                log("Driver disponível.")

            if not can_proceed_to_connect(driver_ready=present):
                result.message = "Driver não pôde ser preparado."
                return result

            user = ""
            if request.session is not None:
                user = qualify_interactive_user(
                    request.session.username, request.domain_hint
                )

            if scope == SCOPE_USER:
                log(f"Criando conexão {unc}...")
                log(f"Executando no contexto de {user}...")
                user_cap = self.connect_user(
                    host,
                    unc=unc,
                    username=user,
                    creds=creds,
                    set_default=effective_set_default(scope, request.set_default),
                    operation_id=op_id,
                    should_cancel=should_cancel,
                )
                if self._cancelled(should_cancel) or user_cap.cancelled:
                    result.cancelled = True
                    result.message = "Operação cancelada."
                    return result
                if not user_cap.ok:
                    result.message = user_cap.error or "PrintUIEntry retornou erro."
                    return result
                result.user_connected = True
                result.ok = True
                result.message = f"Impressora instalada com sucesso para {user}."
                return result

            log("Criando conexão por computador...")
            ga = self.connect_computer(
                host, unc, creds, should_cancel=should_cancel
            )
            if self._cancelled(should_cancel) or ga.cancelled:
                result.cancelled = True
                result.message = "Operação cancelada."
                return result
            if not ga.ok:
                result.message = ga.error or "PrintUIEntry retornou erro."
                return result
            result.computer_connected = True
            log("Conexão /ga criada.")

            if should_connect_active_user(scope, is_session_active(request.session)):
                log("Aplicando também à sessão ativa...")
                user_cap = self.connect_user(
                    host,
                    unc=unc,
                    username=user,
                    creds=creds,
                    set_default=False,
                    operation_id=op_id,
                    should_cancel=should_cancel,
                )
                if self._cancelled(should_cancel) or user_cap.cancelled:
                    result.cancelled = True
                    result.message = "Operação cancelada."
                    return result
                if not user_cap.ok:
                    result.message = (
                        "Conexão /ga criada, mas falhou ao aplicar à sessão ativa: "
                        + (user_cap.error or "PrintUIEntry retornou erro.")
                    )
                    return result
                result.user_connected = True

            result.ok = True
            result.message = "Instalação concluída."
            return result
        finally:
            if remote_started:
                self.cleanup_operation(host, op_id, creds)

    def run_remote_script(
        self,
        host: str,
        script: str,
        creds: CredentialContext,
        *,
        timeout_s: int,
        should_cancel: Optional[CancelFn] = None,
        as_system: bool = True,
        pstools_path: str = "",
    ) -> CommandCapture:
        host = normalize_host(host)
        if not host or not is_valid_host(host):
            return CommandCapture(ok=False, error="Host inválido.")
        flags = reject_psexec_interactive_identity(
            elevated_psexec_flags(as_system=as_system)
        )
        encoded = encode_powershell(script)
        remote_argv = powershell_encoded_argv(encoded)
        used = creds if creds is not None else CredentialContext()
        argv = build_psexec_argv(
            psexec_exe=resolve_psexec_exe(pstools_path or get_pstools_dir()),
            host=host,
            remote_argv=remote_argv,
            creds=used,
            extra_flags=flags,
            include_password=True,
        )
        return self._run_argv(
            argv,
            timeout_s=timeout_s,
            should_cancel=should_cancel,
            passwords=used.passwords,
        )

    def _run_argv(
        self,
        argv: Sequence[str],
        *,
        timeout_s: int,
        should_cancel: Optional[CancelFn] = None,
        passwords: Optional[Sequence[str]] = None,
    ) -> CommandCapture:
        secrets = list(passwords or [])

        def _started(proc: subprocess.Popen) -> None:
            with self._lock:
                self._proc = proc

        def _finished() -> None:
            with self._lock:
                self._proc = None

        captured = run_argv_captured(
            argv,
            timeout_s=max(5, int(timeout_s)),
            should_cancel=lambda: self._cancelled(should_cancel),
            on_started=_started,
            on_finished=_finished,
        )
        if captured.spawn_error:
            return CommandCapture(ok=False, error=captured.spawn_error)

        stdout = decode_console_bytes(captured.stdout or b"")
        stderr = decode_console_bytes(captured.stderr or b"")
        stdout = redact_command_text(stdout, passwords=secrets)
        stderr = redact_command_text(stderr, passwords=secrets)
        code = int(captured.returncode)
        if captured.cancelled:
            return CommandCapture(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                exit_code=code,
                cancelled=True,
                error="Operação cancelada.",
            )
        if captured.timed_out:
            return CommandCapture(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                exit_code=code,
                timed_out=True,
                error="Timeout aguardando tarefa do usuário."
                if "task" in " ".join(str(a).lower() for a in argv)
                else "Timeout na operação de impressora.",
            )
        ok = code == 0
        error = ""
        if not ok:
            error = classify_printer_error(f"{stdout}\n{stderr}", code)
        return CommandCapture(
            ok=ok,
            stdout=stdout,
            stderr=stderr,
            exit_code=code,
            error=error,
        )

    @staticmethod
    def _cancelled(should_cancel: Optional[CancelFn]) -> bool:
        if should_cancel is None:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    @staticmethod
    def _is_print_server_access_denied(capture: CommandCapture) -> bool:
        text = f"{capture.error}\n{capture.stdout}\n{capture.stderr}".casefold()
        return "acesso negado" in text or "access is denied" in text

    @staticmethod
    def _driver_prepare_error(capture: CommandCapture) -> str:
        classified = classify_printer_error(
            f"{capture.stdout}\n{capture.stderr}\n{capture.error}",
            capture.exit_code,
        )
        if classified and classified != "Falha na operação de impressora.":
            return classified
        return "Driver não pôde ser preparado."
