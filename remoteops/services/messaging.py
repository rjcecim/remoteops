"""Envio de mensagens a sessões Windows via ``msg.exe``.

A construção do comando é separada da execução e da UI. Credenciais
reutilizam ``CredentialContext`` — nunca duplicadas aqui.

O Executor da janela principal pode estar ocupado com ConPTY/PsExec.
A aba Mensagem usa um Executor dedicado; este serviço só valida,
monta ``CommandSpec`` e classifica o resultado.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from remoteops.core.models import CommandSpec, ExecutionResult, OperationStatus
from remoteops.services.msg_style import msg_unit_count
from remoteops.services.ops import CredentialContext, build_psexec_argv, resolve_psexec_exe
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.redaction import redact_command_text

# Limite documentado do ``msg.exe`` (Microsoft).
MSG_MAX_LENGTH = 255
DEFAULT_DISPLAY_SECONDS = 30
MIN_DISPLAY_SECONDS = 5
MAX_DISPLAY_SECONDS = 86_400
RECIPIENT_ALL = "*"
MSG_EXE_NAME = "msg.exe"

# Destinatário: todos (*) ou ID numérico de sessão (0–65535).
_SESSION_ID_RE = re.compile(r"^[0-9]{1,5}$")
_RECIPIENT_INJECT = frozenset('&|<>^"\'%!;,/\t\\@')
_HOST_EXTRA_INVALID = frozenset("/\\:;*?@")


class MessageResultKind(str, Enum):
    COMMAND_SENT = "command_sent"
    ACCESS_DENIED = "access_denied"
    HOST_UNAVAILABLE = "host_unavailable"
    NO_SESSION = "no_session"
    INVALID_SESSION = "invalid_session"
    MSG_NOT_FOUND = "msg_not_found"
    PSEXEC_NOT_FOUND = "psexec_not_found"
    RPC_FAILURE = "rpc_failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MessageRequest:
    host: str
    recipient: str
    message: str
    timeout_seconds: Optional[int]
    verbose: bool


@dataclass(frozen=True)
class MessageClassification:
    kind: MessageResultKind
    ok: bool
    title: str
    detail: str
    exit_code: Optional[int] = None


def normalize_message(text: str) -> str:
    """Normaliza quebras de linha para o diálogo de uma linha do ``msg.exe``."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in raw.split("\n")]
    return " ".join(part for part in parts if part)


def is_valid_recipient(recipient: str) -> bool:
    value = (recipient or "").strip()
    if value == RECIPIENT_ALL:
        return True
    if not _SESSION_ID_RE.fullmatch(value):
        return False
    session_id = int(value)
    return 0 <= session_id <= 65535


def recipient_injection_chars(recipient: str) -> bool:
    value = recipient or ""
    if any(ch in _RECIPIENT_INJECT for ch in value):
        return True
    if value.startswith("-") or value.startswith("/"):
        return True
    return False


def psexec_executable_available(pstools_path: str) -> bool:
    exe = resolve_psexec_exe(pstools_path)
    return bool(exe) and os.path.isfile(exe)


def history_detail(request: MessageRequest, *, ok: bool) -> str:
    """Resumo seguro para auditoria — sem senha e sem o texto da mensagem."""
    timeout = (
        "none"
        if request.timeout_seconds is None
        else str(int(request.timeout_seconds))
    )
    chars = msg_unit_count(normalize_message(request.message))
    return (
        f"host={normalize_host(request.host)} "
        f"method=psexec "
        f"recipient={request.recipient} "
        f"time={timeout} "
        f"verbose={int(bool(request.verbose))} "
        f"ok={int(bool(ok))} "
        f"chars={chars}"
    )


def build_msg_argv(
    *,
    msg_exe: str,
    recipient: str,
    message: str,
    timeout_seconds: Optional[int] = None,
    verbose: bool = False,
) -> list[str]:
    """Lista argv remoto do ``msg.exe`` (sem shell). A mensagem é o último argumento."""
    argv = [msg_exe, recipient]
    if timeout_seconds is not None:
        argv.append(f"/TIME:{int(timeout_seconds)}")
    if verbose:
        argv.append("/V")
    argv.append(message)
    return argv


class MessageService:
    """Validação, construção de comando e classificação — sem executar."""

    def validate_request(self, request: MessageRequest) -> list[str]:
        errors: list[str] = []
        host = normalize_host(request.host)
        if (
            not host
            or not is_valid_host(host)
            or host.startswith("-")
            or any(ch in host for ch in _HOST_EXTRA_INVALID)
        ):
            errors.append("Host inválido.")

        recipient = (request.recipient or "").strip()
        if recipient_injection_chars(recipient) or not is_valid_recipient(recipient):
            errors.append("Destinatário inválido.")

        message = normalize_message(request.message)
        if not message:
            errors.append("A mensagem não pode estar vazia.")
        elif msg_unit_count(message) > MSG_MAX_LENGTH:
            errors.append(
                f"A mensagem excede o limite de {MSG_MAX_LENGTH} caracteres do msg.exe."
            )

        if request.timeout_seconds is not None:
            try:
                seconds = int(request.timeout_seconds)
            except (TypeError, ValueError):
                errors.append("Tempo de exibição inválido.")
            else:
                if seconds < MIN_DISPLAY_SECONDS or seconds > MAX_DISPLAY_SECONDS:
                    errors.append(
                        f"Tempo de exibição deve estar entre "
                        f"{MIN_DISPLAY_SECONDS} e {MAX_DISPLAY_SECONDS} segundos."
                    )

        return errors

    def build_psexec_spec(
        self,
        request: MessageRequest,
        *,
        pstools_path: str,
        creds: Optional[CredentialContext] = None,
        include_password: bool = True,
    ) -> CommandSpec:
        errors = self.validate_request(request)
        if errors:
            raise ValueError(errors[0])

        host = normalize_host(request.host)
        message = normalize_message(request.message)
        remote_argv = build_msg_argv(
            msg_exe=MSG_EXE_NAME,
            recipient=request.recipient.strip(),
            message=message,
            timeout_seconds=request.timeout_seconds,
            verbose=request.verbose,
        )
        extra_flags = ["-accepteula", "-nobanner", "-s"]
        used_creds = creds if creds is not None else CredentialContext()
        argv = build_psexec_argv(
            psexec_exe=resolve_psexec_exe(pstools_path),
            host=host,
            remote_argv=remote_argv,
            creds=used_creds,
            extra_flags=extra_flags,
            include_password=include_password,
        )
        passwords = used_creds.passwords if include_password else None
        return CommandSpec.from_argv(
            argv,
            has_secrets=bool(used_creds.passwords),
            passwords=passwords,
            metadata={
                "kind": "message",
                "method": "psexec",
                "host": host,
                "recipient": request.recipient.strip(),
            },
        )

    def classify_result(
        self,
        request: MessageRequest,
        result: ExecutionResult,
        *,
        passwords: Optional[Sequence[str]] = None,
    ) -> MessageClassification:
        exit_code = result.return_code
        combined = "\n".join(
            part
            for part in (result.stdout or "", result.stderr or "", result.exception or "")
            if part
        )
        safe = redact_command_text(combined, passwords=passwords)
        kind = self._classify_kind(request, result, safe)
        ok = kind is MessageResultKind.COMMAND_SENT
        title, detail = self._messages(request, kind, safe, exit_code)
        return MessageClassification(
            kind=kind,
            ok=ok,
            title=title,
            detail=detail,
            exit_code=exit_code,
        )

    def _classify_kind(
        self,
        request: MessageRequest,
        result: ExecutionResult,
        text: str,
    ) -> MessageResultKind:
        if result.cancelled or result.status is OperationStatus.CANCELLED:
            return MessageResultKind.CANCELLED
        if result.timed_out or result.status is OperationStatus.TIMED_OUT:
            return MessageResultKind.TIMEOUT

        low = (text or "").lower()
        exe = ""
        if result.exception:
            exe = result.exception.lower()

        if "psexec" in exe and (
            "não encontrado" in exe
            or "nao encontrado" in exe
            or "not found" in exe
        ):
            return MessageResultKind.PSEXEC_NOT_FOUND
        if "msg.exe" in exe or exe.endswith("msg.exe"):
            if "não encontrado" in exe or "nao encontrado" in exe or "not found" in exe:
                return MessageResultKind.MSG_NOT_FOUND
        if "executável não encontrado" in low or "executavel nao encontrado" in low:
            if "psexec" in low:
                return MessageResultKind.PSEXEC_NOT_FOUND
            return MessageResultKind.MSG_NOT_FOUND

        if any(
            m in low
            for m in (
                "access is denied",
                "acesso negado",
                "logon failure",
                "unknown user name or bad password",
                "logon failure: unknown user name",
            )
        ):
            return MessageResultKind.ACCESS_DENIED
        if any(
            m in low
            for m in (
                "couldn't access",
                "couldnt access",
                "network path was not found",
                "o caminho de rede não foi encontrado",
                "o caminho de rede nao foi encontrado",
                "host is offline",
                "the network name cannot be found",
            )
        ):
            return MessageResultKind.HOST_UNAVAILABLE
        if any(
            m in low
            for m in (
                "rpc server is unavailable",
                "o servidor rpc não está disponível",
                "o servidor rpc nao esta disponivel",
                "rpc_s_server_unavailable",
            )
        ):
            return MessageResultKind.RPC_FAILURE
        if any(
            m in low
            for m in (
                "no user exists",
                "no session exists",
                "não existe usuário",
                "nao existe usuario",
                "não existe sess",
                "nao existe sess",
            )
        ):
            return MessageResultKind.NO_SESSION
        if any(
            m in low
            for m in (
                "session does not exist",
                "sessão não existe",
                "sessao nao existe",
                "id is not valid",
                "identificador de sessão inválido",
                "identificador de sessao invalido",
            )
        ):
            return MessageResultKind.INVALID_SESSION

        if result.success or result.return_code == 0:
            return MessageResultKind.COMMAND_SENT
        return MessageResultKind.UNKNOWN

    def _messages(
        self,
        request: MessageRequest,
        kind: MessageResultKind,
        safe_text: str,
        exit_code: Optional[int],
    ) -> tuple[str, str]:
        host = normalize_host(request.host)
        recipient = request.recipient.strip()
        target = (
            f"todos os usuários de {host}"
            if recipient == RECIPIENT_ALL
            else f"a sessão {recipient} de {host}"
        )
        code = "" if exit_code is None else f" (código {exit_code})"
        snippets = {
            MessageResultKind.COMMAND_SENT: (
                f"Comando de mensagem enviado ao host {host}.",
                f"O processo remoto concluiu o envio para {target}.",
            ),
            MessageResultKind.ACCESS_DENIED: (
                f"Acesso negado ao enviar para {host}{code}.",
                safe_text or "Credenciais ou permissão insuficientes.",
            ),
            MessageResultKind.HOST_UNAVAILABLE: (
                f"Host {host} indisponível{code}.",
                safe_text or "Não foi possível alcançar o host.",
            ),
            MessageResultKind.NO_SESSION: (
                f"Nenhuma sessão existente em {host}{code}.",
                safe_text or "Não há usuários conectados para receber a mensagem.",
            ),
            MessageResultKind.INVALID_SESSION: (
                f"Sessão inválida ou encerrada em {host}{code}.",
                safe_text or "A sessão selecionada não está mais disponível.",
            ),
            MessageResultKind.MSG_NOT_FOUND: (
                f"msg.exe não encontrado{code}.",
                safe_text or "Não foi possível localizar o msg.exe.",
            ),
            MessageResultKind.PSEXEC_NOT_FOUND: (
                f"PsExec não encontrado{code}.",
                safe_text or "Verifique a pasta PSTools em Configurações.",
            ),
            MessageResultKind.RPC_FAILURE: (
                f"Falha de RPC ou comunicação com {host}{code}.",
                safe_text or "Serviço de sessão remota, firewall ou RPC indisponível.",
            ),
            MessageResultKind.TIMEOUT: (
                f"Tempo esgotado ao enviar para {host}{code}.",
                safe_text or "A operação excedeu o tempo limite.",
            ),
            MessageResultKind.CANCELLED: (
                "Envio cancelado.",
                "A operação foi interrompida antes de concluir.",
            ),
            MessageResultKind.UNKNOWN: (
                f"Falha ao enviar mensagem para {host}{code}.",
                safe_text or "Erro desconhecido.",
            ),
        }
        return snippets[kind]
