"""Regras da Pesquisa de Host (sem Qt): hostname, filtro, usuário ativo, alvo PsExec."""

from __future__ import annotations

from typing import Optional, Sequence

from remoteops.utils.domain_users import lookup_domain_full_names
from remoteops.utils.installed_printers import session_account
from remoteops.utils.ping import normalize_host
from remoteops.utils.printers import active_sessions
from remoteops.utils.sessions import RemoteSession, list_remote_sessions

EMPTY_CELL = "—"
ACTIVE_USER_SEPARATOR = "  ·  "
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
    full_name: str = "",
) -> bool:
    """Filtro ao vivo: substring em IP, hostname, usuário ou nome completo."""
    text = (query or "").strip().casefold()
    if not text:
        return True
    haystack = " ".join(
        (
            normalize_host(ip),
            display_hostname(ip, hostname),
            (user or "").strip() or EMPTY_CELL,
            (full_name or "").strip() or EMPTY_CELL,
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
    return ACTIVE_USER_SEPARATOR.join(users) if users else EMPTY_CELL


def lookup_full_names_for_users(user_display: str, hostname: str = "") -> str:
    """Nome completo AD (NetUserGetInfo) para cada conta do usuário ativo."""
    names = lookup_domain_full_names(
        user_display, hostname=hostname, empty=EMPTY_CELL
    )
    if not names:
        return EMPTY_CELL
    shown = [name.strip() or EMPTY_CELL for name in names]
    if all(item == EMPTY_CELL for item in shown):
        return EMPTY_CELL
    return ACTIVE_USER_SEPARATOR.join(shown)


def lookup_targets(ip: str, hostname: str = "") -> list[str]:
    """Hostname (quser/WTS) primeiro; IP se for diferente."""
    out: list[str] = []
    seen: set[str] = set()
    for item in (hostname, ip):
        target = normalize_host(item)
        if not target:
            continue
        key = target.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(target)
    return out


def lookup_active_session_users(
    host: str,
    user: str = "",
    password: str = "",
    *,
    hostname: str = "",
) -> str:
    """Consulta WTS + IPC$ (mesmo backend de Energia/Mensagem). Falha → em-dash.

    Tenta o hostname (como o ``quser /server:NOME``) e depois o IP. Logins
    só numéricos vêm do qwinsta/quser quando a API WTS omite o usuário.
    """
    for target in lookup_targets(host, hostname):
        try:
            sessions, error = list_remote_sessions(
                target, user=user, password=password
            )
        except Exception:
            continue
        if error and not sessions:
            continue
        text = format_active_session_users(sessions)
        if text != EMPTY_CELL:
            return text
    return EMPTY_CELL


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
