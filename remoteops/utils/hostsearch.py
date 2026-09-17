"""Regras da Pesquisa de Host (sem Qt): hostname, filtro, usuário ativo, alvo PsExec."""

from __future__ import annotations

from typing import Optional, Sequence

from remoteops.utils.installed_printers import session_account
from remoteops.utils.ping import normalize_host
from remoteops.utils.printers import active_sessions
from remoteops.utils.sessions import RemoteSession, list_remote_sessions

EMPTY_CELL = "—"
SESSION_LOOKUP_WORKERS = 8


def display_hostname(ip: str, hostname: str) -> str:
    """Hostname só se for um nome distinto do IP; senão em-dash."""
    addr = normalize_host(ip)
    name = normalize_host(hostname)
    if not name or not addr:
        return EMPTY_CELL
    if name.casefold() == addr.casefold():
        return EMPTY_CELL
    return name


def psexec_target(ip: str, hostname: str) -> str:
    """Hostname distinto do IP para o campo do PsExec; senão o IP."""
    shown = display_hostname(ip, hostname)
    if shown != EMPTY_CELL:
        return shown
    return normalize_host(ip)


def row_matches_filter(
    ip: str,
    hostname: str,
    user: str,
    query: str,
) -> bool:
    """Filtro ao vivo: substring em IP, hostname exibido ou usuário (sem nova varredura)."""
    text = (query or "").strip().casefold()
    if not text:
        return True
    haystack = " ".join(
        (
            normalize_host(ip),
            display_hostname(ip, hostname),
            (user or "").strip() or EMPTY_CELL,
        )
    ).casefold()
    return text in haystack


def format_active_session_users(sessions: Sequence[RemoteSession]) -> str:
    """Usuários de sessão Ativa como ``DOMÍNIO\\user``, no mesmo critério de Energia."""
    users: list[str] = []
    seen: set[str] = set()
    for session in active_sessions(sessions):
        name = (session_account(session) or session.username or "").strip()
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        users.append(name)
    return "  ·  ".join(users) if users else EMPTY_CELL


def lookup_active_session_users(
    host: str,
    user: str = "",
    password: str = "",
) -> str:
    """Consulta WTS + IPC$ (mesmo backend de Energia/Mensagem). Falha → em-dash."""
    target = normalize_host(host)
    if not target:
        return EMPTY_CELL
    try:
        sessions, error = list_remote_sessions(target, user=user, password=password)
    except Exception:
        return EMPTY_CELL
    if error or not sessions:
        return EMPTY_CELL
    return format_active_session_users(sessions)


def snapshot_creds(provider: Optional[object]) -> tuple[str, str]:
    if provider is None:
        return "", ""
    try:
        pair = provider()  # type: ignore[misc]
    except Exception:
        return "", ""
    if not isinstance(pair, (tuple, list)) or len(pair) < 2:
        return "", ""
    return str(pair[0] or ""), str(pair[1] or "")


def require_active_network_range_message(
    mode: str,
    error: Optional[str] = None,
) -> Optional[str]:
    """None se a faixa está ativa; senão o aviso (sem fallback para hosts.json)."""
    if (mode or "").strip().casefold() == "network":
        return None
    if (mode or "").strip().casefold() == "invalid":
        return (error or "").strip() or "Faixa de IP inválida."
    return (
        "Ative uma faixa de IP em Configurações. "
        "A Pesquisa de Host não usa hosts.json."
    )
