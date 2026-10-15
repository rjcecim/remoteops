"""Verificação estruturada de conectividade do host (ICMP + TCP 445).

Sem Qt. Usa PsPing quando disponível. Se um probe do PsPing falhar
(timeout, inacessível, recusa, erro), tenta ping.exe / socket TCP com a
mesma classificação — sem marcar verificação limitada. Se o PsPing estiver
ausente, usa só ping.exe + socket (verificação limitada).

TCP 445 acessível confirma rede, não autenticação nem permissão do PsExec.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from remoteops.utils.ping import IcmpPingKind, is_valid_host, normalize_host, ping_host_status
from remoteops.utils.psping import (
    PSEXEC_TCP_PORT,
    CancelFn,
    IpFamily,
    PsPingMode,
    PsPingResult,
    PsPingState,
    RunnerFn,
    family_for_host,
    get_cached_psping,
    psping_available,
    run_psping,
    store_cached_psping,
)

PSPING_MISSING_CAPTION = "PsPing ausente — verificação limitada"
CAPTION_NETWORK_READY = "Rede acessível"
CAPTION_ICMP_BLOCKED = "Rede acessível — ICMP indisponível"
CAPTION_TCP_UNAVAILABLE = "TCP 445 indisponível"
CAPTION_OFFLINE = "Host inacessível"
CAPTION_UNRESOLVED = "Nome não resolvido"
CAPTION_INVALID = "Host inválido"
CAPTION_CHECK_FAILED = "Falha na verificação"
# Nome antigo: a verificação nunca significou que o PsExec estava autorizado.
CAPTION_PSEXEC_READY = CAPTION_NETWORK_READY
NATIVE_TCP_TIMEOUT_S = 3.0
_AUTH_NOTE = (
    "Isso confirma só a conectividade de rede. "
    "Não confirma autenticação nem permissão para executar o PsExec."
)


class HostAccessKind(str, Enum):
    PSEXEC_READY = "psexec_ready"
    ICMP_BLOCKED = "icmp_blocked"
    TCP_UNAVAILABLE = "tcp_unavailable"
    OFFLINE = "offline"
    UNRESOLVED = "unresolved"
    INVALID_HOST = "invalid_host"
    CHECK_FAILED = "check_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class HostReachability:
    host: str
    kind: HostAccessKind
    caption: str
    status_state: str
    psexec_enabled: bool
    icmp: Optional[PsPingResult] = None
    tcp: Optional[PsPingResult] = None
    limited: bool = False
    cancelled: bool = False
    tooltip: str = ""

    @property
    def icmp_ok(self) -> Optional[bool]:
        if self.icmp is None:
            return None
        if self.icmp.state in (PsPingState.CANCELLED, PsPingState.TOOL_MISSING):
            return None
        return self.icmp.ok

    @property
    def tcp_ok(self) -> Optional[bool]:
        if self.tcp is None:
            return None
        if self.tcp.state in (PsPingState.CANCELLED, PsPingState.TOOL_MISSING):
            return None
        return self.tcp.ok


@dataclass(frozen=True)
class BatchConnectivityDecision:
    continue_inventory: bool
    online: bool
    icmp_blocked: bool
    log_message: str


def is_stale_host_result(result_host: str, wanted_host: str, current_host: str) -> bool:
    """True se o resultado não corresponde ao host ainda visível na UI."""
    result = normalize_host(result_host).casefold()
    wanted = normalize_host(wanted_host).casefold()
    current = normalize_host(current_host).casefold()
    if not result:
        return True
    return result != wanted or result != current


def has_fresh_tcp445(
    host: str,
    *,
    family: Optional[IpFamily] = None,
    port: int = PSEXEC_TCP_PORT,
) -> bool:
    resolved = family if family is not None else family_for_host(host)
    cached = get_cached_psping(host, PsPingMode.TCP, port, resolved)
    return bool(cached is not None and cached.ok)


def psexec_tcp_blocked_message(*, icmp_ok: Optional[bool] = None) -> str:
    if icmp_ok is False:
        head = (
            "O host não responde ao ICMP e a porta TCP 445 não está acessível."
        )
    elif icmp_ok is True:
        head = (
            "O host responde à rede, mas a porta TCP 445 não está acessível."
        )
    else:
        head = "A porta TCP 445 não está acessível."
    return (
        f"{head}\n\n"
        "Verifique o Firewall do Windows, o SMB e os compartilhamentos "
        "administrativos antes de executar o PsExec."
    )


def _pstools_folder(pstools_dir: Optional[str] = None) -> str:
    from remoteops.utils.pstools import get_pstools_dir

    return (pstools_dir if pstools_dir is not None else get_pstools_dir()) or ""


def _probe_line(label: str, result: Optional[PsPingResult]) -> str:
    if result is None:
        return f"{label}: não testado"
    text = (result.message or result.caption or "").strip()
    return f"{label}: {text}" if text else f"{label}: {result.caption}"


def _probe_summary(
    kind: HostAccessKind,
    *,
    icmp: Optional[PsPingResult],
    tcp: Optional[PsPingResult],
) -> str:
    if kind in (HostAccessKind.INVALID_HOST, HostAccessKind.CANCELLED):
        return ""
    lines: list[str] = []
    if icmp is not None or tcp is not None:
        lines.append(_probe_line("ICMP", icmp))
        port = tcp.port if tcp is not None and tcp.port else PSEXEC_TCP_PORT
        lines.append(_probe_line(f"TCP {port}", tcp))
    if kind in (HostAccessKind.PSEXEC_READY, HostAccessKind.ICMP_BLOCKED):
        lines.append(_AUTH_NOTE)
    elif kind == HostAccessKind.TCP_UNAVAILABLE:
        lines.append(
            "A porta TCP 445 não aceitou conexão. "
            "O computador pode estar ligado com essa porta filtrada."
        )
    elif kind == HostAccessKind.OFFLINE:
        lines.append("ICMP e TCP 445 não responderam.")
    elif kind == HostAccessKind.CHECK_FAILED:
        lines.append(
            "A verificação não terminou de forma conclusiva. "
            "Isso não significa que o host esteja desligado."
        )
    elif kind == HostAccessKind.UNRESOLVED:
        lines.append("O nome não foi resolvido.")
    return "\n".join(line for line in lines if line)


def _tooltip_for(caption: str, *, limited: bool, extra: str = "") -> str:
    parts = [caption]
    if limited and not extra:
        parts.append(PSPING_MISSING_CAPTION)
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def _caption_for(kind: HostAccessKind, *, limited: bool) -> str:
    del limited  # o aviso de PsPing ausente fica no tooltip, não na legenda
    captions = {
        HostAccessKind.PSEXEC_READY: CAPTION_NETWORK_READY,
        HostAccessKind.ICMP_BLOCKED: CAPTION_ICMP_BLOCKED,
        HostAccessKind.TCP_UNAVAILABLE: CAPTION_TCP_UNAVAILABLE,
        HostAccessKind.OFFLINE: CAPTION_OFFLINE,
        HostAccessKind.UNRESOLVED: CAPTION_UNRESOLVED,
        HostAccessKind.INVALID_HOST: CAPTION_INVALID,
        HostAccessKind.CHECK_FAILED: CAPTION_CHECK_FAILED,
        HostAccessKind.CANCELLED: "Verificando…",
    }
    return captions[kind]


def _from_kind(
    host: str,
    kind: HostAccessKind,
    *,
    icmp: Optional[PsPingResult] = None,
    tcp: Optional[PsPingResult] = None,
    limited: bool = False,
    pstools_dir: Optional[str] = None,
) -> HostReachability:
    status = {
        HostAccessKind.PSEXEC_READY: "online",
        HostAccessKind.ICMP_BLOCKED: "warn",
        HostAccessKind.TCP_UNAVAILABLE: "warn",
        HostAccessKind.OFFLINE: "offline",
        HostAccessKind.UNRESOLVED: "invalid",
        HostAccessKind.INVALID_HOST: "invalid",
        HostAccessKind.CHECK_FAILED: "err",
        HostAccessKind.CANCELLED: "checking",
    }
    caption = _caption_for(kind, limited=limited)
    # Rede pronta para uma tentativa. Não significa sessão autenticada.
    psexec_enabled = kind in (
        HostAccessKind.PSEXEC_READY,
        HostAccessKind.ICMP_BLOCKED,
    )
    extra = _probe_summary(kind, icmp=icmp, tcp=tcp)
    if limited and kind not in (HostAccessKind.INVALID_HOST, HostAccessKind.CANCELLED):
        folder = _pstools_folder(pstools_dir)
        missing = (
            f"PsPing não encontrado em {folder}."
            if folder
            else "PsPing não encontrado na pasta PSTools."
        )
        extra = "\n".join(
            part
            for part in (
                extra,
                missing,
                "ICMP via ping.exe; TCP 445 via socket.",
            )
            if part
        )
    return HostReachability(
        host=host,
        kind=kind,
        caption=caption,
        status_state=status[kind],
        psexec_enabled=psexec_enabled,
        icmp=icmp,
        tcp=tcp,
        limited=limited,
        cancelled=kind == HostAccessKind.CANCELLED,
        tooltip=_tooltip_for(caption, limited=limited, extra=extra),
    )


def _icmp_from_fallback(host: str, status, family: IpFamily) -> PsPingResult:
    mapping = {
        IcmpPingKind.OK: (PsPingState.SUCCESS, "Host respondeu ao ICMP."),
        IcmpPingKind.INVALID: (PsPingState.NAME_UNRESOLVED, "Host inválido."),
        IcmpPingKind.UNRESOLVED: (PsPingState.NAME_UNRESOLVED, "Nome não resolvido."),
        IcmpPingKind.TIMEOUT: (PsPingState.TIMEOUT, "Tempo limite esgotado."),
        IcmpPingKind.UNREACHABLE: (
            PsPingState.CONNECTIVITY_FAILURE,
            "Host de destino inacessível.",
        ),
        IcmpPingKind.ERROR: (PsPingState.EXECUTION_ERROR, "Falha ao executar o ping."),
    }
    state, message = mapping.get(
        status.kind, (PsPingState.CONNECTIVITY_FAILURE, "Falha de conectividade.")
    )
    if status.kind == IcmpPingKind.ERROR and getattr(status, "error", "") == "cancelled":
        state = PsPingState.CANCELLED
        message = "Teste cancelado."
    return PsPingResult(
        host=host,
        mode=PsPingMode.ICMP,
        state=state,
        message=message,
        attempts=1,
        ip_family=family,
    )


def _tcp_from_fallback(host: str, port: int, probe: str, family: IpFamily) -> PsPingResult:
    mapping = {
        "open": (PsPingState.SUCCESS, "TCP acessível."),
        "refused": (PsPingState.CONNECTION_REFUSED, "Conexão recusada."),
        "timeout": (PsPingState.TIMEOUT, "Tempo limite esgotado."),
        "unresolved": (PsPingState.NAME_UNRESOLVED, "Nome não resolvido."),
        "cancelled": (PsPingState.CANCELLED, "Teste cancelado."),
        "error": (PsPingState.EXECUTION_ERROR, "Falha ao testar a porta TCP."),
    }
    state, message = mapping.get(
        probe, (PsPingState.CONNECTIVITY_FAILURE, "Falha de conectividade.")
    )
    return PsPingResult(
        host=host,
        mode=PsPingMode.TCP,
        state=state,
        port=port,
        message=message,
        attempts=1,
        ip_family=family,
    )


_RETRY_NATIVE_STATES = frozenset(
    {
        PsPingState.TIMEOUT,
        PsPingState.CONNECTIVITY_FAILURE,
        PsPingState.EXECUTION_ERROR,
        PsPingState.CONNECTION_REFUSED,
    }
)


def _can_retry_native(result: PsPingResult) -> bool:
    return result.state in _RETRY_NATIVE_STATES


def _prefer_native_if_ok(original: PsPingResult, native: PsPingResult) -> PsPingResult:
    if native.ok:
        return native
    return original


def _native_icmp(
    host: str,
    family: IpFamily,
    icmp_fallback: Optional[Callable[[str], object]] = None,
    should_cancel: Optional[CancelFn] = None,
) -> PsPingResult:
    if icmp_fallback is not None:
        status = icmp_fallback(host)
    else:
        status = ping_host_status(host, should_cancel=should_cancel)
    return _icmp_from_fallback(host, status, family)


def _probe_tcp_port(host: str, port: int, should_cancel: Optional[CancelFn]) -> str:
    """Socket TCP com timeout de reachability. Cancelamento entre a espera."""
    import threading
    import time

    from remoteops.utils.network_scan import probe_tcp_port

    if should_cancel is not None and should_cancel():
        return "cancelled"
    box: dict[str, str] = {}

    def _run() -> None:
        box["probe"] = probe_tcp_port(host, port, timeout=NATIVE_TCP_TIMEOUT_S)

    worker = threading.Thread(target=_run, name="remoteops-tcp-445", daemon=True)
    worker.start()
    deadline = time.monotonic() + NATIVE_TCP_TIMEOUT_S + 0.3
    while worker.is_alive():
        if should_cancel is not None and should_cancel():
            return "cancelled"
        if time.monotonic() >= deadline:
            break
        worker.join(0.1)
    return box.get("probe") or "timeout"


def _native_tcp(
    host: str,
    port: int,
    family: IpFamily,
    tcp_fallback: Optional[Callable[[str, int], str]] = None,
    should_cancel: Optional[CancelFn] = None,
) -> PsPingResult:
    if tcp_fallback is not None:
        probe = tcp_fallback(host, port)
    else:
        probe = _probe_tcp_port(host, port, should_cancel)
    return _tcp_from_fallback(host, port, str(probe or "error"), family)


_DEFINITIVE_DOWN = frozenset(
    {
        PsPingState.TIMEOUT,
        PsPingState.CONNECTIVITY_FAILURE,
    }
)
_INCONCLUSIVE = frozenset(
    {
        PsPingState.EXECUTION_ERROR,
        PsPingState.TOOL_MISSING,
    }
)


def _classify(icmp: PsPingResult, tcp: Optional[PsPingResult]) -> HostAccessKind:
    if icmp.state == PsPingState.CANCELLED or (
        tcp is not None and tcp.state == PsPingState.CANCELLED
    ):
        return HostAccessKind.CANCELLED
    tcp_ok = bool(tcp is not None and tcp.ok)
    if icmp.state == PsPingState.NAME_UNRESOLVED and not tcp_ok:
        return HostAccessKind.UNRESOLVED
    if tcp is not None and tcp.state == PsPingState.NAME_UNRESOLVED and not icmp.ok:
        return HostAccessKind.UNRESOLVED
    icmp_ok = icmp.ok
    if icmp_ok and tcp_ok:
        return HostAccessKind.PSEXEC_READY
    if tcp_ok and not icmp_ok:
        return HostAccessKind.ICMP_BLOCKED
    if tcp is None:
        return HostAccessKind.CHECK_FAILED if icmp_ok or icmp.state in _INCONCLUSIVE else HostAccessKind.OFFLINE
    if tcp.state == PsPingState.CONNECTION_REFUSED or (
        icmp_ok and tcp.state in _DEFINITIVE_DOWN
    ):
        return HostAccessKind.TCP_UNAVAILABLE
    if icmp.state in _INCONCLUSIVE or tcp.state in _INCONCLUSIVE:
        return HostAccessKind.CHECK_FAILED
    if icmp.state in _DEFINITIVE_DOWN and tcp.state in _DEFINITIVE_DOWN:
        return HostAccessKind.OFFLINE
    return HostAccessKind.OFFLINE


def probe_host_reachability(
    host: str,
    *,
    should_cancel: Optional[CancelFn] = None,
    use_cache: bool = True,
    pstools_dir: Optional[str] = None,
    family: Optional[IpFamily] = None,
    runner: Optional[RunnerFn] = None,
    psping_present: Optional[bool] = None,
    icmp_fallback: Optional[Callable[[str], object]] = None,
    tcp_fallback: Optional[Callable[[str, int], str]] = None,
    log: bool = True,
) -> HostReachability:
    """ICMP + TCP 445. Nunca propaga exceção."""
    h = normalize_host(host)
    if not h:
        return _from_kind(h, HostAccessKind.INVALID_HOST)
    if not is_valid_host(h):
        return _from_kind(h, HostAccessKind.INVALID_HOST)

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    if _cancelled():
        return _from_kind(h, HostAccessKind.CANCELLED)

    resolved_family = family if family is not None else family_for_host(h)
    present = psping_available(pstools_dir) if psping_present is None else bool(psping_present)
    limited = not present
    port = PSEXEC_TCP_PORT

    def _reach(kind: HostAccessKind, **kwargs) -> HostReachability:
        kwargs.setdefault("limited", limited)
        return _from_kind(h, kind, pstools_dir=pstools_dir, **kwargs)

    try:
        if present:
            icmp = run_psping(
                h,
                mode=PsPingMode.ICMP,
                attempts=1,
                family=resolved_family,
                pstools_dir=pstools_dir,
                should_cancel=should_cancel,
                use_cache=use_cache,
                log=log,
                runner=runner,
            )
            if icmp.state == PsPingState.TOOL_MISSING:
                present = False
                limited = True
            elif _cancelled() or icmp.state == PsPingState.CANCELLED:
                return _reach(HostAccessKind.CANCELLED, icmp=icmp)
            elif icmp.state == PsPingState.NAME_UNRESOLVED:
                return _reach(HostAccessKind.UNRESOLVED, icmp=icmp)
            else:
                if not icmp.ok and _can_retry_native(icmp):
                    if log:
                        from remoteops.utils.app_logging import log_operation

                        log_operation(
                            f"[HOST] {h}: PsPing ICMP falhou — nova tentativa via ping.exe"
                        )
                    native_icmp = _native_icmp(
                        h, resolved_family, icmp_fallback, should_cancel
                    )
                    if _cancelled():
                        return _reach(HostAccessKind.CANCELLED, icmp=icmp)
                    icmp = _prefer_native_if_ok(icmp, native_icmp)
                    if use_cache and icmp.ok:
                        store_cached_psping(icmp)

                tcp = run_psping(
                    h,
                    mode=PsPingMode.TCP,
                    port=port,
                    attempts=1,
                    family=resolved_family,
                    pstools_dir=pstools_dir,
                    should_cancel=should_cancel,
                    use_cache=use_cache,
                    log=log,
                    runner=runner,
                )
                if tcp.state == PsPingState.TOOL_MISSING:
                    present = False
                    limited = True
                elif _cancelled() or tcp.state == PsPingState.CANCELLED:
                    return _reach(HostAccessKind.CANCELLED, icmp=icmp, tcp=tcp)
                else:
                    if not tcp.ok and _can_retry_native(tcp):
                        if log:
                            from remoteops.utils.app_logging import log_operation

                            log_operation(
                                f"[HOST] {h}: PsPing TCP {port} falhou — "
                                "nova tentativa via socket"
                            )
                        native_tcp = _native_tcp(
                            h, port, resolved_family, tcp_fallback, should_cancel
                        )
                        if _cancelled():
                            return _reach(
                                HostAccessKind.CANCELLED, icmp=icmp, tcp=tcp
                            )
                        tcp = _prefer_native_if_ok(tcp, native_tcp)
                        if use_cache and tcp.ok:
                            store_cached_psping(tcp)
                    kind = _classify(icmp, tcp)
                    return _reach(kind, icmp=icmp, tcp=tcp)

        if not present:
            if log:
                from remoteops.utils.app_logging import log_operation

                folder = _pstools_folder(pstools_dir)
                where = folder or "pasta PSTools"
                log_operation(
                    f"[HOST] {h}: PsPing ausente em {where} — "
                    "ICMP via ping.exe, TCP 445 via socket"
                )
            icmp = _native_icmp(h, resolved_family, icmp_fallback, should_cancel)
            if _cancelled():
                return _reach(HostAccessKind.CANCELLED, icmp=icmp, limited=True)
            if icmp.state == PsPingState.NAME_UNRESOLVED:
                return _reach(HostAccessKind.UNRESOLVED, icmp=icmp, limited=True)
            tcp = _native_tcp(h, port, resolved_family, tcp_fallback, should_cancel)
            if _cancelled():
                return _reach(
                    HostAccessKind.CANCELLED, icmp=icmp, tcp=tcp, limited=True
                )
            if use_cache:
                store_cached_psping(icmp)
                store_cached_psping(tcp)
            kind = _classify(icmp, tcp)
            return _reach(kind, icmp=icmp, tcp=tcp, limited=True)

        return _reach(HostAccessKind.OFFLINE)
    except Exception as exc:
        if log:
            from remoteops.utils.app_logging import log_operation
            from remoteops.utils.redaction import redact_command_text

            detail = redact_command_text(f"{type(exc).__name__}: {exc}")
            log_operation(f"[HOST] {h}: falha na verificação — {detail}")
        return _reach(HostAccessKind.CHECK_FAILED, limited=limited)


def probe_tcp_445(
    host: str,
    *,
    should_cancel: Optional[CancelFn] = None,
    use_cache: bool = True,
    pstools_dir: Optional[str] = None,
    family: Optional[IpFamily] = None,
    runner: Optional[RunnerFn] = None,
    psping_present: Optional[bool] = None,
    tcp_fallback: Optional[Callable[[str, int], str]] = None,
    log: bool = True,
) -> PsPingResult:
    """TCP Ping rápido na porta 445 (pré-verificação do PsExec)."""
    h = normalize_host(host)
    if not is_valid_host(h):
        return PsPingResult(
            host=h,
            mode=PsPingMode.TCP,
            state=PsPingState.NAME_UNRESOLVED,
            port=PSEXEC_TCP_PORT,
            message="Host inválido.",
        )
    present = psping_available(pstools_dir) if psping_present is None else bool(psping_present)
    resolved_family = family if family is not None else family_for_host(h)
    if present:
        result = run_psping(
            h,
            mode=PsPingMode.TCP,
            port=PSEXEC_TCP_PORT,
            attempts=1,
            family=resolved_family,
            pstools_dir=pstools_dir,
            should_cancel=should_cancel,
            use_cache=use_cache,
            log=log,
            runner=runner,
        )
        if result.state == PsPingState.CANCELLED or result.cancelled:
            return result
        if result.ok or result.state == PsPingState.NAME_UNRESOLVED:
            return result
        if result.state != PsPingState.TOOL_MISSING and not _can_retry_native(result):
            return result
        if log and result.state != PsPingState.TOOL_MISSING:
            from remoteops.utils.app_logging import log_operation

            log_operation(
                f"[HOST] {h}: PsPing TCP {PSEXEC_TCP_PORT} falhou — "
                "nova tentativa via socket"
            )
        native = _native_tcp(
            h, PSEXEC_TCP_PORT, resolved_family, tcp_fallback, should_cancel
        )
        merged = (
            native
            if result.state == PsPingState.TOOL_MISSING
            else _prefer_native_if_ok(result, native)
        )
        if use_cache and (merged.ok or result.state == PsPingState.TOOL_MISSING):
            store_cached_psping(merged)
        return merged
    result = _native_tcp(h, PSEXEC_TCP_PORT, resolved_family, tcp_fallback, should_cancel)
    if use_cache:
        store_cached_psping(result)
    return result


def decide_batch_connectivity(reach: HostReachability) -> BatchConnectivityDecision:
    """Filtragem inicial da Instalação em Lote (ICMP não exclui se TCP 445 ok)."""
    host = reach.host
    if reach.kind == HostAccessKind.CANCELLED:
        return BatchConnectivityDecision(False, False, False, "")
    if reach.kind in (HostAccessKind.INVALID_HOST, HostAccessKind.UNRESOLVED):
        label = "host inválido" if reach.kind == HostAccessKind.INVALID_HOST else "nome não resolvido"
        return BatchConnectivityDecision(
            False, False, False, f"{host}: {label}"
        )
    limited_note = (
        " — verificação limitada (PsPing ausente; ping.exe / TCP 445)"
        if reach.limited
        else ""
    )
    if reach.kind == HostAccessKind.CHECK_FAILED:
        return BatchConnectivityDecision(
            False, False, False, f"{host}: falha na verificação de rede{limited_note}"
        )
    if reach.kind == HostAccessKind.TCP_UNAVAILABLE:
        return BatchConnectivityDecision(
            False, False, False, f"{host}: TCP 445 indisponível{limited_note}"
        )
    if reach.psexec_enabled:
        if reach.kind == HostAccessKind.ICMP_BLOCKED:
            return BatchConnectivityDecision(
                True,
                True,
                True,
                f"{host}: acessível — ICMP indisponível{limited_note}",
            )
        return BatchConnectivityDecision(True, True, False, "")
    return BatchConnectivityDecision(
        False, False, False, f"{host}: inacessível{limited_note}"
    )
