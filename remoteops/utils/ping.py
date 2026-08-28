"""Verificação leve de host online via ping (Windows)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.win_cmd import run_captured

_INVALID_CHARS = ('&', '|', '<', '>', '^', '"', "'", '%', ' ', '\t')

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
    return (host or "").strip().strip("\\")


def is_valid_host(host: str) -> bool:
    h = normalize_host(host)
    if not h:
        return False
    if any(ch in h for ch in _INVALID_CHARS):
        return False
    return True


def ping_host_status(host: str, timeout_ms: int = 1000) -> IcmpPingStatus:
    """Faz 1 ping e classifica o resultado (sem lançar exceção)."""
    h = normalize_host(host)
    if not is_valid_host(h):
        return IcmpPingStatus(False, IcmpPingKind.INVALID, "invalid")

    try:
        result = run_captured(
            ["ping", "-n", "1", "-w", str(max(200, int(timeout_ms))), h],
            timeout=max(3.0, (timeout_ms / 1000.0) + 2.0),
        )
    except Exception:
        return IcmpPingStatus(False, IcmpPingKind.ERROR, "error")

    out = (
        decode_console_bytes(result.stdout or b"")
        + decode_console_bytes(result.stderr or b"")
    ).lower()
    if any(m in out for m in _UNRESOLVED_MARKERS):
        return IcmpPingStatus(False, IcmpPingKind.UNRESOLVED, "")
    if any(m in out for m in _TIMEOUT_MARKERS):
        return IcmpPingStatus(False, IcmpPingKind.TIMEOUT, "")
    if any(m in out for m in _UNREACHABLE_MARKERS):
        return IcmpPingStatus(False, IcmpPingKind.UNREACHABLE, "")
    if result.returncode == 0:
        return IcmpPingStatus(True, IcmpPingKind.OK, "")
    return IcmpPingStatus(False, IcmpPingKind.UNREACHABLE, "")


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
