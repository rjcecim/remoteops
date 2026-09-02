"""Alteração de senha de contas locais via PsPasswd."""

from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Sequence

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.process_runner import CancelFn, CapturedProcess, run_argv_captured
from remoteops.utils.app_logging import log_operation
from remoteops.utils.host_reachability import is_stale_host_result
from remoteops.utils.local_accounts import LocalAccount
from remoteops.utils.ping import normalize_host
from remoteops.utils.pspasswd import (
    PasswordChangePhase,
    PasswordChangeRequest,
    PasswordChangeResult,
    audit_detail,
    build_pspasswd_argv,
    classify_pspasswd_output,
    collect_secret_passwords,
    local_process_timeout_s,
    phase_detail,
    preview_pspasswd_argv,
    pspasswd_available,
    redact_pspasswd_output,
    resolve_pspasswd_exe,
    validate_password_change_request,
)
from remoteops.utils.pstools import get_pstools_dir

RunnerFn = Callable[..., CapturedProcess]


def current_operator() -> str:
    for key in ("USERNAME", "USER"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    try:
        return (os.getlogin() or "").strip()
    except Exception:
        return ""


class PasswordChangeService:
    """Valida, executa e classifica alteração de senha local."""

    def run(
        self,
        request: PasswordChangeRequest,
        *,
        new_password: str,
        confirmation: str = "",
        admin_user: str = "",
        admin_password: str = "",
        pstools_dir: str = "",
        listed_accounts: Sequence[LocalAccount] = (),
        account: Optional[LocalAccount] = None,
        should_cancel: Optional[CancelFn] = None,
        current_host: str = "",
        runner: Optional[RunnerFn] = None,
        operator: str = "",
    ) -> PasswordChangeResult:
        host = normalize_host(request.host)
        confirm = confirmation if confirmation != "" else new_password
        errors = validate_password_change_request(
            request,
            new_password=new_password,
            confirmation=confirm,
            admin_user=admin_user,
            admin_password=admin_password,
            listed_accounts=listed_accounts,
            account=account,
            require_listed=bool(listed_accounts),
        )
        if errors:
            return PasswordChangeResult(
                host=host,
                account_name=request.account_name,
                account_sid=request.account_sid or "",
                phase=PasswordChangePhase.UNKNOWN_FAILURE,
                output_sanitized=errors[0],
            )

        wanted = host
        visible = normalize_host(current_host) or host
        if is_stale_host_result(host, wanted, visible):
            return PasswordChangeResult(
                host=host,
                account_name=request.account_name,
                account_sid=request.account_sid or "",
                phase=PasswordChangePhase.STALE_HOST,
                output_sanitized=phase_detail(PasswordChangePhase.STALE_HOST),
            )

        tool_dir = pstools_dir or get_pstools_dir()
        if not pspasswd_available(tool_dir):
            return PasswordChangeResult(
                host=host,
                account_name=request.account_name,
                account_sid=request.account_sid or "",
                phase=PasswordChangePhase.TOOL_MISSING,
                output_sanitized=phase_detail(PasswordChangePhase.TOOL_MISSING),
            )

        exe = resolve_pspasswd_exe(tool_dir)
        argv = build_pspasswd_argv(
            exe,
            request,
            new_password,
            user=admin_user,
            admin_password=admin_password,
            include_secrets=True,
        )
        preview = preview_pspasswd_argv(argv)
        if not argv:
            return PasswordChangeResult(
                host=host,
                account_name=request.account_name,
                account_sid=request.account_sid or "",
                phase=PasswordChangePhase.UNKNOWN_FAILURE,
                argv_preview=preview,
                output_sanitized="Não foi possível montar o comando PsPasswd.",
            )

        secrets = collect_secret_passwords(admin_password, new_password)
        started = time.monotonic()
        run_fn = runner or run_argv_captured
        try:
            captured = run_fn(
                argv,
                timeout_s=local_process_timeout_s(),
                should_cancel=should_cancel,
            )
        except TypeError:
            captured = run_fn(argv)
        except Exception as exc:
            captured = CapturedProcess(spawn_error=str(exc) or "Falha ao iniciar o PsPasswd.")

        duration = max(0.0, time.monotonic() - started)
        output = _combine_output(captured)
        output = redact_pspasswd_output(
            output,
            admin_password=admin_password,
            new_password=new_password,
        )
        phase = classify_pspasswd_output(
            output,
            returncode=int(captured.returncode),
            timed_out=bool(captured.timed_out),
            cancelled=bool(captured.cancelled),
            spawn_error=captured.spawn_error or "",
        )

        visible_now = normalize_host(current_host) or host
        if is_stale_host_result(host, wanted, visible_now):
            phase = PasswordChangePhase.STALE_HOST

        result = PasswordChangeResult(
            host=host,
            account_name=request.account_name,
            account_sid=request.account_sid or "",
            phase=phase,
            exit_code=int(captured.returncode),
            output_sanitized=phase_detail(phase, output),
            argv_preview=preview,
            duration=duration,
        )
        log_operation(
            "local_password_change",
            detail=audit_detail(
                request,
                phase,
                operator=operator or current_operator(),
                exit_code=result.exit_code,
            ),
            exit_code=result.exit_code,
            passwords=secrets,
        )
        return result

    def run_batch(
        self,
        requests: Sequence[tuple[PasswordChangeRequest, Optional[LocalAccount]]],
        *,
        new_password: str,
        confirmation: str = "",
        admin_user: str = "",
        admin_password: str = "",
        pstools_dir: str = "",
        listed_accounts: Sequence[LocalAccount] = (),
        should_cancel: Optional[CancelFn] = None,
        current_host: str = "",
        runner: Optional[RunnerFn] = None,
        operator: str = "",
    ) -> List[PasswordChangeResult]:
        results: List[PasswordChangeResult] = []
        for request, account in requests:
            if should_cancel and should_cancel():
                results.append(
                    PasswordChangeResult(
                        host=normalize_host(request.host),
                        account_name=request.account_name,
                        account_sid=request.account_sid or "",
                        phase=PasswordChangePhase.CANCELLED_LOCAL,
                        output_sanitized=phase_detail(PasswordChangePhase.CANCELLED_LOCAL),
                    )
                )
                continue
            results.append(
                self.run(
                    request,
                    new_password=new_password,
                    confirmation=confirmation,
                    admin_user=admin_user,
                    admin_password=admin_password,
                    pstools_dir=pstools_dir,
                    listed_accounts=listed_accounts,
                    account=account,
                    should_cancel=should_cancel,
                    current_host=current_host,
                    runner=runner,
                    operator=operator,
                )
            )
        return results


def _combine_output(captured: CapturedProcess) -> str:
    return (
        decode_console_bytes(captured.stdout or b"")
        + "\n"
        + decode_console_bytes(captured.stderr or b"")
    ).strip()
