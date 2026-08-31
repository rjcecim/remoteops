"""Execução remota de energia via PsShutdown.

Captura stdout/stderr sem shell, limpa credenciais após o uso e acompanha
o host por TCP 445. Matar o processo local NÃO cancela a ação remota.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from typing import Callable, List, Optional, Sequence

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.process_runner import CapturedProcess, CancelFn, run_argv_captured
from remoteops.utils.app_logging import log_operation
from remoteops.utils.host_reachability import is_stale_host_result, probe_tcp_445
from remoteops.utils.ping import normalize_host
from remoteops.utils.psshutdown import (
    FOLLOWUP_OFFLINE_GRACE_SECONDS,
    FOLLOWUP_ONLINE_WAIT_SECONDS,
    FOLLOWUP_POLL_SECONDS,
    PHASE_LABELS,
    REBOOT_UPTIME_CONFIRM_SECONDS,
    PowerAction,
    PowerClassification,
    PowerPhase,
    PowerRequest,
    TimingMode,
    action_profile,
    apply_action_defaults,
    build_psshutdown_argv,
    classify_psshutdown_output,
    effective_countdown_seconds,
    history_detail,
    local_process_timeout_s,
    parse_uptime_seconds,
    preview_psshutdown_argv,
    psshutdown_available,
    resolve_psshutdown_exe,
    validate_power_request,
)
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import redact_command_text
from remoteops.utils.sessions import RemoteSession, list_remote_sessions

RunnerFn = Callable[..., CapturedProcess]
TcpProbeFn = Callable[..., object]
ProgressFn = Callable[[str], None]


@dataclass
class PowerResult:
    host: str
    action: PowerAction
    classification: PowerClassification
    argv_preview: str = ""
    exit_code: Optional[int] = None
    output: str = ""
    timed_out: bool = False
    cancelled_local: bool = False
    stale: bool = False
    tcp_went_offline: bool = False
    tcp_came_back: bool = False
    uptime_seconds: Optional[int] = None
    followup_phase: Optional[PowerPhase] = None
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.classification.ok) and not self.stale and not self.errors

    @property
    def phase(self) -> PowerPhase:
        return self.classification.phase


def current_operator() -> str:
    for key in ("USERNAME", "USER"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    try:
        return (os.getlogin() or "").strip()
    except Exception:
        return ""


def list_power_sessions(
    host: str,
    *,
    user: str = "",
    password: str = "",
) -> tuple[List[RemoteSession], str]:
    return list_remote_sessions(host, user=user, password=password)


def interactive_sessions(sessions: Sequence[RemoteSession]) -> List[RemoteSession]:
    out: List[RemoteSession] = []
    for item in sessions:
        name = (item.name or "").strip().casefold()
        if name in ("services",):
            continue
        user = (item.username or "").strip()
        if not user:
            continue
        state = (item.state or "").strip().casefold()
        if state in (
            "desconectada",
            "disconnected",
            "listen",
            "down",
            "reset",
            "init",
            "idle",
            "ociosa",
        ):
            continue
        out.append(item)
    return out


def sessions_warning(sessions: Sequence[RemoteSession]) -> str:
    active = interactive_sessions(sessions)
    if not active:
        return ""
    labels = "; ".join(item.label() for item in active[:8])
    extra = "" if len(active) <= 8 else f" (+{len(active) - 8})"
    return f"Há {len(active)} sessão(ões) interativa(s): {labels}{extra}."


class PowerService:
    """Valida, executa e acompanha uma ação de energia em um único host."""

    def run(
        self,
        request: PowerRequest,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        should_cancel: Optional[CancelFn] = None,
        current_host: str = "",
        follow_up: bool = False,
        runner: Optional[RunnerFn] = None,
        tcp_probe: Optional[TcpProbeFn] = None,
        on_progress: Optional[ProgressFn] = None,
        operator: str = "",
    ) -> PowerResult:
        request = apply_action_defaults(request)
        host = normalize_host(request.host)
        errors = validate_power_request(request)
        if errors:
            failed = PowerClassification(
                PowerPhase.FAILED,
                False,
                PHASE_LABELS[PowerPhase.FAILED],
                errors[0],
            )
            return PowerResult(
                host=host,
                action=request.action,
                classification=failed,
                errors=errors,
            )

        wanted = host
        visible = normalize_host(current_host) or host
        if is_stale_host_result(host, wanted, visible):
            return self._stale_result(request, host)

        tool_dir = pstools_dir or get_pstools_dir()
        exe = resolve_psshutdown_exe(tool_dir)
        if not psshutdown_available(tool_dir):
            missing = PowerClassification(
                PowerPhase.TOOL_MISSING,
                False,
                PHASE_LABELS[PowerPhase.TOOL_MISSING],
                "PsShutdown não encontrado na pasta PSTools configurada.",
            )
            return PowerResult(
                host=host,
                action=request.action,
                classification=missing,
                errors=[missing.detail],
            )

        argv = build_psshutdown_argv(
            exe,
            request,
            user=user,
            password=password,
            include_password=True,
        )
        preview = preview_psshutdown_argv(argv)
        if not argv:
            failed = PowerClassification(
                PowerPhase.FAILED,
                False,
                PHASE_LABELS[PowerPhase.FAILED],
                "Não foi possível montar o comando PsShutdown.",
            )
            return PowerResult(
                host=host,
                action=request.action,
                classification=failed,
                argv_preview=preview,
                errors=[failed.detail],
            )

        passwords = [password] if (password or "").strip() else None
        log_operation(
            "power",
            detail=history_detail(
                request,
                PowerClassification(
                    PowerPhase.COMMAND_SENT,
                    True,
                    PHASE_LABELS[PowerPhase.COMMAND_SENT],
                    "",
                ),
                operator=operator or current_operator(),
            ),
            passwords=passwords,
        )
        if on_progress:
            on_progress(f"Enviando {request.action.flag} para {host}…")

        run_fn = runner or run_argv_captured
        try:
            captured = run_fn(
                argv,
                timeout_s=local_process_timeout_s(request),
                should_cancel=should_cancel,
            )
        except TypeError:
            captured = run_fn(argv)
        except Exception as exc:
            captured = CapturedProcess(
                spawn_error=str(exc) or "Falha ao iniciar o PsShutdown."
            )

        output = _combine_output(captured)
        if passwords:
            output = redact_command_text(output, passwords=passwords)
        classification = classify_psshutdown_output(
            output,
            returncode=int(captured.returncode),
            action=request.action,
            timed_out=bool(captured.timed_out),
            cancelled=bool(captured.cancelled),
            spawn_error=captured.spawn_error or "",
        )
        result = PowerResult(
            host=host,
            action=request.action,
            classification=classification,
            argv_preview=preview,
            exit_code=int(captured.returncode),
            output=output,
            timed_out=bool(captured.timed_out),
            cancelled_local=bool(captured.cancelled),
        )

        visible_now = normalize_host(current_host) or host
        if is_stale_host_result(host, wanted, visible_now):
            return replace(
                result,
                stale=True,
                classification=PowerClassification(
                    PowerPhase.STALE_HOST,
                    False,
                    PHASE_LABELS[PowerPhase.STALE_HOST],
                    "O host da aba PsExec mudou durante a operação. O resultado foi ignorado.",
                ),
            )

        if follow_up and classification.ok and not result.cancelled_local:
            result = self._follow_up(
                request,
                result,
                should_cancel=should_cancel,
                tcp_probe=tcp_probe,
                on_progress=on_progress,
                user=user,
                password=password,
                pstools_dir=tool_dir,
            )

        log_operation(
            "power",
            detail=history_detail(
                request,
                result.classification,
                operator=operator or current_operator(),
                followup=(result.followup_phase.value if result.followup_phase else ""),
            ),
            exit_code=result.exit_code,
            passwords=passwords,
        )
        return result

    def _stale_result(self, request: PowerRequest, host: str) -> PowerResult:
        classification = PowerClassification(
            PowerPhase.STALE_HOST,
            False,
            PHASE_LABELS[PowerPhase.STALE_HOST],
            "O host da aba PsExec mudou durante a operação. O resultado foi ignorado.",
        )
        return PowerResult(
            host=host,
            action=request.action,
            classification=classification,
            stale=True,
        )

    def _follow_up(
        self,
        request: PowerRequest,
        result: PowerResult,
        *,
        should_cancel: Optional[CancelFn],
        tcp_probe: Optional[TcpProbeFn],
        on_progress: Optional[ProgressFn],
        user: str,
        password: str,
        pstools_dir: str,
    ) -> PowerResult:
        profile = action_profile(request.action)
        if request.action in (PowerAction.ABORT, PowerAction.LOCK, PowerAction.MONITOR_OFF):
            return result
        if request.timing == TimingMode.SCHEDULED:
            pending = PowerClassification(
                PowerPhase.ACTION_PENDING,
                True,
                PHASE_LABELS[PowerPhase.ACTION_PENDING],
                "Ação agendada: o acompanhamento automático começa só após a hora marcada.",
                expected_offline=profile.expects_offline,
            )
            return replace(
                result,
                classification=pending,
                followup_phase=PowerPhase.ACTION_PENDING,
            )
        if not profile.expects_offline:
            return result

        countdown = effective_countdown_seconds(request) or 0
        if countdown > 0:
            if on_progress:
                on_progress(
                    f"Ação pendente: aguardando a contagem de {countdown} s no host…"
                )
            if not _wait(countdown, should_cancel=should_cancel, on_progress=on_progress):
                return replace(
                    result,
                    cancelled_local=True,
                    classification=PowerClassification(
                        PowerPhase.CANCELLED_LOCAL,
                        False,
                        PHASE_LABELS[PowerPhase.CANCELLED_LOCAL],
                        "Acompanhamento interrompido. A ação remota pode continuar na contagem.",
                    ),
                    followup_phase=PowerPhase.CANCELLED_LOCAL,
                )

        if on_progress:
            on_progress("Verificando TCP 445: aguardando o host ficar offline…")
        offline_wait = max(
            FOLLOWUP_OFFLINE_GRACE_SECONDS,
            int(countdown) + FOLLOWUP_OFFLINE_GRACE_SECONDS,
        )
        went_offline = _wait_tcp_state(
            request.host,
            want_online=False,
            timeout_s=offline_wait,
            should_cancel=should_cancel,
            tcp_probe=tcp_probe,
        )
        if went_offline is None:
            return replace(
                result,
                cancelled_local=True,
                classification=PowerClassification(
                    PowerPhase.CANCELLED_LOCAL,
                    False,
                    PHASE_LABELS[PowerPhase.CANCELLED_LOCAL],
                    "Acompanhamento interrompido. Use Cancelar ação agendada se a contagem continuar.",
                ),
                followup_phase=PowerPhase.CANCELLED_LOCAL,
            )
        if not went_offline:
            unconfirmed = PowerClassification(
                PowerPhase.UNCONFIRMED,
                False,
                PHASE_LABELS[PowerPhase.UNCONFIRMED],
                "O host continuou acessível na porta TCP 445 após a janela esperada.",
                expected_offline=True,
            )
            return replace(
                result,
                classification=unconfirmed,
                followup_phase=PowerPhase.UNCONFIRMED,
            )

        unavailable = PowerClassification(
            PowerPhase.HOST_UNAVAILABLE,
            True,
            PHASE_LABELS[PowerPhase.HOST_UNAVAILABLE],
            "O host ficou indisponível na porta TCP 445, como esperado para esta ação.",
            expected_offline=True,
        )
        result = replace(
            result,
            classification=unavailable,
            tcp_went_offline=True,
            followup_phase=PowerPhase.HOST_UNAVAILABLE,
        )
        if not profile.expects_reboot:
            return result

        if on_progress:
            on_progress("Aguardando o host voltar após o reinício…")
        came_back = _wait_tcp_state(
            request.host,
            want_online=True,
            timeout_s=FOLLOWUP_ONLINE_WAIT_SECONDS,
            should_cancel=should_cancel,
            tcp_probe=tcp_probe,
        )
        if came_back is None:
            return replace(
                result,
                cancelled_local=True,
                followup_phase=PowerPhase.CANCELLED_LOCAL,
            )
        if not came_back:
            unconfirmed = PowerClassification(
                PowerPhase.UNCONFIRMED,
                False,
                PHASE_LABELS[PowerPhase.UNCONFIRMED],
                "O host ficou offline, mas não voltou na porta TCP 445 dentro do tempo de espera.",
                expected_offline=True,
            )
            return replace(
                result,
                classification=unconfirmed,
                followup_phase=PowerPhase.UNCONFIRMED,
            )

        uptime = _query_uptime(
            request.host,
            user=user,
            password=password,
            pstools_dir=pstools_dir,
            should_cancel=should_cancel,
        )
        if uptime is not None and uptime <= REBOOT_UPTIME_CONFIRM_SECONDS:
            returned = PowerClassification(
                PowerPhase.HOST_RETURNED,
                True,
                PHASE_LABELS[PowerPhase.HOST_RETURNED],
                f"O host voltou após o reinício (tempo ligado ≈ {uptime} s).",
                expected_offline=True,
            )
        else:
            extra = ""
            if uptime is None:
                extra = " Não foi possível confirmar o tempo de inicialização pelo PsInfo."
            else:
                extra = f" Tempo ligado reportado: {uptime} s."
            returned = PowerClassification(
                PowerPhase.HOST_RETURNED,
                True,
                PHASE_LABELS[PowerPhase.HOST_RETURNED],
                "O host voltou a responder na porta TCP 445." + extra,
                expected_offline=True,
            )
        return replace(
            result,
            classification=returned,
            tcp_came_back=True,
            uptime_seconds=uptime,
            followup_phase=PowerPhase.HOST_RETURNED,
        )


def _combine_output(captured: CapturedProcess) -> str:
    return (
        decode_console_bytes(captured.stdout or b"")
        + "\n"
        + decode_console_bytes(captured.stderr or b"")
    ).strip()


def _cancelled(should_cancel: Optional[CancelFn]) -> bool:
    return bool(should_cancel and should_cancel())


def _wait(
    seconds: float,
    *,
    should_cancel: Optional[CancelFn],
    on_progress: Optional[ProgressFn] = None,
    tick: float = FOLLOWUP_POLL_SECONDS,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        if _cancelled(should_cancel):
            return False
        remaining = max(0.0, deadline - time.monotonic())
        if on_progress and remaining > 1:
            on_progress(f"Aguardando {int(remaining)} s…")
        time.sleep(min(tick, remaining if remaining > 0 else tick))
    return not _cancelled(should_cancel)


def _tcp_online(
    host: str,
    *,
    tcp_probe: Optional[TcpProbeFn],
) -> Optional[bool]:
    probe = tcp_probe or probe_tcp_445
    try:
        result = probe(host, use_cache=False, log=False)
    except TypeError:
        try:
            result = probe(host)
        except Exception:
            return None
    except Exception:
        return None
    ok = getattr(result, "ok", None)
    if ok is None:
        return None
    return bool(ok)


def _wait_tcp_state(
    host: str,
    *,
    want_online: bool,
    timeout_s: float,
    should_cancel: Optional[CancelFn],
    tcp_probe: Optional[TcpProbeFn],
) -> Optional[bool]:
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    while time.monotonic() < deadline:
        if _cancelled(should_cancel):
            return None
        online = _tcp_online(host, tcp_probe=tcp_probe)
        if online is not None and bool(online) == bool(want_online):
            return True
        time.sleep(FOLLOWUP_POLL_SECONDS)
    if _cancelled(should_cancel):
        return None
    return False


def _query_uptime(
    host: str,
    *,
    user: str,
    password: str,
    pstools_dir: str,
    should_cancel: Optional[CancelFn],
) -> Optional[int]:
    if _cancelled(should_cancel):
        return None
    try:
        from remoteops.utils.psinfo import build_psinfo_argv, parse_psinfo_output
        from remoteops.utils.pstools import resolve_pstools_tool
    except Exception:
        return None
    exe = resolve_pstools_tool(
        pstools_dir or get_pstools_dir(),
        ("PsInfo64.exe", "PsInfo.exe"),
    )
    if not exe or not os.path.isfile(exe):
        return None
    argv = build_psinfo_argv(
        exe,
        host,
        include_disks=False,
        include_hotfixes=False,
        include_software=False,
        nobanner=True,
        user=user,
        password=password,
    )
    if not argv:
        return None
    try:
        captured = run_argv_captured(argv, timeout_s=45.0, should_cancel=should_cancel)
    except Exception:
        return None
    if captured.cancelled or captured.timed_out or captured.spawn_error:
        return None
    text = _combine_output(captured)
    try:
        parsed = parse_psinfo_output(text, host=host)
    except Exception:
        return parse_uptime_seconds(text)
    uptime_text = (parsed.system or {}).get("Uptime") or ""
    return parse_uptime_seconds(uptime_text) or parse_uptime_seconds(text)
