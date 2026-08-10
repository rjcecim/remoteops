"""Configurações da Pesquisa de Aplicativos (persistidas em settings.ini)."""

from __future__ import annotations

import os
from typing import Any, Optional, Tuple

from remoteops.utils.app_settings import (
    KEY_SEARCH_HOSTS_PATH,
    KEY_SEARCH_MAX_WORKERS,
    load_setting,
    save_portable_settings,
)

DEFAULT_SEARCH_MAX_WORKERS = 8
MIN_SEARCH_MAX_WORKERS = 1
MAX_SEARCH_MAX_WORKERS = 32

_runtime_max_workers: Optional[int] = None
_runtime_hosts_path: Optional[str] = None  # None = ainda não carregado


def normalize_search_max_workers(value: Any) -> int:
    """
    Normaliza a quantidade de consultas simultâneas.
    Inválidos → 8; abaixo de 1 → 1; acima de 32 → 32.
    """
    try:
        if value is None or value is False or value is True:
            return DEFAULT_SEARCH_MAX_WORKERS
        if isinstance(value, str) and not value.strip():
            return DEFAULT_SEARCH_MAX_WORKERS
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SEARCH_MAX_WORKERS
    if n < MIN_SEARCH_MAX_WORKERS:
        return MIN_SEARCH_MAX_WORKERS
    if n > MAX_SEARCH_MAX_WORKERS:
        return MAX_SEARCH_MAX_WORKERS
    return n


def _load_from_settings() -> int:
    raw = load_setting(KEY_SEARCH_MAX_WORKERS, DEFAULT_SEARCH_MAX_WORKERS)
    return normalize_search_max_workers(raw)


def get_search_max_workers() -> int:
    """Consultas simultâneas em uso (persistidas; padrão 8)."""
    global _runtime_max_workers
    if _runtime_max_workers is None:
        _runtime_max_workers = _load_from_settings()
    return _runtime_max_workers


def set_search_max_workers(value: int) -> int:
    """
    Define e persiste a quantidade de consultas simultâneas (snapshot completo).
    Em falha de gravação, mantém o valor anterior e propaga SettingsWriteError.
    """
    global _runtime_max_workers
    normalized = normalize_search_max_workers(value)
    save_portable_settings({KEY_SEARCH_MAX_WORKERS: int(normalized)})
    _runtime_max_workers = normalized
    return normalized


def normalize_search_hosts_path(path: Any) -> str:
    """Normaliza caminho do hosts.json; vazio se inválido/ausente."""
    p = str(path or "").strip().strip('"').strip("'")
    if not p:
        return ""
    p = os.path.normpath(p)
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def _load_hosts_path_from_settings() -> str:
    raw = load_setting(KEY_SEARCH_HOSTS_PATH, "")
    return normalize_search_hosts_path(raw)


def get_search_hosts_path() -> str:
    """Caminho preferido do hosts.json (persistido; vazio = usar padrão local)."""
    global _runtime_hosts_path
    if _runtime_hosts_path is None:
        _runtime_hosts_path = _load_hosts_path_from_settings()
    return _runtime_hosts_path


def set_search_hosts_path(path: str) -> str:
    """
    Define e persiste o hosts.json da pesquisa (snapshot completo).
    Em falha de gravação, mantém o valor anterior e propaga SettingsWriteError.
    """
    global _runtime_hosts_path
    normalized = normalize_search_hosts_path(path)
    save_portable_settings({KEY_SEARCH_HOSTS_PATH: normalized})
    _runtime_hosts_path = normalized
    return normalized


def resolve_configured_hosts_path() -> Tuple[Optional[str], str]:
    """
    Resolve o hosts.json configurado.
    Preferência persistida → hosts.json local → missing.
    """
    from remoteops.utils.hosts import resolve_hosts_path

    preferred = get_search_hosts_path()
    return resolve_hosts_path(preferred or None)
