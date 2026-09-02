"""PsPing local: ICMP Ping e TCP Ping (sem Latency/Bandwidth, sem modo servidor).

Independente de Qt. Executa via ``run_argv_captured`` (sem shell, sem cmd.exe).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.process_runner import CapturedProcess, run_argv_captured
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.redaction import redact_command_text

PSPING_NAMES: tuple[str, ...] = ("PsPing64.exe", "PsPing.exe")
DEFAULT_ATTEMPTS = 1
ALLOWED_ATTEMPTS: tuple[int, ...] = (1, 3, 5)
MIN_PORT = 1
MAX_PORT = 65535
CACHE_TTL_S = 8.0
PSEXEC_TCP_PORT = 445

RunnerFn = Callable[..., CapturedProcess]
CancelFn = Callable[[], bool]

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, "PsPingResult"]] = {}
_logged_missing = False


class PsPingMode(str, Enum):
    ICMP = "icmp"
    TCP = "tcp"


class IpFamily(str, Enum):
    IPV4 = "ipv4"
    IPV6 = "ipv6"


class PsPingState(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    NAME_UNRESOLVED = "name_unresolved"
    TOOL_MISSING = "tool_missing"
    CANCELLED = "cancelled"
    EXECUTION_ERROR = "execution_error"
    CONNECTIVITY_FAILURE = "connectivity_failure"


_STATE_CAPTIONS = {
    PsPingState.SUCCESS: "sucesso",
    PsPingState.TIMEOUT: "timeout",
    PsPingState.CONNECTION_REFUSED: "conexão recusada",
    PsPingState.NAME_UNRESOLVED: "nome não resolvido",
    PsPingState.TOOL_MISSING: "ferramenta ausente",
    PsPingState.CANCELLED: "cancelado",
    PsPingState.EXECUTION_ERROR: "erro de execução",
    PsPingState.CONNECTIVITY_FAILURE: "falha de conectividade",
}

_UNRESOLVED_MARKERS = (
    "error resolving",
    "could not find host",
    "ping request could not find host",
    "não foi possível encontrar o host",
    "nao foi possivel encontrar o host",
    "name or service not known",
    "no such host",
    "unknown host",
    "host unknown",
    "requested name is valid, but no data",
    "getaddrinfo failed",
    "não é um host conhecido",
    "nao e um host conhecido",
)
_REFUSED_MARKERS = (
    "refused",
    "recusou",
    "connection refused",
    "actively refused",
    "wsaeconnrefused",
    "target machine actively refused",
)
_TIMEOUT_MARKERS = (
    "request timed out",
    "timed out",
    "timeout period expired",
    "esgotado o tempo limite",
    "tempo limite",
    "100% loss",
    "100% de perda",
    "lost = 1 (100%",
)
_SUCCESS_MARKERS = (
    "lost = 0 (0% loss)",
    "lost = 0 (0% de perda)",
    "0% loss",
    "0% de perda",
)


@dataclass(frozen=True)
class PsPingResult:
    host: str
    mode: PsPingMode
    state: PsPingState
    port: Optional[int] = None
    attempts: int = DEFAULT_ATTEMPTS
    ip_family: IpFamily = IpFamily.IPV4
    returncode: Optional[int] = None
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    spawn_error: str = ""

    @property
    def ok(self) -> bool:
        return self.state == PsPingState.SUCCESS

    @property
    def caption(self) -> str:
        return _STATE_CAPTIONS.get(self.state, self.state.value)


def state_caption(state: PsPingState) -> str:
    return _STATE_CAPTIONS.get(state, state.value)


def looks_like_ipv6(host: str) -> bool:
    h = (host or "").strip()
    if h.startswith("[") and "]" in h:
        return True
    return h.count(":") >= 2


def snap_attempts(value: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ATTEMPTS
    if n <= 1:
        return 1
    if n <= 3:
        return 3
    return 5


def validate_port(port: object) -> tuple[bool, int, str]:
    try:
        n = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, 0, "Porta TCP inválida."
    if n < MIN_PORT or n > MAX_PORT:
        return False, n, "A porta TCP deve estar entre 1 e 65535."
    return True, n, ""


def tcp_destination(host: str, port: int) -> str:
    h = normalize_host(host)
    if h.startswith("[") and "]" in h:
        return f"{h}:{int(port)}"
    if looks_like_ipv6(h):
        return f"[{h}]:{int(port)}"
    return f"{h}:{int(port)}"


def resolve_psping_exe(pstools_dir: Optional[str] = None) -> str:
    from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool

    base = pstools_dir if pstools_dir is not None else get_pstools_dir()
    return resolve_pstools_tool(base, PSPING_NAMES)


def psping_available(pstools_dir: Optional[str] = None) -> bool:
    path = resolve_psping_exe(pstools_dir)
    return bool(path and os.path.isfile(path))


def _family_flag(family: IpFamily) -> str:
    return "-6" if family == IpFamily.IPV6 else "-4"


def build_psping_argv(
    *,
    exe: str,
    host: str,
    mode: PsPingMode,
    port: Optional[int] = None,
    attempts: int = DEFAULT_ATTEMPTS,
    family: IpFamily = IpFamily.IPV4,
) -> list[str]:
    """Monta o argv do PsPing. Não executa. Levanta ValueError se inválido."""
    h = normalize_host(host)
    if not is_valid_host(h):
        raise ValueError("Host inválido.")
    if h.count(":") == 1 and not looks_like_ipv6(h):
        raise ValueError("Host inválido.")
    n = snap_attempts(attempts)
    argv = [
        str(exe),
        _family_flag(family),
        "-n",
        str(n),
        "-w",
        "0",
        "-q",
    ]
    if mode == PsPingMode.TCP:
        ok, port_n, err = validate_port(port)
        if not ok:
            raise ValueError(err or "Porta TCP inválida.")
        argv.append(tcp_destination(h, port_n))
    else:
        argv.append(h)
    return argv


def _decode_output(data: bytes) -> str:
    return decode_console_bytes(data or b"")


def _combined_text(stdout: str, stderr: str, spawn_error: str = "") -> str:
    return "\n".join(p for p in (stdout, stderr, spawn_error) if p).casefold()


def interpret_psping(
    captured: CapturedProcess,
    *,
    host: str,
    mode: PsPingMode,
    port: Optional[int],
    attempts: int,
    family: IpFamily,
) -> PsPingResult:
    stdout = _decode_output(captured.stdout)
    stderr = _decode_output(captured.stderr)
    spawn = redact_command_text(captured.spawn_error or "")
    combined = _combined_text(stdout, stderr, spawn)

    if captured.cancelled:
        state = PsPingState.CANCELLED
        message = "Teste cancelado."
    elif spawn and (
        "não encontrado" in spawn.casefold()
        or "nao encontrado" in spawn.casefold()
        or "not found" in spawn.casefold()
    ):
        state = PsPingState.TOOL_MISSING
        message = "PsPing não encontrado na pasta PSTools."
    elif spawn:
        state = PsPingState.EXECUTION_ERROR
        message = spawn or "Falha ao iniciar o PsPing."
    elif captured.timed_out:
        state = PsPingState.TIMEOUT
        message = "Tempo limite esgotado."
    elif any(m in combined for m in _UNRESOLVED_MARKERS):
        state = PsPingState.NAME_UNRESOLVED
        message = "Nome não resolvido."
    elif any(m in combined for m in _REFUSED_MARKERS):
        state = PsPingState.CONNECTION_REFUSED
        message = "Conexão recusada."
    elif any(m in combined for m in _TIMEOUT_MARKERS):
        state = PsPingState.TIMEOUT
        message = "Tempo limite esgotado."
    elif captured.returncode == 0 or any(m in combined for m in _SUCCESS_MARKERS):
        state = PsPingState.SUCCESS
        message = (
            "TCP acessível." if mode == PsPingMode.TCP else "Host respondeu ao ICMP."
        )
    else:
        state = PsPingState.CONNECTIVITY_FAILURE
        message = "Falha de conectividade."

    return PsPingResult(
        host=normalize_host(host),
        mode=mode,
        state=state,
        port=int(port) if port else None,
        attempts=snap_attempts(attempts),
        ip_family=family,
        returncode=int(captured.returncode) if captured.returncode is not None else None,
        message=message,
        stdout=stdout,
        stderr=stderr,
        timed_out=bool(captured.timed_out),
        cancelled=bool(captured.cancelled),
        spawn_error=spawn,
    )


def _cache_key(
    host: str, mode: PsPingMode, port: Optional[int], family: IpFamily
) -> tuple:
    return (
        normalize_host(host).casefold(),
        mode.value,
        int(port or 0),
        family.value,
    )


def invalidate_psping_cache() -> None:
    global _logged_missing
    with _cache_lock:
        _cache.clear()
        _logged_missing = False


def get_cached_psping(
    host: str,
    mode: PsPingMode,
    port: Optional[int] = None,
    family: IpFamily = IpFamily.IPV4,
) -> Optional[PsPingResult]:
    key = _cache_key(host, mode, port, family)
    now = time.monotonic()
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        expires, result = item
        if now >= expires:
            _cache.pop(key, None)
            return None
        return result


def store_cached_psping(result: PsPingResult) -> None:
    if result.state in (
        PsPingState.CANCELLED,
        PsPingState.TOOL_MISSING,
        PsPingState.EXECUTION_ERROR,
    ):
        return
    key = _cache_key(result.host, result.mode, result.port, result.ip_family)
    expires = time.monotonic() + CACHE_TTL_S
    with _cache_lock:
        _cache[key] = (expires, result)


def _log_psping(result: PsPingResult) -> None:
    from remoteops.utils.app_logging import log_operation

    global _logged_missing
    if result.state == PsPingState.TOOL_MISSING:
        if _logged_missing:
            return
        _logged_missing = True
        log_operation(f"[PSPING] {result.message or 'Executável não encontrado'}")
        return
    if result.state == PsPingState.CANCELLED:
        log_operation("[PSPING] Teste cancelado")
        return
    host = result.host
    if result.mode == PsPingMode.ICMP:
        if result.ok:
            log_operation(f"[PSPING] ICMP {host}: sucesso")
        else:
            log_operation(f"[PSPING] ICMP {host}: falha")
        return
    dest = f"{host}:{result.port}" if result.port else host
    if result.ok:
        log_operation(f"[PSPING] TCP {dest}: acessível")
    else:
        log_operation(f"[PSPING] TCP {dest}: falha")


def _timeout_for_attempts(attempts: int) -> float:
    n = snap_attempts(attempts)
    return max(5.0, (n * 2.5) + 3.0)


def _missing_result(
    host: str,
    mode: PsPingMode,
    port: Optional[int],
    attempts: int,
    family: IpFamily,
    pstools_dir: Optional[str] = None,
) -> PsPingResult:
    from remoteops.utils.pstools import get_pstools_dir

    folder = (pstools_dir if pstools_dir is not None else get_pstools_dir()) or ""
    if folder:
        message = f"PsPing não encontrado em {folder}."
    else:
        message = "PsPing não encontrado na pasta PSTools."
    return PsPingResult(
        host=normalize_host(host),
        mode=mode,
        state=PsPingState.TOOL_MISSING,
        port=int(port) if port else None,
        attempts=snap_attempts(attempts),
        ip_family=family,
        message=message,
    )


def _invalid_result(
    host: str,
    mode: PsPingMode,
    port: Optional[int],
    attempts: int,
    family: IpFamily,
    message: str,
    state: PsPingState = PsPingState.EXECUTION_ERROR,
) -> PsPingResult:
    return PsPingResult(
        host=normalize_host(host),
        mode=mode,
        state=state,
        port=int(port) if port else None,
        attempts=snap_attempts(attempts),
        ip_family=family,
        message=message,
    )


def run_psping(
    host: str,
    *,
    mode: PsPingMode = PsPingMode.ICMP,
    port: Optional[int] = None,
    attempts: int = DEFAULT_ATTEMPTS,
    family: IpFamily = IpFamily.IPV4,
    pstools_dir: Optional[str] = None,
    should_cancel: Optional[CancelFn] = None,
    use_cache: bool = True,
    log: bool = True,
    runner: Optional[RunnerFn] = None,
) -> PsPingResult:
    """Executa ICMP ou TCP Ping. Nunca propaga exceção ao chamador."""
    try:
        h = normalize_host(host)
        if not is_valid_host(h):
            result = _invalid_result(
                h,
                mode,
                port,
                attempts,
                family,
                "Host inválido.",
                state=PsPingState.NAME_UNRESOLVED,
            )
            return result
        if should_cancel and should_cancel():
            return _invalid_result(
                h,
                mode,
                port,
                attempts,
                family,
                "Teste cancelado.",
                state=PsPingState.CANCELLED,
            )

        n = snap_attempts(attempts)
        port_n: Optional[int] = None
        if mode == PsPingMode.TCP:
            ok, port_n, err = validate_port(port)
            if not ok:
                return _invalid_result(h, mode, port, n, family, err)

        if use_cache:
            cached = get_cached_psping(h, mode, port_n, family)
            if cached is not None:
                return cached

        exe = resolve_psping_exe(pstools_dir)
        if not exe or not os.path.isfile(exe):
            result = _missing_result(h, mode, port_n, n, family, pstools_dir=pstools_dir)
            if log:
                _log_psping(result)
            return result

        try:
            argv = build_psping_argv(
                exe=exe,
                host=h,
                mode=mode,
                port=port_n,
                attempts=n,
                family=family,
            )
        except ValueError as exc:
            return _invalid_result(h, mode, port_n, n, family, str(exc) or "Parâmetro inválido.")

        run = runner or run_argv_captured
        try:
            captured = run(
                argv,
                timeout_s=_timeout_for_attempts(n),
                should_cancel=should_cancel,
            )
        except Exception as exc:
            safe = redact_command_text(str(exc) or "Falha ao executar o PsPing.")
            result = _invalid_result(h, mode, port_n, n, family, safe)
            if log:
                _log_psping(result)
            return result

        if not isinstance(captured, CapturedProcess):
            result = _invalid_result(
                h, mode, port_n, n, family, "Falha ao executar o PsPing."
            )
            if log:
                _log_psping(result)
            return result

        result = interpret_psping(
            captured,
            host=h,
            mode=mode,
            port=port_n,
            attempts=n,
            family=family,
        )
        if use_cache:
            store_cached_psping(result)
        if log:
            _log_psping(result)
        return result
    except Exception as exc:
        safe = redact_command_text(str(exc) or "Falha ao executar o PsPing.")
        result = _invalid_result(
            host or "",
            mode,
            port,
            attempts,
            family,
            safe,
        )
        if log:
            try:
                _log_psping(result)
            except Exception:
                pass
        return result


def format_result_row(result: PsPingResult, when: str = "") -> str:
    """Uma linha de texto para copiar (sem credenciais)."""
    port = str(result.port) if result.port else "—"
    proto = "TCP" if result.mode == PsPingMode.TCP else "ICMP"
    stamp = when or ""
    return (
        f"{stamp}\t{proto}\t{result.host}\t{port}\t{result.attempts}\t"
        f"{result.caption}\t{result.message}"
    ).strip()
