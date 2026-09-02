"""PsPasswd — argv, sanitização posicional e classificação de respostas.

Sem Qt. O PsPasswd atua no computador local se o destino for omitido;
por isso o host nunca pode ficar vazio. A nova senha é sempre o último
argumento; a senha administrativa segue ``-p``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence

from remoteops.utils.local_accounts import (
    LocalAccount,
    account_belongs_to_host,
    is_listed_local_account,
    normalize_account_name,
    validate_account_name,
    validate_admin_credential,
    validate_local_account_host,
    validate_new_password,
)
from remoteops.utils.ping import normalize_host
from remoteops.utils.psshutdown import build_remote_target
from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool
from remoteops.utils.redaction import REDACTED, quote_arg_for_display

PSPASSWD_NAMES: tuple[str, ...] = ("PsPasswd64.exe", "PsPasswd.exe")

DEFAULT_PSPASSWD_TIMEOUT_SECONDS = 60.0
LOCAL_PROCESS_TIMEOUT_FLOOR = 30.0


class PasswordChangePhase(str, Enum):
    SUCCESS = "success"
    ACCESS_DENIED = "access_denied"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCOUNT_NOT_FOUND = "account_not_found"
    PASSWORD_POLICY_REJECTED = "password_policy_rejected"
    HOST_UNAVAILABLE = "host_unavailable"
    TOOL_MISSING = "tool_missing"
    TIMEOUT = "timeout"
    CANCELLED_LOCAL = "cancelled_local"
    STALE_HOST = "stale_host"
    UNKNOWN_FAILURE = "unknown_failure"


PHASE_LABELS = {
    PasswordChangePhase.SUCCESS: "Senha alterada",
    PasswordChangePhase.ACCESS_DENIED: "Acesso negado",
    PasswordChangePhase.AUTHENTICATION_FAILED: "Autenticação administrativa inválida",
    PasswordChangePhase.ACCOUNT_NOT_FOUND: "Conta não encontrada",
    PasswordChangePhase.PASSWORD_POLICY_REJECTED: "Senha rejeitada pela política",
    PasswordChangePhase.HOST_UNAVAILABLE: "Host indisponível",
    PasswordChangePhase.TOOL_MISSING: "PsPasswd ausente",
    PasswordChangePhase.TIMEOUT: "Tempo esgotado",
    PasswordChangePhase.CANCELLED_LOCAL: "Processo local interrompido",
    PasswordChangePhase.STALE_HOST: "Host alterado durante a operação",
    PasswordChangePhase.UNKNOWN_FAILURE: "Falha desconhecida",
}


@dataclass(frozen=True)
class PasswordChangeRequest:
    host: str
    account_name: str
    account_sid: str = ""


@dataclass(frozen=True)
class PasswordChangeResult:
    host: str
    account_name: str
    account_sid: str
    phase: PasswordChangePhase
    exit_code: Optional[int] = None
    output_sanitized: str = ""
    argv_preview: str = ""
    duration: float = 0.0

    def __repr__(self) -> str:
        return (
            f"PasswordChangeResult(host={self.host!r}, account={self.account_name!r}, "
            f"phase={self.phase.value}, exit_code={self.exit_code})"
        )

    @property
    def ok(self) -> bool:
        return self.phase == PasswordChangePhase.SUCCESS


def resolve_pspasswd_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSPASSWD_NAMES)


def pspasswd_available(pstools_dir: str = "") -> bool:
    path = resolve_pspasswd_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def redact_pspasswd_argv(
    argv: Sequence[str],
    *,
    placeholder: str = REDACTED,
) -> list[str]:
    """Mascara ``-p`` e o último argumento (nova senha)."""
    out = [str(a) for a in argv]
    for i, arg in enumerate(out):
        if arg.lower() == "-p" and i + 1 < len(out):
            out[i + 1] = placeholder
    if len(out) >= 4:
        out[-1] = placeholder
    return out


def preview_pspasswd_argv(argv: Sequence[str], *, placeholder: str = REDACTED) -> str:
    safe = redact_pspasswd_argv(argv, placeholder=placeholder)
    return " ".join(quote_arg_for_display(a) for a in safe)


def redact_pspasswd_output(
    text: str,
    *,
    admin_password: str = "",
    new_password: str = "",
    placeholder: str = REDACTED,
) -> str:
    result = text or ""
    passwords = [p for p in (admin_password, new_password) if p]
    for pwd in sorted(set(passwords), key=len, reverse=True):
        if pwd:
            result = result.replace(pwd, placeholder)
    return result


def build_pspasswd_argv(
    exe: str,
    request: PasswordChangeRequest,
    new_password: str,
    *,
    user: str = "",
    admin_password: str = "",
    include_secrets: bool = True,
) -> list[str]:
    """Monta argv sem shell. Retorna lista vazia se validação falhar."""
    errors = validate_password_change_request(
        request,
        new_password=new_password,
        confirmation=new_password,
        admin_user=user,
        admin_password=admin_password,
        listed_accounts=(),
        require_listed=False,
    )
    if errors:
        return []
    if not (exe or "").strip():
        return []
    target = build_remote_target(request.host)
    if not target:
        return []
    argv: list[str] = [exe, target]
    user_name = (user or "").strip()
    if user_name:
        argv.extend(["-u", user_name])
        secret = admin_password or ""
        if secret:
            argv.extend(["-p", secret if include_secrets else REDACTED])
    argv.append(request.account_name)
    argv.append(new_password if include_secrets else REDACTED)
    return argv


def local_process_timeout_s() -> float:
    return max(LOCAL_PROCESS_TIMEOUT_FLOOR, DEFAULT_PSPASSWD_TIMEOUT_SECONDS)


def validate_password_change_request(
    request: PasswordChangeRequest,
    *,
    new_password: str,
    confirmation: str,
    admin_user: str = "",
    admin_password: str = "",
    listed_accounts: Sequence[LocalAccount] = (),
    account: Optional[LocalAccount] = None,
    require_listed: bool = True,
) -> List[str]:
    errors: List[str] = []
    errors.extend(validate_local_account_host(request.host))
    errors.extend(validate_account_name(request.account_name))
    errors.extend(validate_new_password(new_password, confirmation))
    errors.extend(validate_admin_credential(admin_user, admin_password))

    if account is not None:
        if not account_belongs_to_host(account, request.host):
            errors.append("A conta selecionada não pertence ao host informado.")
        if normalize_account_name(account.name) != normalize_account_name(
            request.account_name
        ):
            errors.append("A conta informada não corresponde à seleção.")
        if request.account_sid and (account.sid or "").strip().casefold() != (
            request.account_sid or ""
        ).strip().casefold():
            errors.append("O SID da conta não corresponde à seleção.")
    elif require_listed and listed_accounts:
        probe = LocalAccount(
            host=normalize_host(request.host),
            name=request.account_name,
            sid=request.account_sid or "",
        )
        if not is_listed_local_account(probe, listed_accounts):
            errors.append("A conta deve ter sido obtida na consulta do host.")

    return errors


def looks_like_usage(text: str) -> bool:
    t = (text or "").lower()
    return "usage:" in t and "pspasswd" in t


def classify_pspasswd_output(
    text: str,
    *,
    returncode: int = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    spawn_error: str = "",
) -> PasswordChangePhase:
    detail = (text or "").strip()
    if spawn_error:
        low = spawn_error.lower()
        if "não encontrado" in low or "not found" in low:
            return PasswordChangePhase.TOOL_MISSING
        return PasswordChangePhase.UNKNOWN_FAILURE
    if cancelled:
        return PasswordChangePhase.CANCELLED_LOCAL
    if timed_out:
        return PasswordChangePhase.TIMEOUT

    low = detail.lower()
    if looks_like_usage(detail):
        return PasswordChangePhase.UNKNOWN_FAILURE
    if any(
        marker in low
        for marker in (
            "logon failure",
            "falha de logon",
            "unknown user name",
            "wrong password",
            "bad username",
            "nome de usuário desconhecido",
        )
    ):
        return PasswordChangePhase.AUTHENTICATION_FAILED
    if any(
        marker in low
        for marker in (
            "access is denied",
            "acesso negado",
        )
    ):
        return PasswordChangePhase.ACCESS_DENIED
    if any(
        marker in low
        for marker in (
            "unable to find",
            "could not find",
            "não foi possível localizar",
            "nao foi possivel localizar",
            "account not found",
            "conta não encontrada",
            "conta nao encontrada",
            "no mapping",
        )
    ):
        return PasswordChangePhase.ACCOUNT_NOT_FOUND
    if any(
        marker in low
        for marker in (
            "password does not meet",
            "does not meet the password policy",
            "política de senhas",
            "politica de senhas",
            "password policy",
            "senha não atende",
            "senha nao atende",
        )
    ):
        return PasswordChangePhase.PASSWORD_POLICY_REJECTED
    if any(
        marker in low
        for marker in (
            "network path was not found",
            "rpc server is unavailable",
            "servidor rpc",
            "could not connect",
            "unable to connect",
            "the network name cannot be found",
            "não foi possível localizar o caminho",
        )
    ):
        return PasswordChangePhase.HOST_UNAVAILABLE

    if returncode == 0:
        return PasswordChangePhase.SUCCESS
    if returncode != 0 and not detail:
        return PasswordChangePhase.UNKNOWN_FAILURE
    return PasswordChangePhase.UNKNOWN_FAILURE


def phase_detail(phase: PasswordChangePhase, output: str = "") -> str:
    label = PHASE_LABELS.get(phase, phase.value)
    detail = (output or "").strip()
    if detail:
        return detail
    if phase == PasswordChangePhase.CANCELLED_LOCAL:
        return (
            "O processo local foi interrompido. "
            "Uma operação já enviada pode ter sido concluída no computador remoto."
        )
    return label


def audit_detail(
    request: PasswordChangeRequest,
    phase: PasswordChangePhase,
    *,
    operator: str = "",
    exit_code: Optional[int] = None,
) -> str:
    parts = [
        f"host={normalize_host(request.host)}",
        f"account={request.account_name}",
    ]
    if request.account_sid:
        parts.append(f"sid={request.account_sid}")
    if operator:
        parts.append(f"operator={operator}")
    parts.append(f"phase={phase.value}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    return " ".join(parts)


def collect_secret_passwords(
  admin_password: str = "",
  new_password: str = "",
) -> List[str]:
    return [p for p in (admin_password, new_password) if p]
