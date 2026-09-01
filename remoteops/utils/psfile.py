"""PsFile — arquivos abertos remotamente via compartilhamentos SMB."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool

PSFILE_NAMES: Tuple[str, ...] = ("psfile64.exe", "PsFile64.exe", "psfile.exe", "PsFile.exe")

PSFILE_TIMEOUT_SECONDS = 90.0
PSFILE_CLOSE_TIMEOUT_SECONDS = 60.0

_EMPTY = "—"

# [2214593372] D:\PATH\FILE.PDF  ou  [  23  ] D:\downloads\secretplans.txt
_RE_ID_PATH = re.compile(r"^\[?\s*(\d+)\s*\]?\s+(.+?)\s*$")
_RE_USER = re.compile(r"^User:\s*(.+?)\s*$", re.I)
_RE_LOCKS = re.compile(r"^Locks:\s*(\d+)\s*$", re.I)
_RE_ACCESS = re.compile(r"^Access:\s*(.+?)\s*$", re.I)
_RE_CLOSED = re.compile(r"^Closed\s+file\s+(.+?)\s+on\s+", re.I)
_RE_NO_FILES = re.compile(
    r"no\s+files\s+opened\s+remotely|nenhum\s+arquivo",
    re.I,
)
_RE_ACCESS_DENIED = re.compile(r"access\s+is\s+denied|acesso\s+negado", re.I)
_RE_RPC = re.compile(r"rpc\s+server\s+is\s+unavailable|servidor\s+rpc", re.I)


@dataclass
class RemoteOpenFile:
    """Arquivo aberto via compartilhamento, conforme o PsFile."""

    id: str
    username: str = ""
    path: str = ""
    locks: Optional[int] = None
    permissions: str = ""
    raw: str = ""


def resolve_psfile_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSFILE_NAMES)


def psfile_available(pstools_dir: str = "") -> bool:
    path = resolve_psfile_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def build_remote_target(host: str) -> str:
    h = (host or "").strip().strip("\\")
    if not h:
        return ""
    return f"\\\\{h}"


def _append_auth(args: List[str], user: str, password: str) -> None:
    u = (user or "").strip()
    if not u:
        return
    args.extend(["-u", u])
    if (password or "").strip():
        args.extend(["-p", password])


def build_psfile_argv(
    exe: str,
    host: str,
    *,
    file_id: str = "",
    path: str = "",
    close: bool = False,
    nobanner: bool = True,
    accepteula: bool = True,
    user: str = "",
    password: str = "",
) -> List[str]:
    """
    Monta argv do PsFile.

    Uso oficial (v1.04)::
        psfile [\\\\RemoteComputer [-u Username [-p Password]]]
               [[Id | path] [-c]]
    """
    target = build_remote_target(host)
    if not exe or not target:
        return []
    args = [exe]
    if accepteula:
        args.append("-accepteula")
    if nobanner:
        args.append("-nobanner")
    args.append(target)
    _append_auth(args, user, password)

    selector = (file_id or "").strip() or (path or "").strip()
    if close and not selector:
        return []
    if selector:
        args.append(selector)
    if close:
        args.append("-c")
    return args


def is_psfile_usage_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return ("usage:" in t and "psfile" in t) or (
        "lists or closes files" in t and "-c" in t
    )


def parse_psfile_output(text: str) -> List[RemoteOpenFile]:
    """Interpreta blocos ``[ID] caminho`` + User/Locks/Access."""
    rows: List[RemoteOpenFile] = []
    current: Optional[RemoteOpenFile] = None
    raw_parts: List[str] = []

    def _flush() -> None:
        nonlocal current, raw_parts
        if current is None:
            return
        current.raw = "\n".join(raw_parts).strip()
        rows.append(current)
        current = None
        raw_parts = []

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if low.startswith("psfile") or low.startswith("copyright"):
            continue
        if low.startswith("sysinternals"):
            continue
        if low.startswith("files opened remotely"):
            continue
        if _RE_NO_FILES.search(stripped):
            continue
        if is_psfile_usage_text(stripped):
            continue

        # Linha de ID: [123] C:\path  ou  123 C:\path (após strip de colchetes)
        id_match = re.match(r"^\[\s*(\d+)\s*\]\s*(.+)$", stripped)
        if id_match:
            _flush()
            current = RemoteOpenFile(
                id=id_match.group(1).strip(),
                path=id_match.group(2).strip(),
            )
            raw_parts = [stripped]
            continue

        if current is None:
            continue

        raw_parts.append(stripped)
        m_user = _RE_USER.match(stripped)
        if m_user:
            current.username = m_user.group(1).strip()
            continue
        m_locks = _RE_LOCKS.match(stripped)
        if m_locks:
            try:
                current.locks = int(m_locks.group(1))
            except ValueError:
                current.locks = None
            continue
        m_access = _RE_ACCESS.match(stripped)
        if m_access:
            current.permissions = m_access.group(1).strip()
            continue

    _flush()
    return rows


def parse_psfile_close_result(text: str) -> Tuple[bool, str]:
    """Retorna (ok, caminho_ou_mensagem) a partir da saída do fechamento."""
    t = (text or "").strip()
    if not t:
        return False, "Sem resposta do PsFile."
    m = _RE_CLOSED.search(t)
    if m:
        return True, m.group(1).strip()
    low = t.lower()
    if "closed file" in low:
        return True, t.splitlines()[0].strip()
    if _RE_NO_FILES.search(t) or "no files" in low:
        return False, "Arquivo já fechado ou não encontrado."
    if _RE_ACCESS_DENIED.search(t):
        return False, "Acesso negado ao fechar o arquivo."
    if is_psfile_usage_text(t):
        return False, "PsFile devolveu Usage ao tentar fechar."
    return False, t.splitlines()[0].strip()


def classify_psfile_error(text: str, returncode: int = 0) -> str:
    t = (text or "").strip()
    low = t.lower()
    if is_psfile_usage_text(t):
        return "usage"
    if _RE_ACCESS_DENIED.search(low):
        return "access_denied"
    if _RE_RPC.search(low):
        return "rpc_unavailable"
    if "network path was not found" in low or "caminho de rede" in low:
        return "smb_unavailable"
    if "logon failure" in low or "falha de logon" in low:
        return "bad_credentials"
    if "timed out" in low or "tempo limite" in low:
        return "timeout"
    if _RE_NO_FILES.search(t) and returncode == 0:
        return "ok"
    if returncode != 0 and t:
        return "failed"
    if returncode != 0:
        return "failed"
    return "ok"


def friendly_psfile_error(kind: str, detail: str = "") -> str:
    messages = {
        "usage": "PsFile devolveu a tela de Usage (argumentos inválidos).",
        "access_denied": "Acesso negado ao listar/fechar arquivos abertos na rede.",
        "rpc_unavailable": "RPC indisponível no host remoto.",
        "smb_unavailable": "SMB/caminho de rede indisponível no host remoto.",
        "bad_credentials": "Credencial inválida para o PsFile.",
        "timeout": "PsFile excedeu o tempo limite.",
        "tool_missing": "PsFile não encontrado na pasta PSTools configurada.",
        "already_closed": "Arquivo já fechado ou não encontrado.",
        "failed": "Falha ao executar PsFile.",
    }
    base = messages.get(kind, messages["failed"])
    extra = (detail or "").strip()
    if extra and extra.casefold() not in base.casefold():
        return f"{base} {extra}"
    return base


def filter_open_files(
    rows: Sequence[RemoteOpenFile], needle: str
) -> List[RemoteOpenFile]:
    q = (needle or "").strip().casefold()
    if not q:
        return list(rows)
    out: List[RemoteOpenFile] = []
    for row in rows:
        blob = " ".join(
            [
                row.id,
                row.username,
                row.path,
                row.permissions,
                str(row.locks if row.locks is not None else ""),
                row.raw,
            ]
        ).casefold()
        if q in blob:
            out.append(row)
    return out
