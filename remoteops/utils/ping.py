"""Verificação leve de host online via ping (Windows)."""

from __future__ import annotations

from typing import Tuple

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.win_cmd import run_captured


_INVALID_CHARS = ('&', '|', '<', '>', '^', '"', "'", '%', ' ', '\t')


def normalize_host(host: str) -> str:
    return (host or "").strip().strip("\\")


def is_valid_host(host: str) -> bool:
    h = normalize_host(host)
    if not h:
        return False
    if any(ch in h for ch in _INVALID_CHARS):
        return False
    return True


def ping_host(host: str, timeout_ms: int = 1000) -> Tuple[bool, str]:
    """
    Faz 1 ping no host.
    Retorna (online, mensagem_erro_opcional).
    """
    h = normalize_host(host)
    if not is_valid_host(h):
        return False, "invalid"

    try:
        result = run_captured(
            ["ping", "-n", "1", "-w", str(max(200, int(timeout_ms))), h],
            timeout=max(3.0, (timeout_ms / 1000.0) + 2.0),
        )
    except Exception:
        return False, "error"

    out = (
        decode_console_bytes(result.stdout or b"")
        + decode_console_bytes(result.stderr or b"")
    ).lower()
    offline_markers = (
        "destination host unreachable",
        "host de destino inacessível",
        "request timed out",
        "esgotado o tempo limite",
        "transmit failed",
        "não foi possível encontrar o host",
        "could not find host",
        "ping request could not find host",
    )
    if any(m in out for m in offline_markers):
        return False, ""

    return result.returncode == 0, ""
