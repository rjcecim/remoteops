"""Verificação estruturada de conectividade do host (ICMP + TCP 445).

Sem Qt. Usa PsPing quando disponível; senão ping.exe + socket TCP.
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
    get_cached_psping,
    psping_available,
    run_psping,
    store_cached_psping,
)

PSPING_MISSING_CAPTION = "PsPing ausente — verificação limitada"
CAPTION_PSEXEC_READY = "Online — PsExec acessível"
CAPTION_ICMP_BLOCKED = "Acessível — ICMP bloqueado"
CAPTION_TCP_UNAVAILABLE = "Online — TCP 445 indisponível"
CAPTION_OFFLINE = "Offline ou inacessível"
CAPTION_UNRESOLVED = "Nome não resolvido"
CAPTION_INVALID = "Host inválido"
CAPTION_PSEXEC_READY_LIMITED = "Online — ping.exe / TCP 445 (sem PsPing)"
CAPTION_ICMP_BLOCKED_LIMITED = "Acessível — TCP 445; ICMP via ping.exe (sem PsPing)"
CAPTION_TCP_UNAVAILABLE_LIMITED = "Online — ping.exe ok; TCP 445 indisponível (sem PsPing)"
CAPTION_OFFLINE_LIMITED = "Offline — ping.exe e TCP 445 (sem PsPing)"
CAPTION_UNRESOLVED_LIMITED = "Nome não resolvido (ping.exe; sem PsPing)"


class HostAccessKind(str, Enum):
    PSEXEC_READY = "psexec_ready"
    ICMP_BLOCKED = "icmp_blocked"
    TCP_UNAVAILABLE = "tcp_unavailable"
    OFFLINE = "offline"
    UNRESOLVED = "unresolved"
    INVALID_HOST = "invalid_host"
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
    family: IpFamily = IpFamily.IPV4,
    port: int = PSEXEC_TCP_PORT,
) -> bool:
    cached = get_cached_psping(host, PsPingMode.TCP, port, family)
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


def _tooltip_for(caption: str, *, limited: bool, extra: str = "") -> str:
    parts = [caption]
    if limited and not extra:
        parts.append(PSPING_MISSING_CAPTION)
    if extra:
        parts.append(extra)
    return "\n".join(parts)


def _caption_for(
    kind: HostAccessKind, *, limited: bool
) -> str:
    captions = {
        HostAccessKind.PSEXEC_READY: CAPTION_PSEXEC_READY,
        HostAccessKind.ICMP_BLOCKED: CAPTION_ICMP_BLOCKED,
        HostAccessKind.TCP_UNAVAILABLE: CAPTION_TCP_UNAVAILABLE,
        HostAccessKind.OFFLINE: CAPTION_OFFLINE,
        HostAccessKind.UNRESOLVED: CAPTION_UNRESOLVED,
        HostAccessKind.INVALID_HOST: CAPTION_INVALID,
        HostAccessKind.CANCELLED: "Verificando…",
    }
    limited_captions = {
        HostAccessKind.PSEXEC_READY: CAPTION_PSEXEC_READY_LIMITED,
        HostAccessKind.ICMP_BLOCKED: CAPTION_ICMP_BLOCKED_LIMITED,
        HostAccessKind.TCP_UNAVAILABLE: CAPTION_TCP_UNAVAILABLE_LIMITED,
        HostAccessKind.OFFLINE: CAPTION_OFFLINE_LIMITED,
        HostAccessKind.UNRESOLVED: CAPTION_UNRESOLVED_LIMITED,
    }
    if limited and kind in limited_captions:
        return limited_captions[kind]
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
        HostAccessKind.CANCELLED: "checking",
    }
    caption = _caption_for(kind, limited=limited)
    psexec_enabled = kind in (
        HostAccessKind.PSEXEC_READY,
        HostAccessKind.ICMP_BLOCKED,
    )
    extra = ""
    if limited and kind not in (HostAccessKind.INVALID_HOST, HostAccessKind.CANCELLED):
        folder = _pstools_folder(pstools_dir)
        extra = (
            f"PsPing não encontrado em {folder}."
            if folder
            else "PsPing não encontrado na pasta PSTools."
        )
        extra += "\nICMP via ping.exe; TCP 445 via socket."
        extra += "\nNão é uma verificação com PsPing."
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


def _icmp_from_fallback(host: str, status) -> PsPingResult:
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
    return PsPingResult(
        host=host,
        mode=PsPingMode.ICMP,
        state=state,
        message=message,
        attempts=1,
    )


def _tcp_from_fallback(host: str, port: int, probe: str) -> PsPingResult:
    mapping = {
        "open": (PsPingState.SUCCESS, "TCP acessível."),
        "refused": (PsPingState.CONNECTION_REFUSED, "Conexão recusada."),
        "timeout": (PsPingState.TIMEOUT, "Tempo limite esgotado."),
        "unresolved": (PsPingState.NAME_UNRESOLVED, "Nome não resolvido."),
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
    )


def _classify(icmp: PsPingResult, tcp: Optional[PsPingResult]) -> HostAccessKind:
    if icmp.state == PsPingState.CANCELLED or (
        tcp is not None and tcp.state == PsPingState.CANCELLED
    ):
        return HostAccessKind.CANCELLED
    if icmp.state == PsPingState.NAME_UNRESOLVED or (
        tcp is not None and tcp.state == PsPingState.NAME_UNRESOLVED and not icmp.ok
    ):
        return HostAccessKind.UNRESOLVED
    tcp_ok = bool(tcp is not None and tcp.ok)
    icmp_ok = icmp.ok
    if icmp_ok and tcp_ok:
        return HostAccessKind.PSEXEC_READY
    if (not icmp_ok) and tcp_ok:
        return HostAccessKind.ICMP_BLOCKED
    if icmp_ok and not tcp_ok:
        return HostAccessKind.TCP_UNAVAILABLE
    return HostAccessKind.OFFLINE


def probe_host_reachability(
    host: str,
    *,
    should_cancel: Optional[CancelFn] = None,
    use_cache: bool = True,
    pstools_dir: Optional[str] = None,
    family: IpFamily = IpFamily.IPV4,
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
                family=family,
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
                tcp = run_psping(
                    h,
                    mode=PsPingMode.TCP,
                    port=port,
                    attempts=1,
                    family=family,
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
            status_fn = icmp_fallback or ping_host_status
            icmp_status = status_fn(h)
            icmp = _icmp_from_fallback(h, icmp_status)
            if _cancelled():
                return _reach(HostAccessKind.CANCELLED, icmp=icmp, limited=True)
            if icmp.state == PsPingState.NAME_UNRESOLVED:
                return _reach(HostAccessKind.UNRESOLVED, icmp=icmp, limited=True)
            if tcp_fallback is not None:
                probe = tcp_fallback(h, port)
            else:
                from remoteops.utils.network_scan import probe_tcp_port

                probe = probe_tcp_port(h, port)
            tcp = _tcp_from_fallback(h, port, str(probe or "error"))
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
    except Exception:
        return _reach(HostAccessKind.OFFLINE, limited=limited)


def probe_tcp_445(
    host: str,
    *,
    should_cancel: Optional[CancelFn] = None,
    use_cache: bool = True,
    pstools_dir: Optional[str] = None,
    family: IpFamily = IpFamily.IPV4,
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
    if present:
        return run_psping(
            h,
            mode=PsPingMode.TCP,
            port=PSEXEC_TCP_PORT,
            attempts=1,
            family=family,
            pstools_dir=pstools_dir,
            should_cancel=should_cancel,
            use_cache=use_cache,
            log=log,
            runner=runner,
        )
    if tcp_fallback is not None:
        probe = tcp_fallback(h, PSEXEC_TCP_PORT)
    else:
        from remoteops.utils.network_scan import probe_tcp_port

        probe = probe_tcp_port(h, PSEXEC_TCP_PORT)
    result = _tcp_from_fallback(h, PSEXEC_TCP_PORT, str(probe or "error"))
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
    if reach.psexec_enabled:
        if reach.kind == HostAccessKind.ICMP_BLOCKED:
            return BatchConnectivityDecision(
                True,
                True,
                True,
                f"{host}: acessível — ICMP bloqueado{limited_note}",
            )
        return BatchConnectivityDecision(True, True, False, "")
    return BatchConnectivityDecision(
        False, False, False, f"{host}: offline ou inacessível"
    )
