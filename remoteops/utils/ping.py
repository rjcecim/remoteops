"""Verificação leve de host online via ping (Windows)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.win_cmd import run_captured

# Metacaracteres de shell/UNC. O host vai em um argumento, mas o preview
# também é copiado para o cmd.exe — estes caracteres não entram no alvo.
_INVALID_CHARS = ('&', '|', '<', '>', '^', '"', "'", '%', ' ', '\t')
_PATH_OR_EXTRA_CHARS = set("\\/;,*?$`!(){}[]=+~#@")
_IPV4_LITERAL = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}\Z")
CancelFn = Optional[Callable[[], bool]]

_UNRESOLVED_MARKERS = (
    "não foi possível encontrar o host",
    "nao foi possivel encontrar o host",
    "could not find host",
    "ping request could not find host",
)
_TIMEOUT_MARKERS = (
    "request timed out",
    "esgotado o tempo limite",
    "transmit failed",
)
_UNREACHABLE_MARKERS = (
    "destination host unreachable",
    "host de destino inacessível",
    "host de destino inacessivel",
)


class IcmpPingKind(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    UNRESOLVED = "unresolved"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    ERROR = "error"


@dataclass(frozen=True)
class IcmpPingStatus:
    online: bool
    kind: IcmpPingKind
    error: str = ""


def normalize_host(host: str) -> str:
    """Remove espaços e barras iniciais/finais (prefixo UNC ou URL)."""
    return (host or "").strip().strip("\\/")


def is_ipv6_host(host: str) -> bool:
    """True só para um literal IPv6 bem formado, com ou sem colchetes."""
    raw = normalize_host(host)
    if len(raw) >= 2 and raw[0] == "[" and raw[-1] == "]":
        raw = raw[1:-1]
    if not raw or "%" in raw or raw.count(":") < 2:
        return False
    try:
        ipaddress.IPv6Address(raw)
    except ValueError:
        return False
    return True


def format_unc_host(host: str) -> str:
    """Token depois de ``\\\\`` no PsExec e em UNC.

    Hostname e IPv4 permanecem como estão. IPv6 entra entre colchetes,
    que é o formato UNC do Windows.
    """
    h = normalize_host(host)
    if not is_ipv6_host(h):
        return h
    if len(h) >= 2 and h[0] == "[" and h[-1] == "]":
        return h
    return f"[{h}]"


def _hostname_labels_ok(host: str) -> bool:
    name = host[:-1] if host.endswith(".") else host
    if not name or name.startswith(".") or name.endswith(".") or ".." in name:
        return False
    labels = name.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return False
    if _IPV4_LITERAL.fullmatch(name):
        try:
            ipaddress.IPv4Address(name)
        except ValueError:
            return False
        return True
    if "." in name and all(part.isdigit() for part in labels):
        return False
    for label in labels:
        if label.startswith("-") or label.endswith("-"):
            return False
        if not all(ch.isalnum() or ch in "_-" for ch in label):
            return False
    return True


def is_valid_host(host: str) -> bool:
    """Hostname, FQDN, IPv4 ou IPv6 literal. Rejeita caminho, porta e metacaracteres."""
    h = normalize_host(host)
    if not h or len(h) > 253:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in h):
        return False
    if any(ch in h for ch in _INVALID_CHARS):
        return False
    if is_ipv6_host(h):
        return True
    if any(ch in h for ch in _PATH_OR_EXTRA_CHARS) or ":" in h:
        return False
    return _hostname_labels_ok(h)


def _classify_ping_text(returncode: Optional[int], out: str) -> IcmpPingStatus:
    if any(m in out for m in _UNRESOLVED_MARKERS):
        return IcmpPingStatus(False, IcmpPingKind.UNRESOLVED, "")
    if any(m in out for m in _TIMEOUT_MARKERS):
        return IcmpPingStatus(False, IcmpPingKind.TIMEOUT, "")
    if any(m in out for m in _UNREACHABLE_MARKERS):
        return IcmpPingStatus(False, IcmpPingKind.UNREACHABLE, "")
    if returncode == 0:
        return IcmpPingStatus(True, IcmpPingKind.OK, "")
    return IcmpPingStatus(False, IcmpPingKind.UNREACHABLE, "")


def ping_host_status(
    host: str,
    timeout_ms: int = 1000,
    should_cancel: CancelFn = None,
) -> IcmpPingStatus:
    """Faz 1 ping e classifica o resultado (sem lançar exceção)."""
    h = normalize_host(host)
    if not is_valid_host(h):
        return IcmpPingStatus(False, IcmpPingKind.INVALID, "invalid")
    if should_cancel is not None and should_cancel():
        return IcmpPingStatus(False, IcmpPingKind.ERROR, "cancelled")

    argv = ["ping", "-n", "1", "-w", str(max(200, int(timeout_ms))), h]
    timeout_s = max(3.0, (timeout_ms / 1000.0) + 2.0)
    try:
        if should_cancel is None:
            result = run_captured(argv, timeout=timeout_s)
            out = (
                decode_console_bytes(result.stdout or b"")
                + decode_console_bytes(result.stderr or b"")
            ).lower()
            return _classify_ping_text(result.returncode, out)

        from remoteops.core.process_runner import run_argv_captured

        captured = run_argv_captured(
            argv,
            timeout_s=timeout_s,
            should_cancel=should_cancel,
        )
        if captured.cancelled:
            return IcmpPingStatus(False, IcmpPingKind.ERROR, "cancelled")
        if captured.spawn_error:
            return IcmpPingStatus(False, IcmpPingKind.ERROR, "error")
        out = (
            decode_console_bytes(captured.stdout or b"")
            + decode_console_bytes(captured.stderr or b"")
        ).lower()
        if captured.timed_out and not any(m in out for m in _UNRESOLVED_MARKERS):
            return IcmpPingStatus(False, IcmpPingKind.TIMEOUT, "")
        return _classify_ping_text(captured.returncode, out)
    except Exception:
        return IcmpPingStatus(False, IcmpPingKind.ERROR, "error")


def ping_host(host: str, timeout_ms: int = 1000) -> Tuple[bool, str]:
    """
    Faz 1 ping no host.
    Retorna (online, mensagem_erro_opcional).
    """
    status = ping_host_status(host, timeout_ms=timeout_ms)
    if status.kind == IcmpPingKind.INVALID:
        return False, "invalid"
    if status.kind == IcmpPingKind.ERROR:
        return False, "error"
    return status.online, ""
