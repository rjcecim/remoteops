"""PsShutdown — argv, validação, reason codes e classificação de respostas.

Sem Qt e sem rede. O PsShutdown atua no computador local se o destino
for omitido; por isso o host nunca pode ficar vazio.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Sequence

from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool
from remoteops.utils.redaction import format_argv_for_display

PSSHUTDOWN_NAMES: tuple[str, ...] = ("PsShutdown64.exe", "PsShutdown.exe")

DEFAULT_COUNTDOWN_SECONDS = 120
MAX_COUNTDOWN_SECONDS = 86_400
MIN_COUNTDOWN_SECONDS = 0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
MAX_CONNECT_TIMEOUT_SECONDS = 600
MAX_MESSAGE_LENGTH = 512
DEFAULT_REASON_MAJOR = 0
DEFAULT_REASON_MINOR = 0
REBOOT_UPTIME_CONFIRM_SECONDS = 15 * 60

LOCAL_PROCESS_TIMEOUT_FLOOR = 30.0
FOLLOWUP_OFFLINE_GRACE_SECONDS = 60
FOLLOWUP_ONLINE_WAIT_SECONDS = 300
FOLLOWUP_POLL_SECONDS = 2.0


class PowerAction(str, Enum):
    POWER_OFF = "k"
    RESTART = "r"
    HIBERNATE = "h"
    SUSPEND = "d"
    LOCK = "l"
    LOGOFF = "o"
    ABORT = "a"
    SHUTDOWN_NO_POWEROFF = "s"
    MONITOR_OFF = "x"

    @property
    def flag(self) -> str:
        return f"-{self.value}"


class TimingMode(str, Enum):
    IMMEDIATE = "immediate"
    COUNTDOWN = "countdown"
    SCHEDULED = "scheduled"


class ShutdownReasonKind(str, Enum):
    NONE = "none"
    PLANNED = "planned"
    UNPLANNED = "unplanned"


@dataclass(frozen=True)
class ShutdownReasonOption:
    """Motivo do Event Viewer compatível com PsShutdown ``-e [u|p]:xx:yy``."""

    kind: ShutdownReasonKind
    major: int
    minor: int
    title: str
    category: str
    message: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.major}:{self.minor}"

    @property
    def token(self) -> str:
        if self.kind == ShutdownReasonKind.PLANNED:
            return f"p:{self.major}:{self.minor}"
        if self.kind == ShutdownReasonKind.UNPLANNED:
            return f"u:{self.major}:{self.minor}"
        return ""


def _reason(
    kind: ShutdownReasonKind,
    major: int,
    minor: int,
    title: str,
    category: str,
    message: str,
) -> ShutdownReasonOption:
    return ShutdownReasonOption(kind, major, minor, title, category, message)


# Lista curada a partir de ``shutdown /l`` (PT-BR), mapeada para ``p``/``u``.
SHUTDOWN_REASON_OPTIONS: tuple[ShutdownReasonOption, ...] = (
    _reason(
        ShutdownReasonKind.PLANNED,
        0,
        0,
        "Outro",
        "Geral",
        "Manutenção planejada neste computador. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        0,
        0,
        "Outro",
        "Geral",
        "Este computador será reiniciado por um motivo não planejado. Salve o seu trabalho agora.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        0,
        5,
        "Outra falha: o sistema não está respondendo",
        "Geral",
        "O sistema não está respondendo. Será necessário reiniciar. Salve o que puder.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        1,
        1,
        "Manutenção",
        "Hardware",
        "Manutenção de hardware planejada. Salve o seu trabalho e aguarde a conclusão.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        1,
        1,
        "Manutenção",
        "Hardware",
        "Manutenção emergencial de hardware. Salve o seu trabalho imediatamente.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        1,
        2,
        "Instalação",
        "Hardware",
        "Instalação de hardware planejada. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        1,
        2,
        "Instalação",
        "Hardware",
        "Instalação emergencial de hardware. Salve o seu trabalho agora.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        2,
        2,
        "Recuperação",
        "Sistema operacional",
        "Recuperação do sistema operacional planejada. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        2,
        2,
        "Recuperação",
        "Sistema operacional",
        "Recuperação emergencial do sistema. Salve o seu trabalho imediatamente.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        2,
        3,
        "Atualização",
        "Sistema operacional",
        "Atualização do Windows em andamento. Salve o seu trabalho e aguarde o reinício.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        2,
        4,
        "Reconfiguração",
        "Sistema operacional",
        "Reconfiguração do sistema planejada. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        2,
        4,
        "Reconfiguração",
        "Sistema operacional",
        "Reconfiguração emergencial do sistema. Salve o seu trabalho agora.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        2,
        16,
        "Service pack",
        "Sistema operacional",
        "Instalação de service pack do Windows. Salve o seu trabalho e aguarde o reinício.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        2,
        17,
        "Hotfix",
        "Sistema operacional",
        "Instalação de hotfix do Windows. Salve o seu trabalho e aguarde o reinício.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        2,
        17,
        "Hotfix",
        "Sistema operacional",
        "Hotfix urgente do Windows. Salve o seu trabalho imediatamente.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        2,
        18,
        "Correção de segurança",
        "Sistema operacional",
        "Correção de segurança do Windows. Salve o seu trabalho e aguarde o reinício.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        2,
        18,
        "Correção de segurança",
        "Sistema operacional",
        "Correção de segurança urgente. Salve o seu trabalho imediatamente.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        4,
        1,
        "Manutenção",
        "Aplicativo",
        "Manutenção de aplicativo planejada. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        4,
        1,
        "Manutenção",
        "Aplicativo",
        "Manutenção emergencial de aplicativo. Salve o seu trabalho agora.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        4,
        2,
        "Instalação",
        "Aplicativo",
        "Instalação de aplicativo planejada. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        4,
        5,
        "Sem resposta",
        "Aplicativo",
        "Um aplicativo parou de responder. O computador será reiniciado. Salve o que puder.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        4,
        6,
        "Instável",
        "Aplicativo",
        "Um aplicativo está instável. O computador será reiniciado. Salve o seu trabalho.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        5,
        15,
        "Erro de parada",
        "Falha do sistema",
        "Falha crítica do sistema. O computador precisa reiniciar. Salve o que puder.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        5,
        19,
        "Problema de segurança",
        "Segurança",
        "Ação de segurança planejada neste computador. Salve o seu trabalho e aguarde.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        5,
        19,
        "Problema de segurança",
        "Segurança",
        "Ação de segurança urgente. Salve o seu trabalho imediatamente.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        5,
        20,
        "Perda de conectividade de rede",
        "Segurança",
        "Perda de conectividade de rede. O computador será reiniciado. Salve o seu trabalho.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        6,
        11,
        "Fio desconectado",
        "Falha de energia",
        "Falha de energia (cabo). O computador será desligado ou reiniciado. Salve o seu trabalho.",
    ),
    _reason(
        ShutdownReasonKind.UNPLANNED,
        6,
        12,
        "Ambiente",
        "Falha de energia",
        "Falha de energia no ambiente. O computador será desligado ou reiniciado. Salve o seu trabalho.",
    ),
    _reason(
        ShutdownReasonKind.PLANNED,
        7,
        0,
        "Desligamento de API legacy",
        "Legacy",
        "Desligamento solicitado por aplicativo. Salve o seu trabalho e aguarde.",
    ),
)

_REASON_BY_KEY = {item.key: item for item in SHUTDOWN_REASON_OPTIONS}


def reasons_for_kind(kind: ShutdownReasonKind) -> tuple[ShutdownReasonOption, ...]:
    if kind not in (ShutdownReasonKind.PLANNED, ShutdownReasonKind.UNPLANNED):
        return ()
    return tuple(item for item in SHUTDOWN_REASON_OPTIONS if item.kind == kind)


def find_reason_option(
    kind: ShutdownReasonKind,
    major: int = 0,
    minor: int = 0,
) -> Optional[ShutdownReasonOption]:
    return _REASON_BY_KEY.get(f"{kind.value}:{int(major)}:{int(minor)}")


def reason_option_label(option: ShutdownReasonOption) -> str:
    """Título amigável: ``Categoria · detalhe``."""
    title = (option.title or "").strip()
    category = (option.category or "").strip()
    if category and title and category.casefold() != title.casefold():
        return f"{category}  ·  {title}"
    return title or category or option.token


def suggested_reason_message(option: Optional[ShutdownReasonOption]) -> str:
    """Mensagem pronta para ``-m``, limitada ao tamanho do PsShutdown."""
    if option is None:
        return ""
    text = (option.message or "").strip()
    if not text:
        return ""
    if len(text) > MAX_MESSAGE_LENGTH:
        return text[:MAX_MESSAGE_LENGTH].rstrip()
    return text


class PowerPhase(str, Enum):
    COMMAND_SENT = "command_sent"
    COMMAND_ACCEPTED = "command_accepted"
    ACTION_PENDING = "action_pending"
    HOST_UNAVAILABLE = "host_unavailable"
    HOST_RETURNED = "host_returned"
    UNCONFIRMED = "unconfirmed"
    ABORTED = "aborted"
    LOCKED = "locked"
    LOGGED_OFF = "logged_off"
    FAILED = "failed"
    CANCELLED_LOCAL = "cancelled_local"
    STALE_HOST = "stale_host"
    TOOL_MISSING = "tool_missing"


@dataclass(frozen=True)
class ActionProfile:
    action: PowerAction
    label: str
    hint: str
    advanced: bool = False
    supports_countdown: bool = True
    supports_message: bool = True
    supports_force: bool = True
    supports_user_abort: bool = True
    supports_reason: bool = True
    expects_offline: bool = False
    expects_reboot: bool = False
    disrupts_users: bool = True
    default_timing: TimingMode = TimingMode.COUNTDOWN
    default_countdown: int = DEFAULT_COUNTDOWN_SECONDS
    default_allow_abort: bool = True
    logoff_console_only: bool = False


ACTION_PROFILES: dict[PowerAction, ActionProfile] = {
    PowerAction.POWER_OFF: ActionProfile(
        action=PowerAction.POWER_OFF,
        label="Desligar a energia",
        hint=(
            "Envia -k. Se o equipamento não suportar power-off, "
            "o PsShutdown poderá reiniciar."
        ),
        expects_offline=True,
    ),
    PowerAction.RESTART: ActionProfile(
        action=PowerAction.RESTART,
        label="Reiniciar",
        hint="Reinicia o computador remoto após o aviso.",
        expects_offline=True,
        expects_reboot=True,
    ),
    PowerAction.HIBERNATE: ActionProfile(
        action=PowerAction.HIBERNATE,
        label="Hibernar",
        hint="Salva a sessão em disco e desliga.",
        expects_offline=True,
        default_timing=TimingMode.IMMEDIATE,
        default_countdown=0,
        default_allow_abort=False,
    ),
    PowerAction.SUSPEND: ActionProfile(
        action=PowerAction.SUSPEND,
        label="Suspender",
        hint="Depende dos estados de energia suportados pelo computador.",
        expects_offline=True,
        default_timing=TimingMode.IMMEDIATE,
        default_countdown=0,
        default_allow_abort=False,
    ),
    PowerAction.LOCK: ActionProfile(
        action=PowerAction.LOCK,
        label="Bloquear",
        hint="Mantém o computador ligado e bloqueia o console.",
        supports_countdown=False,
        supports_message=False,
        supports_force=False,
        supports_user_abort=False,
        supports_reason=False,
        disrupts_users=False,
        default_timing=TimingMode.IMMEDIATE,
        default_countdown=0,
        default_allow_abort=False,
    ),
    PowerAction.LOGOFF: ActionProfile(
        action=PowerAction.LOGOFF,
        label="Encerrar sessão",
        hint=(
            "O PsShutdown encerra a sessão do console, "
            "não uma sessão RDP selecionada arbitrariamente."
        ),
        supports_countdown=False,
        supports_message=False,
        supports_user_abort=False,
        supports_reason=False,
        default_timing=TimingMode.IMMEDIATE,
        default_countdown=0,
        default_allow_abort=False,
        logoff_console_only=True,
    ),
    PowerAction.ABORT: ActionProfile(
        action=PowerAction.ABORT,
        label="Cancelar ação agendada",
        hint="Só funciona enquanto houver contagem regressiva no host remoto.",
        supports_countdown=False,
        supports_message=False,
        supports_force=False,
        supports_user_abort=False,
        supports_reason=False,
        disrupts_users=False,
        default_timing=TimingMode.IMMEDIATE,
        default_countdown=0,
        default_allow_abort=False,
    ),
    PowerAction.SHUTDOWN_NO_POWEROFF: ActionProfile(
        action=PowerAction.SHUTDOWN_NO_POWEROFF,
        label="Desligar sem power-off",
        hint="Envia -s. Não é o mesmo que desligar a energia (-k).",
        advanced=True,
        expects_offline=True,
    ),
    PowerAction.MONITOR_OFF: ActionProfile(
        action=PowerAction.MONITOR_OFF,
        label="Desligar monitor",
        hint="Pode iniciar Modern Standby quando o equipamento suportar.",
        advanced=True,
        supports_countdown=False,
        supports_message=False,
        supports_force=False,
        supports_user_abort=False,
        supports_reason=False,
        disrupts_users=False,
        default_timing=TimingMode.IMMEDIATE,
        default_countdown=0,
        default_allow_abort=False,
    ),
}

PRIMARY_ACTIONS: tuple[PowerAction, ...] = (
    PowerAction.POWER_OFF,
    PowerAction.RESTART,
    PowerAction.HIBERNATE,
    PowerAction.SUSPEND,
    PowerAction.LOCK,
    PowerAction.LOGOFF,
)
ADVANCED_ACTIONS: tuple[PowerAction, ...] = (
    PowerAction.SHUTDOWN_NO_POWEROFF,
    PowerAction.MONITOR_OFF,
)

PHASE_LABELS = {
    PowerPhase.COMMAND_SENT: "Comando enviado",
    PowerPhase.COMMAND_ACCEPTED: "Comando aceito",
    PowerPhase.ACTION_PENDING: "Ação pendente",
    PowerPhase.HOST_UNAVAILABLE: "Host ficou indisponível",
    PowerPhase.HOST_RETURNED: "Host voltou após reinício",
    PowerPhase.UNCONFIRMED: "Resultado não confirmado",
    PowerPhase.ABORTED: "Ação agendada cancelada",
    PowerPhase.LOCKED: "Console bloqueado",
    PowerPhase.LOGGED_OFF: "Sessão do console encerrada",
    PowerPhase.FAILED: "Falha",
    PowerPhase.CANCELLED_LOCAL: "Processo local interrompido",
    PowerPhase.STALE_HOST: "Host alterado durante a operação",
    PowerPhase.TOOL_MISSING: "PsShutdown ausente",
}

_HOST_MULTI_CHARS = frozenset(",;")
_HOST_EXTRA_INVALID = frozenset("/\\:*?@")
_UPTIME_RE = re.compile(
    r"(?:(?P<days>\d+)\s*days?)?\s*"
    r"(?:(?P<hours>\d+)\s*hours?)?\s*"
    r"(?:(?P<minutes>\d+)\s*minutes?)?\s*"
    r"(?:(?P<seconds>\d+)\s*seconds?)?",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class PowerRequest:
    host: str
    action: PowerAction = PowerAction.RESTART
    timing: TimingMode = TimingMode.COUNTDOWN
    countdown_seconds: int = DEFAULT_COUNTDOWN_SECONDS
    scheduled_hour: int = 18
    scheduled_minute: int = 0
    message: str = ""
    notice_seconds: Optional[int] = None
    allow_user_abort: bool = True
    force: bool = False
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    reason: ShutdownReasonKind = ShutdownReasonKind.PLANNED
    reason_major: int = DEFAULT_REASON_MAJOR
    reason_minor: int = DEFAULT_REASON_MINOR
    accepteula: bool = True


@dataclass(frozen=True)
class PowerClassification:
    phase: PowerPhase
    ok: bool
    title: str
    detail: str
    expected_offline: bool = False


def action_profile(action: PowerAction) -> ActionProfile:
    return ACTION_PROFILES[action]


def resolve_psshutdown_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSSHUTDOWN_NAMES)


def psshutdown_available(pstools_dir: str = "") -> bool:
    path = resolve_psshutdown_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def build_remote_target(host: str) -> str:
    h = normalize_host(host)
    if not h:
        return ""
    return f"\\\\{h}"


def is_multi_host_target(host: str) -> bool:
    raw = (host or "").strip()
    if not raw:
        return False
    if raw.startswith("@") or "@" in raw:
        return True
    stripped = raw.strip("\\").strip()
    if stripped == "*":
        return True
    return any(ch in raw for ch in _HOST_MULTI_CHARS)


def validate_power_host(host: str) -> list[str]:
    """Recusa destino vazio, listas, curingas e @arquivo."""
    errors: list[str] = []
    raw = (host or "").strip()
    if not raw:
        errors.append(
            "Informe um host remoto. Sem destino o PsShutdown atua neste computador."
        )
        return errors
    if is_multi_host_target(raw):
        errors.append(
            "O modo de host único não aceita listas, curingas nem arquivos de destino."
        )
        return errors
    h = normalize_host(raw)
    if not h:
        errors.append(
            "Informe um host remoto. Sem destino o PsShutdown atua neste computador."
        )
        return errors
    if h.startswith("-") or any(ch in h for ch in _HOST_EXTRA_INVALID):
        errors.append("Host inválido.")
        return errors
    if not is_valid_host(h):
        errors.append("Host inválido.")
    return errors


def normalize_power_message(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [part.strip() for part in raw.split("\n")]
    return " ".join(part for part in parts if part)


def format_scheduled_time(hour: int, minute: int) -> str:
    return f"{int(hour)}:{int(minute):02d}"


def parse_scheduled_time(value: str) -> tuple[int, int] | None:
    match = _TIME_RE.fullmatch((value or "").strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def reason_flag(
    kind: ShutdownReasonKind,
    *,
    major: int = DEFAULT_REASON_MAJOR,
    minor: int = DEFAULT_REASON_MINOR,
) -> str:
    try:
        maj = int(major)
        mn = int(minor)
    except (TypeError, ValueError):
        maj, mn = DEFAULT_REASON_MAJOR, DEFAULT_REASON_MINOR
    if maj < 0 or maj >= 256 or mn < 0 or mn >= 65536:
        maj, mn = DEFAULT_REASON_MAJOR, DEFAULT_REASON_MINOR
    if kind == ShutdownReasonKind.PLANNED:
        return f"p:{maj}:{mn}"
    if kind == ShutdownReasonKind.UNPLANNED:
        return f"u:{maj}:{mn}"
    return ""


def reason_display(request: PowerRequest) -> str:
    """Texto curto para confirmação/histórico."""
    if request.reason == ShutdownReasonKind.NONE:
        return "—"
    option = find_reason_option(
        request.reason, request.reason_major, request.reason_minor
    )
    token = reason_flag(
        request.reason,
        major=request.reason_major,
        minor=request.reason_minor,
    )
    if option is None:
        kind_label = (
            "Planejado"
            if request.reason == ShutdownReasonKind.PLANNED
            else "Não planejado"
        )
        return f"{kind_label} ({token})" if token else kind_label
    return f"{reason_option_label(option)} ({token})"


def effective_countdown_seconds(request: PowerRequest) -> Optional[int]:
    profile = action_profile(request.action)
    if not profile.supports_countdown:
        return None
    if request.timing == TimingMode.SCHEDULED:
        return None
    if request.timing == TimingMode.IMMEDIATE:
        return 0
    try:
        return max(MIN_COUNTDOWN_SECONDS, int(request.countdown_seconds))
    except (TypeError, ValueError):
        return DEFAULT_COUNTDOWN_SECONDS


def effective_timing_token(request: PowerRequest) -> Optional[str]:
    profile = action_profile(request.action)
    if not profile.supports_countdown:
        return None
    if request.timing == TimingMode.SCHEDULED:
        return format_scheduled_time(request.scheduled_hour, request.scheduled_minute)
    seconds = effective_countdown_seconds(request)
    if seconds is None:
        return None
    if request.timing == TimingMode.IMMEDIATE:
        return "0"
    return str(int(seconds))


def validate_power_request(request: PowerRequest) -> list[str]:
    errors = list(validate_power_host(request.host))
    try:
        PowerAction(request.action)
    except ValueError:
        errors.append("Ação de energia inválida.")
        return errors
    profile = action_profile(request.action)
    message = normalize_power_message(request.message)
    if len(message) > MAX_MESSAGE_LENGTH:
        errors.append(
            f"A mensagem excede o limite de {MAX_MESSAGE_LENGTH} caracteres."
        )

    if profile.supports_countdown:
        if request.timing == TimingMode.COUNTDOWN:
            try:
                seconds = int(request.countdown_seconds)
            except (TypeError, ValueError):
                errors.append("Contagem regressiva inválida.")
            else:
                if seconds < MIN_COUNTDOWN_SECONDS or seconds > MAX_COUNTDOWN_SECONDS:
                    errors.append(
                        "Contagem regressiva deve estar entre "
                        f"{MIN_COUNTDOWN_SECONDS} e {MAX_COUNTDOWN_SECONDS} segundos."
                    )
                elif seconds > 0 and not message:
                    errors.append("Informe uma mensagem quando houver contagem regressiva.")
        elif request.timing == TimingMode.SCHEDULED:
            try:
                hour = int(request.scheduled_hour)
                minute = int(request.scheduled_minute)
            except (TypeError, ValueError):
                errors.append("Horário agendado inválido.")
            else:
                if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                    errors.append("Horário agendado inválido.")
                elif not message:
                    errors.append("Informe uma mensagem quando a ação estiver agendada.")
        elif request.timing != TimingMode.IMMEDIATE:
            errors.append("Modo de agendamento inválido.")

    if request.notice_seconds is not None:
        try:
            notice = int(request.notice_seconds)
        except (TypeError, ValueError):
            errors.append("Tempo de exibição do aviso inválido.")
        else:
            if notice < 0 or notice > MAX_COUNTDOWN_SECONDS:
                errors.append("Tempo de exibição do aviso inválido.")

    try:
        timeout = int(request.connect_timeout)
    except (TypeError, ValueError):
        errors.append("Timeout de conexão inválido.")
    else:
        if timeout < 1 or timeout > MAX_CONNECT_TIMEOUT_SECONDS:
            errors.append(
                "Timeout de conexão deve estar entre "
                f"1 e {MAX_CONNECT_TIMEOUT_SECONDS} segundos."
            )
    return errors


def apply_action_defaults(request: PowerRequest) -> PowerRequest:
    """Ajusta flags incompatíveis com a ação, sem inventar host."""
    profile = action_profile(request.action)
    timing = request.timing if profile.supports_countdown else TimingMode.IMMEDIATE
    countdown = request.countdown_seconds if profile.supports_countdown else 0
    message = request.message if profile.supports_message else ""
    allow_abort = bool(request.allow_user_abort) if profile.supports_user_abort else False
    force = bool(request.force) if profile.supports_force else False
    reason = request.reason if profile.supports_reason else ShutdownReasonKind.NONE
    notice = request.notice_seconds if profile.supports_message else None
    if timing == TimingMode.IMMEDIATE:
        allow_abort = False
        countdown = 0
    major = int(request.reason_major) if reason != ShutdownReasonKind.NONE else 0
    minor = int(request.reason_minor) if reason != ShutdownReasonKind.NONE else 0
    return replace(
        request,
        timing=timing,
        countdown_seconds=countdown,
        message=message,
        allow_user_abort=allow_abort,
        force=force,
        reason=reason,
        reason_major=major,
        reason_minor=minor,
        notice_seconds=notice,
    )


def build_psshutdown_argv(
    exe: str,
    request: PowerRequest,
    *,
    user: str = "",
    password: str = "",
    include_password: bool = True,
) -> list[str]:
    """Monta argv sem shell. Retorna lista vazia se o host for recusado."""
    request = apply_action_defaults(request)
    if validate_power_request(request):
        return []
    if not (exe or "").strip():
        return []
    target = build_remote_target(request.host)
    if not target:
        return []
    argv = [exe]
    if request.accepteula:
        argv.append("-accepteula")
    argv.append(target)
    user_name = (user or "").strip()
    if user_name:
        argv.extend(["-u", user_name])
        secret = (password or "").strip()
        if secret:
            argv.extend(["-p", secret if include_password else "********"])
    argv.append(request.action.flag)
    if request.force:
        argv.append("-f")
    if request.allow_user_abort:
        argv.append("-c")
    argv.extend(["-n", str(int(request.connect_timeout))])
    token = effective_timing_token(request)
    if token is not None:
        argv.extend(["-t", token])
    reason = reason_flag(
        request.reason,
        major=request.reason_major,
        minor=request.reason_minor,
    )
    if reason:
        argv.extend(["-e", reason])
    message = normalize_power_message(request.message)
    if message:
        argv.extend(["-m", message])
    if request.notice_seconds is not None:
        argv.extend(["-v", str(int(request.notice_seconds))])
    return argv


def preview_psshutdown_argv(argv: Sequence[str]) -> str:
    return format_argv_for_display(argv)


def local_process_timeout_s(request: PowerRequest) -> float:
    try:
        connect = float(int(request.connect_timeout))
    except (TypeError, ValueError):
        connect = float(DEFAULT_CONNECT_TIMEOUT_SECONDS)
    return max(LOCAL_PROCESS_TIMEOUT_FLOOR, connect + 30.0)


def confirmation_summary(
    request: PowerRequest,
    *,
    sessions_note: str = "",
) -> str:
    request = apply_action_defaults(request)
    profile = action_profile(request.action)
    host = normalize_host(request.host)
    lines = [
        f"Ação: {profile.label} ({request.action.flag})",
        f"Host: {host}",
    ]
    token = effective_timing_token(request)
    if request.timing == TimingMode.SCHEDULED:
        lines.append(f"Horário: {token} (relógio 24 h do host)")
    elif token == "0" or token is None:
        lines.append("Horário: imediato")
    else:
        lines.append(f"Contagem: {token} s")
    message = normalize_power_message(request.message)
    lines.append(f"Mensagem: {message or '—'}")
    if profile.supports_reason and request.reason != ShutdownReasonKind.NONE:
        lines.append(f"Motivo: {reason_display(request)}")
    lines.append(f"Forçar aplicativos (-f): {'sim' if request.force else 'não'}")
    if request.allow_user_abort:
        lines.append("O usuário remoto poderá cancelar a contagem (-c).")
    if profile.logoff_console_only:
        lines.append(profile.hint)
    if sessions_note:
        lines.append(sessions_note)
    return "\n".join(lines)


def parse_uptime_seconds(text: str) -> Optional[int]:
    raw = (text or "").strip()
    if not raw:
        return None
    match = _UPTIME_RE.search(raw)
    if not match or not any(
        match.group(name) for name in ("days", "hours", "minutes", "seconds")
    ):
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def looks_like_usage(text: str) -> bool:
    t = (text or "").lower()
    return "usage:" in t and "psshutdown" in t


def classify_psshutdown_output(
    text: str,
    *,
    returncode: int = 0,
    action: PowerAction = PowerAction.RESTART,
    timed_out: bool = False,
    cancelled: bool = False,
    spawn_error: str = "",
) -> PowerClassification:
    profile = action_profile(action)
    detail = (text or "").strip()
    if spawn_error:
        low = spawn_error.lower()
        if "não encontrado" in low or "not found" in low:
            return PowerClassification(
                PowerPhase.TOOL_MISSING,
                False,
                PHASE_LABELS[PowerPhase.TOOL_MISSING],
                spawn_error,
            )
        return PowerClassification(
            PowerPhase.FAILED,
            False,
            PHASE_LABELS[PowerPhase.FAILED],
            spawn_error,
        )
    if cancelled:
        return PowerClassification(
            PowerPhase.CANCELLED_LOCAL,
            False,
            PHASE_LABELS[PowerPhase.CANCELLED_LOCAL],
            "O processo local do PsShutdown foi interrompido. "
            "Isso não cancela uma ação já aceita pelo computador remoto.",
        )
    if timed_out:
        return PowerClassification(
            PowerPhase.UNCONFIRMED,
            False,
            PHASE_LABELS[PowerPhase.UNCONFIRMED],
            "O PsShutdown excedeu o tempo limite local. "
            "A ação remota pode ter sido aceita.",
        )

    low = detail.lower()
    if looks_like_usage(detail):
        return PowerClassification(
            PowerPhase.FAILED,
            False,
            PHASE_LABELS[PowerPhase.FAILED],
            "O PsShutdown devolveu a tela de Usage. Verifique os parâmetros.",
        )
    if any(
        marker in low
        for marker in (
            "access is denied",
            "acesso negado",
            "logon failure",
            "falha de logon",
            "unknown user name",
            "wrong password",
        )
    ):
        return PowerClassification(
            PowerPhase.FAILED,
            False,
            PHASE_LABELS[PowerPhase.FAILED],
            detail or "Acesso negado.",
        )
    if action == PowerAction.ABORT and any(
        marker in low
        for marker in (
            "no shutdown is in progress",
            "não há desligamento",
            "nao ha desligamento",
            "unable to abort",
        )
    ):
        return PowerClassification(
            PowerPhase.FAILED,
            False,
            PHASE_LABELS[PowerPhase.FAILED],
            detail or "Não há contagem regressiva para cancelar neste host.",
        )
    if any(
        marker in low
        for marker in (
            "network path was not found",
            "rpc server is unavailable",
            "servidor rpc",
            "could not connect",
            "unable to connect",
            "the network name cannot be found",
        )
    ):
        return PowerClassification(
            PowerPhase.FAILED,
            False,
            PHASE_LABELS[PowerPhase.FAILED],
            detail or "Não foi possível conectar ao host remoto.",
        )

    accepted = returncode == 0 or any(
        marker in low
        for marker in (
            "initiated",
            "initiating",
            "is shutting down",
            "has been locked",
            "lock initiated",
            "logged off",
            "logoff",
            "aborted",
            "aborting",
            "hibernate",
            "standby",
            "suspended",
        )
    )
    if action == PowerAction.ABORT and (
        returncode == 0 or "abort" in low
    ):
        return PowerClassification(
            PowerPhase.ABORTED,
            True,
            PHASE_LABELS[PowerPhase.ABORTED],
            detail or "A ação agendada foi cancelada no host remoto.",
        )
    if action == PowerAction.LOCK and accepted:
        return PowerClassification(
            PowerPhase.LOCKED,
            True,
            PHASE_LABELS[PowerPhase.LOCKED],
            detail or "O console remoto foi bloqueado.",
            expected_offline=False,
        )
    if action == PowerAction.LOGOFF and accepted:
        return PowerClassification(
            PowerPhase.LOGGED_OFF,
            True,
            PHASE_LABELS[PowerPhase.LOGGED_OFF],
            detail or "A sessão do console foi encerrada.",
        )
    if accepted:
        pending = bool(
            profile.supports_countdown
            and profile.disrupts_users
        )
        phase = PowerPhase.ACTION_PENDING if pending else PowerPhase.COMMAND_ACCEPTED
        return PowerClassification(
            phase,
            True,
            PHASE_LABELS[phase],
            detail or "O host remoto aceitou o comando.",
            expected_offline=profile.expects_offline,
        )
    if returncode != 0:
        return PowerClassification(
            PowerPhase.FAILED,
            False,
            PHASE_LABELS[PowerPhase.FAILED],
            detail or f"PsShutdown terminou com código {returncode}.",
        )
    return PowerClassification(
        PowerPhase.COMMAND_SENT,
        True,
        PHASE_LABELS[PowerPhase.COMMAND_SENT],
        detail or "Comando enviado ao PsShutdown.",
        expected_offline=profile.expects_offline,
    )


def phase_is_success_offline(phase: PowerPhase, action: PowerAction) -> bool:
    """True se o host indisponível confirma a ação, em vez de indicar erro."""
    profile = action_profile(action)
    return profile.expects_offline and phase in (
        PowerPhase.COMMAND_ACCEPTED,
        PowerPhase.ACTION_PENDING,
        PowerPhase.COMMAND_SENT,
        PowerPhase.HOST_UNAVAILABLE,
    )


def history_detail(
    request: PowerRequest,
    classification: PowerClassification,
    *,
    operator: str = "",
    followup: str = "",
) -> str:
    request = apply_action_defaults(request)
    token = effective_timing_token(request) or "none"
    parts = [
        f"host={normalize_host(request.host)}",
        f"action={request.action.name.lower()}",
        f"flag={request.action.flag}",
        f"timing={request.timing.value}",
        f"countdown={token}",
        f"force={int(bool(request.force))}",
        f"allow_abort={int(bool(request.allow_user_abort))}",
        f"reason={request.reason.value}",
        f"reason_code={reason_flag(request.reason, major=request.reason_major, minor=request.reason_minor) or 'none'}",
        f"phase={classification.phase.value}",
        f"ok={int(bool(classification.ok))}",
    ]
    if operator:
        parts.append(f"operator={operator}")
    if followup:
        parts.append(f"followup={followup}")
    return " ".join(parts)
