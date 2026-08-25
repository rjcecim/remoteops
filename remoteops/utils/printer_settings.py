"""Preferências da aba Impressoras (persistidas em settings.ini)."""

from __future__ import annotations

from typing import Any, Optional

from remoteops.utils.app_settings import (
    KEY_PRINT_LIST_TIMEOUT,
    KEY_PRINT_SERVER,
    load_setting,
    save_portable_settings,
)
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.printers import print_server_unc, printer_unc

DEFAULT_PRINT_SERVER = ""
PRINT_SERVER_PLACEHOLDER = r"\\printserver"
PRINT_SERVER_REQUIRED_MSG = (
    "Nenhum servidor de impressão foi configurado.\n\n"
    "Acesse Configurações e informe o servidor de impressão antes de continuar."
)
DEFAULT_PRINT_LIST_TIMEOUT_S = 90
MIN_PRINT_LIST_TIMEOUT_S = 30
MAX_PRINT_LIST_TIMEOUT_S = 180

_runtime_server: Optional[str] = None
_runtime_timeout: Optional[int] = None


def parse_print_server_input(name: str) -> str:
    """Normaliza o host do servidor de impressão.

    Vazio permanece vazio. ``\\\\servidor\\\\share`` usa só o host.
    Inválido e não vazio → ``ValueError``.
    """
    raw = normalize_host(name)
    if not raw:
        return ""
    raw = raw.replace("/", "\\")
    if "\\" in raw:
        raw = raw.split("\\", 1)[0].strip()
    if not is_valid_host(raw):
        raise ValueError("Servidor de impressão inválido.")
    return raw


def normalize_print_server(name: str) -> str:
    """Host persistível; vazio e inválidos viram string vazia."""
    try:
        return parse_print_server_input(name)
    except ValueError:
        return ""


def normalize_print_list_timeout(value: Any) -> int:
    """Timeout da consulta Get-Printer; inválidos → 90; limita 30–180."""
    try:
        if value is None or value is False or value is True:
            return DEFAULT_PRINT_LIST_TIMEOUT_S
        if isinstance(value, str) and not value.strip():
            return DEFAULT_PRINT_LIST_TIMEOUT_S
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PRINT_LIST_TIMEOUT_S
    if n < MIN_PRINT_LIST_TIMEOUT_S:
        return MIN_PRINT_LIST_TIMEOUT_S
    if n > MAX_PRINT_LIST_TIMEOUT_S:
        return MAX_PRINT_LIST_TIMEOUT_S
    return n


def get_print_server() -> str:
    """Servidor de impressão em uso (persistido; vazio até o usuário informar)."""
    global _runtime_server
    if _runtime_server is None:
        raw = load_setting(KEY_PRINT_SERVER, DEFAULT_PRINT_SERVER)
        _runtime_server = normalize_print_server(str(raw or ""))
    return _runtime_server


def set_print_server(name: str) -> str:
    """Define e persiste o servidor de impressão (snapshot completo)."""
    global _runtime_server
    normalized = parse_print_server_input(name)
    save_portable_settings({KEY_PRINT_SERVER: normalized})
    _runtime_server = normalized
    return normalized


def is_print_server_configured() -> bool:
    return bool(get_print_server())


def require_print_server() -> str:
    """Host persistido; vazio → ``ValueError`` com mensagem para a UI."""
    server = get_print_server()
    if not server:
        raise ValueError(PRINT_SERVER_REQUIRED_MSG)
    return server


def get_print_list_timeout() -> int:
    """Timeout da listagem Get-Printer (persistido; padrão 90 s)."""
    global _runtime_timeout
    if _runtime_timeout is None:
        raw = load_setting(KEY_PRINT_LIST_TIMEOUT, DEFAULT_PRINT_LIST_TIMEOUT_S)
        _runtime_timeout = normalize_print_list_timeout(raw)
    return int(_runtime_timeout)


def set_print_list_timeout(seconds: int) -> int:
    """Define e persiste o timeout da consulta do catálogo."""
    global _runtime_timeout
    normalized = normalize_print_list_timeout(seconds)
    save_portable_settings({KEY_PRINT_LIST_TIMEOUT: int(normalized)})
    _runtime_timeout = normalized
    return normalized


def configured_print_server_unc() -> str:
    return print_server_unc(require_print_server())


def configured_printer_unc(share_name: str) -> str:
    return printer_unc(share_name, require_print_server())
