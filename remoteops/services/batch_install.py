"""Instalação remota em lote: decisão de versão + execução via CommandBuilder/PsExec."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from remoteops.core.builder import CommandBuilder
from remoteops.core.executor import decode_best_effort
from remoteops.core.models import CommandSpec
from remoteops.core.win_cmd import CREATE_NO_WINDOW, popen_argv
from remoteops.services.ops import materialize_password_in_argv
from remoteops.utils.product_identity import (
    ProductIdentity,
    compare_versions,
    format_app_found,
    format_app_version,
    match_identity_app,
    valid_numeric_version,
)
from remoteops.utils.psinfo import HostInventoryStatus
from remoteops.utils.redaction import redact_command_text

ACTION_INSTALL = "Instalar"
ACTION_UPDATE = "Atualizar"
ACTION_SKIP = "Não instalar"

RESULT_INSTALLED = "Instalado"
RESULT_UPDATED = "Atualizado"
RESULT_SKIPPED = "Não instalado"
RESULT_ERROR = "Erro"
RESULT_UPDATING = "Atualizando"
RESULT_PENDING = ""
REASON_IN_PROGRESS = "Instalação em andamento"

REASON_NOT_INSTALLED = "Aplicativo não estava instalado"
REASON_OLD_VERSION = "Versão antiga"
REASON_ALREADY_CURRENT = "Versão já atual"
REASON_NEWER_INSTALLED = "Versão instalada superior"
REASON_OFFLINE = "Computador offline"
REASON_DETECT_FAILED = "Falha ao detectar aplicativo/versão"
REASON_INSTALLER_VERSION_UNKNOWN = "Não foi possível identificar a versão do instalador"
REASON_INSTALL_FAILED = "Falha na instalação"
REASON_CANCELLED = "Operação interrompida"
REASON_PSEXEC_FAILED = "Falha ao executar o instalador (PsExec)"

# 0 = sucesso; 1641 = reboot iniciado; 3010 = reboot necessário.
INSTALLER_SUCCESS_CODES = frozenset({0, 1641, 3010})

LogFn = Callable[[str], None]


@dataclass
class BatchHostRow:
    host: str
    app_found: str = "—"
    version: str = "—"
    desired: str = "—"
    action: str = ""
    result: str = ""
    reason: str = ""
    needs_install: bool = False
    is_update: bool = False
    online: bool = True
    detection_ok: bool = False
    order: int = 0

    def as_tuple(self) -> Tuple[str, ...]:
        return (
            self.host,
            self.app_found,
            self.version,
            self.desired or "—",
            self.action,
            self.result,
            self.reason,
        )


@dataclass
class BatchSummary:
    installed: int = 0
    updated: int = 0
    skipped: int = 0
    offline: int = 0
    errors: int = 0

    def add(self, row: BatchHostRow) -> None:
        if row.result in (RESULT_UPDATING, RESULT_PENDING, ""):
            return
        if row.result == RESULT_INSTALLED:
            self.installed += 1
        elif row.result == RESULT_UPDATED:
            self.updated += 1
        elif not row.online or row.reason == REASON_OFFLINE:
            self.offline += 1
        elif row.result == RESULT_ERROR:
            self.errors += 1
        else:
            self.skipped += 1

    def as_text(self) -> str:
        return (
            f"Instalados: {self.installed}  |  Atualizados: {self.updated}  |  "
            f"Ignorados: {self.skipped}  |  Offline: {self.offline}  |  "
            f"Erros: {self.errors}"
        )


@dataclass
class RemoteInstallOutcome:
    ok: bool
    return_code: Optional[int] = None
    cancelled: bool = False
    psexec_ok: bool = False
    installer_ok: bool = False
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    display_command: str = ""


def desired_display(desired_version: str) -> str:
    text = (desired_version or "").strip()
    return text if text else "—"


def resolve_target_version(desired_version: str, identity: ProductIdentity) -> str:
    """Versão a instalar: campo da UI, ou ProductVersion/FileVersion do EXE."""
    typed = valid_numeric_version(desired_version)
    if typed:
        return typed
    if (desired_version or "").strip():
        return (desired_version or "").strip()
    return valid_numeric_version(getattr(identity, "installer_version", "") or "")


def enrich_inventory(
    rr_status: HostInventoryStatus,
    identity: ProductIdentity,
    *,
    win32_query: Optional[Callable[[str], HostInventoryStatus]] = None,
) -> HostInventoryStatus:
    """
    Remote Registry é a fonte principal.

    Win32_Product (PowerShell/WMI) **não** é tentado quando o RR falhou:
    host inacessível, RPC/auth/timeout. Esse fallback disparava o EDR
    (powershell.exe) em cada falha de detecção.

    Só consulta WMI se o RR conectou e devolveu inventário vazio — o host
    já está alcançável e o Uninstall veio vazio.
    """
    if not rr_status.ok:
        return rr_status
    if match_identity_app(rr_status.apps or [], identity) is not None:
        return rr_status
    if rr_status.apps:
        return rr_status
    if win32_query is None:
        return rr_status
    wmi_status = win32_query(rr_status.host)
    if wmi_status.ok:
        return wmi_status
    return rr_status


def decide_host_action(
    *,
    host: str,
    desired_version: str,
    online: bool,
    inventory: Optional[HostInventoryStatus],
    identity: ProductIdentity,
) -> BatchHostRow:
    """
    Decide ação/resultado a partir da conectividade e do inventário.

    Falha na consulta não é tratada como aplicativo ausente.
    Nunca faz downgrade. EXE sem versão e app já instalado → erro.
    """
    target = resolve_target_version(desired_version, identity)
    row = BatchHostRow(host=host, desired=desired_display(target))
    if not online:
        row.online = False
        row.action = ACTION_SKIP
        row.result = RESULT_SKIPPED
        row.reason = REASON_OFFLINE
        return row

    if inventory is None or not inventory.ok:
        row.action = ACTION_SKIP
        row.result = RESULT_ERROR
        row.reason = REASON_DETECT_FAILED
        return row

    row.detection_ok = True
    app = match_identity_app(inventory.apps or [], identity)
    row.app_found = format_app_found(app)
    row.version = format_app_version(app)

    if app is None:
        row.action = ACTION_INSTALL
        row.needs_install = True
        row.reason = REASON_NOT_INSTALLED
        return row

    if not target:
        row.action = ACTION_SKIP
        row.result = RESULT_ERROR
        row.reason = REASON_INSTALLER_VERSION_UNKNOWN
        return row

    cmp = compare_versions(app.version or "", target)
    if cmp is None:
        row.action = ACTION_SKIP
        row.result = RESULT_ERROR
        row.reason = REASON_DETECT_FAILED
        return row
    if cmp < 0:
        row.action = ACTION_UPDATE
        row.needs_install = True
        row.is_update = True
        row.reason = REASON_OLD_VERSION
        return row
    if cmp == 0:
        row.action = ACTION_SKIP
        row.result = RESULT_SKIPPED
        row.reason = REASON_ALREADY_CURRENT
        return row
    row.action = ACTION_SKIP
    row.result = RESULT_SKIPPED
    row.reason = REASON_NEWER_INSTALLED
    return row


def apply_install_outcome(row: BatchHostRow, outcome: RemoteInstallOutcome) -> BatchHostRow:
    if outcome.cancelled:
        row.result = RESULT_ERROR
        row.reason = REASON_CANCELLED
        row.needs_install = False
        return row
    if outcome.ok and outcome.installer_ok:
        if row.is_update:
            row.result = RESULT_UPDATED
            row.reason = REASON_OLD_VERSION
        elif row.app_found in ("", "—"):
            row.result = RESULT_INSTALLED
            row.reason = REASON_NOT_INSTALLED
        elif row.action == ACTION_UPDATE:
            row.result = RESULT_UPDATED
            row.reason = REASON_OLD_VERSION
        else:
            row.result = RESULT_INSTALLED
            if row.reason != REASON_NOT_INSTALLED:
                row.reason = REASON_NOT_INSTALLED if row.app_found in ("", "—") else row.reason
        row.needs_install = False
        return row
    row.result = RESULT_ERROR
    row.reason = REASON_INSTALL_FAILED
    if outcome.message and "psexec" in outcome.message.casefold():
        row.reason = REASON_PSEXEC_FAILED
    row.needs_install = False
    return row


def prepare_batch_psexec_params(
    base_params: Optional[dict],
    *,
    host: str,
    extra_args: str,
    pstools_path: str,
) -> dict:
    """Reusa flags da aba PsExec; força cópia, espera, SYSTEM e extra args da Lote.

    Instaladores rodam como SYSTEM (``-s``). ``-h`` e ``-l`` são desligados
    porque conflitam com ``-s``.
    """
    params = dict(base_params or {})
    params["host"] = (host or "").strip().strip("\\")
    params["psexec_path"] = pstools_path
    params["extra_args"] = extra_args or ""
    params["remote_cmd"] = ""
    params["-c"] = True
    params["-f"] = True
    params["-v"] = False
    params["-d"] = False
    params["session_interactive"] = False
    params["session_id"] = None
    params["copy_allowed"] = True
    params["-accepteula"] = True
    params["-s"] = True
    params["-h"] = False
    params["-l"] = False
    return params


def build_batch_install_spec(
    *,
    host: str,
    exe_path: str,
    extra_args: str,
    psexec_params: Optional[dict],
    pstools_path: str,
    has_password: bool,
) -> CommandSpec:
    builder = CommandBuilder()
    builder.set_file_selection({"mode": "file", "file": exe_path, "folder": None})
    params = prepare_batch_psexec_params(
        psexec_params,
        host=host,
        extra_args=extra_args,
        pstools_path=pstools_path,
    )
    if has_password and (params.get("user") or "").strip():
        params["has_password"] = True
    builder.set_psexec_params(params)
    return builder.build_psexec_spec()


def run_remote_installer(
    spec: CommandSpec,
    *,
    password: str = "",
    should_cancel: Optional[Callable[[], bool]] = None,
    on_line: Optional[LogFn] = None,
) -> RemoteInstallOutcome:
    """
    Executa o spec do CommandBuilder e espera o instalador remoto.

    Sucesso do PsExec (conexão) ≠ sucesso da instalação: só ``installer_ok``
    quando o processo remoto termina com código de instalador conhecido.
    """
    errors = (spec.metadata or {}).get("psexec_errors") or []
    display = spec.display_command or ""
    if errors or (display or "").startswith("#"):
        msg = "; ".join(str(e) for e in errors) if errors else display
        return RemoteInstallOutcome(
            ok=False,
            message=msg or "Falha ao montar o comando PsExec.",
            display_command=display,
        )

    argv = materialize_password_in_argv(spec.argv, password)
    passwords = [password] if (password or "").strip() else None
    log = on_line or (lambda _m: None)

    try:
        proc = popen_argv(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return RemoteInstallOutcome(
            ok=False,
            message=f"Executável não encontrado: {argv[0] if argv else 'PsExec'}",
            display_command=display,
        )
    except OSError as exc:
        safe = redact_command_text(str(exc), passwords=passwords)
        return RemoteInstallOutcome(
            ok=False,
            message=f"Falha ao iniciar o PsExec: {safe}",
            display_command=display,
        )

    stdout_acc: List[str] = []
    stderr_acc: List[str] = []

    def _read(pipe, bucket: List[str], prefix: str = "") -> None:
        try:
            for raw in iter(pipe.readline, b""):
                line = decode_best_effort(raw).rstrip("\r\n")
                if not line:
                    continue
                bucket.append(line)
                log(f"{prefix}{line}" if prefix else line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    t_out = threading.Thread(
        target=_read, args=(proc.stdout, stdout_acc, ""), daemon=True
    )
    t_err = threading.Thread(
        target=_read, args=(proc.stderr, stderr_acc, ""), daemon=True
    )
    t_out.start()
    t_err.start()

    cancelled = False
    while proc.poll() is None:
        if should_cancel and should_cancel():
            cancelled = True
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            break
        time.sleep(0.15)

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    code = proc.returncode
    stdout = "\n".join(stdout_acc)
    stderr = "\n".join(stderr_acc)
    combined = f"{stdout}\n{stderr}".lower()
    psexec_launch_error = _looks_like_psexec_failure(combined, code)

    if cancelled:
        return RemoteInstallOutcome(
            ok=False,
            return_code=code,
            cancelled=True,
            psexec_ok=not psexec_launch_error,
            installer_ok=False,
            message=REASON_CANCELLED,
            stdout=stdout,
            stderr=stderr,
            display_command=display,
        )

    installer_ok = (code in INSTALLER_SUCCESS_CODES) and not psexec_launch_error
    psexec_ok = (not psexec_launch_error) and code is not None
    if installer_ok:
        return RemoteInstallOutcome(
            ok=True,
            return_code=code,
            psexec_ok=True,
            installer_ok=True,
            message="",
            stdout=stdout,
            stderr=stderr,
            display_command=display,
        )
    if psexec_launch_error:
        detail = _first_error_line(stderr_acc or stdout_acc) or f"código {code}"
        return RemoteInstallOutcome(
            ok=False,
            return_code=code,
            psexec_ok=False,
            installer_ok=False,
            message=f"PsExec: {detail}",
            stdout=stdout,
            stderr=stderr,
            display_command=display,
        )
    return RemoteInstallOutcome(
        ok=False,
        return_code=code,
        psexec_ok=psexec_ok,
        installer_ok=False,
        message=f"Instalador retornou código {code}",
        stdout=stdout,
        stderr=stderr,
        display_command=display,
    )


def summarize_rows(rows: Sequence[BatchHostRow]) -> BatchSummary:
    summary = BatchSummary()
    for row in rows:
        summary.add(row)
    return summary


def _looks_like_psexec_failure(text: str, code: Optional[int]) -> bool:
    markers = (
        "couldn't access",
        "could not start",
        "the system cannot find the file specified",
        "access is denied",
        "o sistema não pode encontrar",
        "acesso negado",
        "error connecting",
        "timeout connecting",
        "logon failure",
        "falha de logon",
    )
    if any(m in (text or "") for m in markers):
        return True
    # Códigos típicos do próprio PsExec (não do instalador).
    if code in {1, 2, 5, 53, 67, 1326, 2250} and "error" in (text or ""):
        return True
    return False


def _first_error_line(lines: Sequence[str]) -> str:
    for line in lines:
        t = (line or "").strip()
        if t:
            return t[:240]
    return ""
