"""PsLoggedOn — usuários logados localmente e via compartilhamentos de rede."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from remoteops.utils.dates import to_display_datetime
from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool

PSLOGGEDON_NAMES: Tuple[str, ...] = ("PsLoggedon64.exe", "PsLoggedon.exe")

PSLOGGEDON_TIMEOUT_SECONDS = 60.0

_EMPTY = "—"

# Seções conhecidas (EN). A ferramenta não localiza esses cabeçalhos.
_RE_LOCAL_HEADER = re.compile(r"users\s+logged\s+on\s+locally\s*:", re.I)
_RE_SHARE_HEADER = re.compile(
    r"users\s+logged\s+on\s+via\s+resource\s+shares\s*:", re.I
)
_RE_NO_LOCAL = re.compile(r"no\s+one\s+is\s+logged\s+on\s+locally", re.I)
_RE_NO_SHARE = re.compile(
    r"no\s+one\s+is\s+logged\s+on\s+via\s+resource\s+shares", re.I
)
# "31/08/2026 11:45:30\tDOMAIN\user" ou sem timestamp (-x)
_RE_USER_LINE = re.compile(
    r"^\s*(?:(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?)\s+)?"
    r"([^\s\\]+\\[^\s]+|[^\s\\]+)\s*$",
    re.I,
)
_RE_ERROR_OPEN = re.compile(r"error\s+opening\s+hkey_users", re.I)
_RE_ACCESS = re.compile(r"access\s+is\s+denied|acesso\s+negado", re.I)
_RE_RPC = re.compile(r"rpc\s+server\s+is\s+unavailable|o\s+servidor\s+rpc", re.I)


@dataclass
class RemoteLoggedOnUser:
    """Uma entrada reportada pelo PsLoggedOn."""

    username: str
    domain: str = ""
    logon_type: str = ""  # Local | Rede
    source: str = ""
    logon_time: str = ""
    raw: str = ""

    @property
    def display_user(self) -> str:
        if self.domain and self.username:
            return f"{self.domain}\\{self.username}"
        return self.username or self.raw or _EMPTY


def resolve_psloggedon_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSLOGGEDON_NAMES)


def psloggedon_available(pstools_dir: str = "") -> bool:
    path = resolve_psloggedon_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def build_remote_target(host: str) -> str:
    h = (host or "").strip().strip("\\")
    if not h:
        return ""
    return f"\\\\{h}"


def build_psloggedon_argv(
    exe: str,
    host: str,
    *,
    local_only: bool = False,
    hide_times: bool = False,
    nobanner: bool = True,
    accepteula: bool = True,
) -> List[str]:
    """
    Monta argv do PsLoggedOn.

    Uso oficial (v1.35)::
        psloggedon [-l] [-x] [\\\\computername]
        psloggedon [username]

    Credenciais nativas ``-u``/``-p`` **não** existem — use IPC$/identidade
    atual quando o alvo exigir autenticação.
    """
    target = build_remote_target(host)
    if not exe or not target:
        return []
    args = [exe]
    if accepteula:
        args.append("-accepteula")
    if local_only:
        args.append("-l")
    if hide_times:
        args.append("-x")
    if nobanner:
        args.append("-nobanner")
    args.append(target)
    return args


def is_psloggedon_usage_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return ("usage:" in t and "psloggedon" in t) or (
        "-l" in t and "local logons" in t and "\\\\computername" in t
    )


def split_account(account: str) -> Tuple[str, str]:
    """Separa DOMAIN\\user → (domain, user)."""
    raw = (account or "").strip()
    if not raw:
        return "", ""
    if "\\" in raw:
        domain, _, user = raw.partition("\\")
        return domain.strip(), user.strip()
    return "", raw


def parse_psloggedon_output(text: str) -> List[RemoteLoggedOnUser]:
    """Interpreta a saída textual do PsLoggedOn."""
    rows: List[RemoteLoggedOnUser] = []
    section = ""  # local | share
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if _RE_LOCAL_HEADER.search(stripped) or _RE_NO_LOCAL.search(stripped):
            section = "local"
            continue
        if _RE_SHARE_HEADER.search(stripped) or _RE_NO_SHARE.search(stripped):
            section = "share"
            continue
        if stripped.lower().startswith("psloggedon") or stripped.lower().startswith(
            "copyright"
        ):
            continue
        if stripped.lower().startswith("sysinternals"):
            continue
        if is_psloggedon_usage_text(stripped):
            continue

        m = _RE_USER_LINE.match(line)
        if not m:
            continue
        logon_time = (m.group(1) or "").strip()
        shown = to_display_datetime(logon_time)
        if shown:
            logon_time = shown
        account = (m.group(2) or "").strip()
        if not account or account.lower() in ("locally.", "shares."):
            continue
        domain, user = split_account(account)
        if section == "local":
            logon_type = "Local"
            source = "Perfil local (HKEY_USERS)"
        elif section == "share":
            logon_type = "Rede"
            source = "Compartilhamento (NetSessionEnum)"
        else:
            logon_type = _EMPTY
            source = _EMPTY
        rows.append(
            RemoteLoggedOnUser(
                username=user or account,
                domain=domain,
                logon_type=logon_type,
                source=source,
                logon_time=logon_time or _EMPTY,
                raw=stripped,
            )
        )
    return rows


def classify_psloggedon_error(text: str, returncode: int = 0) -> str:
    t = (text or "").strip()
    low = t.lower()
    if is_psloggedon_usage_text(t):
        return "usage"
    if _RE_ACCESS.search(low):
        return "access_denied"
    if _RE_RPC.search(low) or "não está disponível" in low:
        return "rpc_unavailable"
    if _RE_ERROR_OPEN.search(low) or "unable to query" in low:
        return "registry_unavailable"
    if "timed out" in low or "tempo limite" in low:
        return "timeout"
    if returncode != 0 and t:
        return "failed"
    if returncode != 0:
        return "failed"
    return "ok"


def friendly_psloggedon_error(kind: str, detail: str = "") -> str:
    messages = {
        "usage": "PsLoggedOn devolveu a tela de Usage (argumentos inválidos).",
        "access_denied": "Acesso negado ao consultar usuários conectados.",
        "rpc_unavailable": "RPC indisponível no host remoto.",
        "registry_unavailable": (
            "Não foi possível consultar o Registro remoto (Remote Registry)."
        ),
        "timeout": "PsLoggedOn excedeu o tempo limite.",
        "tool_missing": "PsLoggedOn não encontrado na pasta PSTools configurada.",
        "failed": "Falha ao executar PsLoggedOn.",
    }
    base = messages.get(kind, messages["failed"])
    extra = (detail or "").strip()
    if extra and extra.casefold() not in base.casefold():
        return f"{base} {extra}"
    return base


def filter_loggedon_users(
    rows: Sequence[RemoteLoggedOnUser], needle: str
) -> List[RemoteLoggedOnUser]:
    q = (needle or "").strip().casefold()
    if not q:
        return list(rows)
    out: List[RemoteLoggedOnUser] = []
    for row in rows:
        blob = " ".join(
            [
                row.display_user,
                row.logon_type,
                row.source,
                row.logon_time,
                row.raw,
            ]
        ).casefold()
        if q in blob:
            out.append(row)
    return out
