"""Verificação leve de host online via ping (Windows)."""

from __future__ import annotations

import subprocess
from typing import Tuple


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

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(max(200, int(timeout_ms))), h],
            capture_output=True,
            text=True,
            timeout=max(3.0, (timeout_ms / 1000.0) + 2.0),
            creationflags=creationflags,
        )
    except Exception:
        return False, "error"

    out = f"{result.stdout or ''}{result.stderr or ''}".lower()
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
