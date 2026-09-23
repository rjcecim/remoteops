"""Instalação MSI em lote: Robocopy + PsExec/msiexec reutilizando o CommandBuilder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from remoteops.core.builder import CommandBuilder
from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.models import CommandSpec, is_robocopy_success
from remoteops.core.process_runner import run_argv_captured
from remoteops.services.batch_install import (
    REASON_CANCELLED,
    REASON_INSTALL_FAILED,
    REASON_OFFLINE,
    RESULT_ERROR,
    BatchHostRow,
    LogFn,
    RemoteInstallOutcome,
    apply_install_outcome,
    prepare_batch_psexec_params,
    run_remote_installer,
)
from remoteops.services.msi_validate import (
    collect_validation_errors,
)
from remoteops.services.ops import materialize_password_in_argv
from remoteops.utils.app_logging import log_operation
from remoteops.utils.redaction import redact_command_text

STATUS_WAITING = "aguardando"
STATUS_COPYING = "copiando arquivo via Robocopy"
STATUS_COPY_ERROR = "erro na cópia"
STATUS_EXECUTING = "executando via PSExec"
STATUS_INSTALLING = "instalando"
STATUS_COMPLETED = "concluído"
STATUS_REBOOT_REQUIRED = "concluído com reinicialização necessária"
STATUS_REBOOT_STARTED = "concluído e reinicialização iniciada"
STATUS_FAILED = "falhou"
STATUS_CANCELLED = "cancelado"
STATUS_CONNECTION = "erro de conexão"
STATUS_PERMISSION = "erro de permissão"
STATUS_INSTALLER = "erro do Windows Installer"

SUCCESS_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_REBOOT_REQUIRED, STATUS_REBOOT_STARTED}
)
FAILURE_STATUSES = frozenset(
    {
        STATUS_COPY_ERROR,
        STATUS_FAILED,
        STATUS_CONNECTION,
        STATUS_PERMISSION,
        STATUS_INSTALLER,
    }
)

MSI_SUCCESS = 0
MSI_REBOOT_REQUIRED = 3010
MSI_REBOOT_STARTED = 1641


@dataclass
class MsiHostRow:
    host: str
    status: str = STATUS_WAITING
    exit_code: Optional[int] = None
    copy_status: str = "—"
    psexec_status: str = "—"
    message: str = ""
    started_at: str = ""
    ended_at: str = ""
    logs: str = ""
    selected: bool = True
    online: bool = True
    order: int = 0

    def as_tuple(self) -> Tuple[str, ...]:
        code = "—" if self.exit_code is None else str(self.exit_code)
        return (
            self.host,
            self.status,
            code,
            self.copy_status,
            self.psexec_status,
            self.started_at or "—",
            self.ended_at or "—",
            self.message or "—",
        )


@dataclass
class MsiBatchSummary:
    completed: int = 0
    reboot: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0

    def add(self, row: MsiHostRow) -> None:
        if row.status in (STATUS_REBOOT_REQUIRED, STATUS_REBOOT_STARTED):
            self.reboot += 1
        elif row.status == STATUS_COMPLETED:
            self.completed += 1
        elif row.status == STATUS_CANCELLED:
            self.cancelled += 1
        elif row.status in FAILURE_STATUSES:
            self.failed += 1
        elif row.status in (STATUS_WAITING,):
            self.skipped += 1

    def as_text(self) -> str:
        return (
            f"Concluídos: {self.completed}  |  Reinício: {self.reboot}  |  "
            f"Falhas: {self.failed}  |  Cancelados: {self.cancelled}  |  "
            f"Ignorados: {self.skipped}"
        )


@dataclass
class MsiBatchPlan:
    robocopy: Optional[CommandSpec]
    psexec: CommandSpec
    cleanup: Optional[CommandSpec]
    msiexec_display: str
    remote_msi_path: str
    errors: List[str] = field(default_factory=list)


def now_clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def classify_msi_exit(code: Optional[int]) -> str:
    if code == MSI_SUCCESS:
        return STATUS_COMPLETED
    if code == MSI_REBOOT_REQUIRED:
        return STATUS_REBOOT_REQUIRED
    if code == MSI_REBOOT_STARTED:
        return STATUS_REBOOT_STARTED
    return STATUS_INSTALLER


def classify_transport_error(message: str, code: Optional[int] = None) -> str:
    text = (message or "").casefold()
    permission = (
        "access is denied",
        "acesso negado",
        "logon failure",
        "falha de logon",
        "1326",
        "error 5",
    )
    connection = (
        "couldn't access",
        "could not start",
        "error connecting",
        "timeout connecting",
        "the network path was not found",
        "o caminho de rede",
        "inacessível",
        "offline",
        "error 53",
        "error 67",
    )
    if any(marker in text for marker in permission):
        return STATUS_PERMISSION
    if any(marker in text for marker in connection):
        return STATUS_CONNECTION
    if code in {5, 1326}:
        return STATUS_PERMISSION
    if code in {53, 67, 2250}:
        return STATUS_CONNECTION
    return STATUS_FAILED


def summarize_msi_rows(rows: Sequence[MsiHostRow]) -> MsiBatchSummary:
    summary = MsiBatchSummary()
    for row in rows:
        summary.add(row)
    return summary


def _copy_params(params: Optional[dict]) -> dict:
    return dict(params or {})


def build_batch_msi_plan(
    *,
    host: str,
    msi_path: str,
    msi_params: Optional[dict],
    robocopy_params: Optional[dict],
    psexec_params: Optional[dict],
    pstools_path: str,
    has_password: bool,
    cleanup: bool = True,
) -> MsiBatchPlan:
    """Monta Robocopy + PsExec/msiexec com o CommandBuilder existente."""
    errors = collect_validation_errors(msi_path=msi_path, msi_params=msi_params or {})
    builder = CommandBuilder()
    builder.set_file_selection({"mode": "file", "file": msi_path, "folder": None})
    params = prepare_batch_psexec_params(
        psexec_params,
        host=host,
        pstools_path=pstools_path,
    )
    params["-c"] = False
    params["-f"] = False
    params["-v"] = False
    if has_password and (params.get("user") or "").strip():
        params["has_password"] = True
    builder.set_psexec_params(params)
    builder.set_msi_params(_copy_params(msi_params) | {"enable": True})
    builder.set_robocopy_params(_copy_params(robocopy_params) or {"dest": "temp"})

    robocopy = builder.build_robocopy_spec()
    psexec = builder.build_psexec_spec()
    msiexec_display = builder.build_msiexec()
    remote_msi = builder._remote_path_after_robocopy() or ""

    if robocopy is None:
        errors.append("Não foi possível montar o comando Robocopy.")
    elif (robocopy.display_command or "").startswith("#"):
        errors.append(robocopy.display_command.lstrip("# ").strip())
    if (psexec.display_command or "").startswith("#"):
        errors.append(psexec.display_command.lstrip("# ").strip())
    if not remote_msi:
        errors.append("Caminho remoto do MSI não pôde ser determinado.")

    cleanup_spec = None
    if cleanup and remote_msi:
        cleanup_builder = CommandBuilder()
        cleanup_params = dict(params)
        cleanup_params["remote_cmd"] = ""
        cleanup_builder.set_psexec_params(cleanup_params)
        cleanup_spec = cleanup_builder._spec_from_psexec_argv(
            ["cmd", "/c", "del", "/f", "/q", remote_msi],
            append_extra=False,
        )

    return MsiBatchPlan(
        robocopy=robocopy,
        psexec=psexec,
        cleanup=cleanup_spec,
        msiexec_display=msiexec_display,
        remote_msi_path=remote_msi,
        errors=errors,
    )


def preview_msiexec_command(
    *,
    msi_path: str,
    msi_params: Optional[dict],
    robocopy_params: Optional[dict],
    host: str = "HOST",
) -> str:
    builder = CommandBuilder()
    builder.set_file_selection({"mode": "file", "file": msi_path, "folder": None})
    builder.set_msi_params(_copy_params(msi_params) | {"enable": True})
    builder.set_robocopy_params(_copy_params(robocopy_params) or {"dest": "temp"})
    builder.set_psexec_params({"host": host, "psexec_path": "", "remote_cmd": ""})
    display = builder.build_msiexec()
    return display or "# msiexec [opções] <arquivo.msi>"


def run_robocopy_spec(
    spec: CommandSpec,
    *,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_output: Optional[LogFn] = None,
    password: str = "",
) -> RemoteInstallOutcome:
    display = spec.display_command or ""
    if (display or "").startswith("#") or not spec.argv:
        return RemoteInstallOutcome(
            ok=False,
            message=display.lstrip("# ").strip() or "Falha ao montar o Robocopy.",
            display_command=display,
        )
    captured = run_argv_captured(spec.argv, should_cancel=should_cancel)
    stdout = decode_console_bytes(captured.stdout or b"")
    stderr = decode_console_bytes(captured.stderr or b"")
    passwords = [password] if (password or "").strip() else None
    text = redact_command_text("\n".join(part for part in (stdout, stderr) if part), passwords=passwords)
    if on_output and text:
        on_output(text)
    if captured.cancelled:
        return RemoteInstallOutcome(
            ok=False,
            cancelled=True,
            return_code=captured.returncode,
            message="Cópia cancelada.",
            stdout=text,
            display_command=display,
        )
    if captured.spawn_error:
        return RemoteInstallOutcome(
            ok=False,
            message=captured.spawn_error,
            stdout=text,
            display_command=display,
        )
    code = captured.returncode
    ok = is_robocopy_success(code)
    return RemoteInstallOutcome(
        ok=ok,
        return_code=code,
        message="" if ok else f"Robocopy retornou código {code}",
        stdout=text,
        display_command=display,
    )


def run_cleanup_spec(
    spec: Optional[CommandSpec],
    *,
    password: str = "",
    should_cancel: Optional[Callable[[], bool]] = None,
) -> None:
    if spec is None or not spec.argv or (spec.display_command or "").startswith("#"):
        return
    argv = materialize_password_in_argv(spec.argv, password)
    run_argv_captured(argv, should_cancel=should_cancel, timeout_s=30)


def apply_msi_copy_outcome(row: MsiHostRow, outcome: RemoteInstallOutcome) -> MsiHostRow:
    if outcome.cancelled:
        row.status = STATUS_CANCELLED
        row.copy_status = STATUS_CANCELLED
        row.message = outcome.message or "Cópia cancelada."
        row.ended_at = now_clock()
        return row
    if outcome.ok:
        row.copy_status = "ok"
        return row
    row.copy_status = STATUS_COPY_ERROR
    row.status = classify_transport_error(outcome.message or outcome.stdout, outcome.return_code)
    if row.status not in (STATUS_PERMISSION, STATUS_CONNECTION):
        row.status = STATUS_COPY_ERROR
    row.exit_code = outcome.return_code
    row.message = outcome.message or "Falha na cópia via Robocopy."
    row.ended_at = now_clock()
    return row


def apply_msi_execution_to_batch(row: BatchHostRow, msi_row: MsiHostRow) -> BatchHostRow:
    """Copia o resultado do Robocopy/msiexec para a linha visual do lote EXE."""
    if msi_row.status in SUCCESS_STATUSES:
        return apply_install_outcome(
            row,
            RemoteInstallOutcome(
                ok=True,
                return_code=msi_row.exit_code,
                psexec_ok=True,
                installer_ok=True,
                message=msi_row.message,
            ),
        )
    if msi_row.status == STATUS_CANCELLED:
        return apply_install_outcome(
            row,
            RemoteInstallOutcome(
                ok=False,
                cancelled=True,
                return_code=msi_row.exit_code,
                message=msi_row.message or REASON_CANCELLED,
            ),
        )
    row.result = RESULT_ERROR
    row.needs_install = False
    if msi_row.status == STATUS_CONNECTION:
        row.online = False
        row.reason = msi_row.message or REASON_OFFLINE
    else:
        row.reason = msi_row.message or REASON_INSTALL_FAILED
    return row


def apply_msi_install_outcome(row: MsiHostRow, outcome: RemoteInstallOutcome) -> MsiHostRow:
    row.exit_code = outcome.return_code
    row.logs = outcome.stdout or row.logs
    row.ended_at = now_clock()
    if outcome.cancelled:
        row.status = STATUS_CANCELLED
        row.psexec_status = STATUS_CANCELLED
        row.message = outcome.message or "Operação cancelada."
        return row
    if outcome.psexec_ok and outcome.installer_ok:
        row.psexec_status = "ok"
        row.status = classify_msi_exit(outcome.return_code)
        if row.status == STATUS_REBOOT_REQUIRED:
            row.message = "Instalação concluída; é necessário reiniciar o computador."
        elif row.status == STATUS_REBOOT_STARTED:
            row.message = "Instalação concluída; a reinicialização foi iniciada."
        else:
            row.message = "Instalação concluída."
        return row
    if not outcome.psexec_ok:
        row.psexec_status = classify_transport_error(
            outcome.message or outcome.stdout, outcome.return_code
        )
        row.status = row.psexec_status
        row.message = outcome.message or "Falha ao executar via PsExec."
        return row
    row.psexec_status = "ok"
    row.status = STATUS_INSTALLER
    row.message = outcome.message or f"Windows Installer retornou código {outcome.return_code}"
    return row


def run_msi_host(
    row: MsiHostRow,
    plan: MsiBatchPlan,
    *,
    password: str = "",
    should_cancel: Optional[Callable[[], bool]] = None,
    on_output: Optional[LogFn] = None,
    copy_runner: Optional[Callable[..., RemoteInstallOutcome]] = None,
    install_runner: Optional[Callable[..., RemoteInstallOutcome]] = None,
    cleanup_runner: Optional[Callable[..., None]] = None,
    on_phase: Optional[Callable[[str, str], None]] = None,
    cols: int = 120,
    rows: int = 30,
) -> MsiHostRow:
    """Copia com Robocopy e, só depois, instala com PsExec/msiexec."""
    row.started_at = now_clock()
    row.message = ""
    if plan.errors or plan.robocopy is None:
        row.status = STATUS_FAILED
        row.message = "; ".join(plan.errors) or "Falha ao montar o plano MSI."
        row.ended_at = now_clock()
        return row
    if should_cancel and should_cancel():
        row.status = STATUS_CANCELLED
        row.message = "Operação cancelada."
        row.ended_at = now_clock()
        return row

    row.status = STATUS_COPYING
    if on_phase is not None and plan.robocopy is not None:
        on_phase("copy", plan.robocopy.display_command or "")
    copy_fn = copy_runner or run_robocopy_spec
    copy_outcome = copy_fn(
        plan.robocopy,
        should_cancel=should_cancel,
        on_output=on_output,
        password=password,
    )
    apply_msi_copy_outcome(row, copy_outcome)
    if row.status != STATUS_COPYING:
        return row

    if should_cancel and should_cancel():
        row.status = STATUS_CANCELLED
        row.message = "Operação cancelada após a cópia."
        row.ended_at = now_clock()
        return row

    row.status = STATUS_EXECUTING
    if on_phase is not None:
        on_phase("install", plan.msiexec_display or "")
    install_fn = install_runner or run_remote_installer
    row.status = STATUS_INSTALLING
    install_outcome = install_fn(
        plan.psexec,
        password=password,
        should_cancel=should_cancel,
        on_output=on_output,
        cols=cols,
        rows=rows,
    )
    apply_msi_install_outcome(row, install_outcome)
    if row.status in SUCCESS_STATUSES:
        cleaner = cleanup_runner or run_cleanup_spec
        cleaner(plan.cleanup, password=password, should_cancel=should_cancel)
    return row


def audit_msi_batch(
    *,
    user: str,
    msi_path: str,
    hosts: Sequence[str],
    msiexec_preview: str,
    rows: Sequence[object],
    started_at: str,
    ended_at: str,
    passwords: Optional[Sequence[str]] = None,
) -> str:
    per_host = []
    for row in rows:
        if isinstance(row, BatchHostRow):
            per_host.append(
                f"{row.host}: action={row.action} result={row.result} "
                f"reason={row.reason} version={row.version} desired={row.desired}"
            )
            continue
        per_host.append(
            f"{row.host}: status={row.status} copy={row.copy_status} "
            f"psexec={row.psexec_status} exit={row.exit_code} msg={row.message}"
        )
    detail = (
        f"user={user or '—'} file={msi_path} hosts={','.join(hosts)} "
        f"params={msiexec_preview} start={started_at} end={ended_at} "
        f"results={' | '.join(per_host)}"
    )
    return log_operation("batch_msi", detail=detail, passwords=passwords)


def selected_online_rows(rows: Dict[str, MsiHostRow]) -> List[MsiHostRow]:
    pending = [
        row
        for row in rows.values()
        if row.selected and row.online and row.status in {STATUS_WAITING, STATUS_CANCELLED}
    ]
    pending.sort(key=lambda item: (item.order, item.host.casefold()))
    return pending


def count_tab_titles(titles: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for title in titles:
        counts[title] = counts.get(title, 0) + 1
    return counts
