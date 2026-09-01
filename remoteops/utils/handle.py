"""Handle — pesquisa/fechamento de handles via PsExec no host remoto.

O Handle é download Sysinternals separado do PsTools. Pasta padrão:
``C:\\Handle`` (configurável em Configurações → Handle).
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from remoteops.services.ops import CredentialContext, build_psexec_argv, resolve_psexec_exe
from remoteops.utils.app_settings import KEY_HANDLE_DIR, load_setting, save_portable_settings
from remoteops.utils.pstools import (
    _probe_tool_variants,
    get_pstools_dir,
    normalize_pstools_dir,
    resolve_pstools_tool,
)

HANDLE_NAMES: Tuple[str, ...] = (
    "handle64.exe",
    "Handle64.exe",
    "handle.exe",
    "Handle.exe",
)
DEFAULT_HANDLE_DIR = r"C:\Handle"
DEFAULT_HANDLE_DIRS: Tuple[str, ...] = (DEFAULT_HANDLE_DIR,)

HANDLE_TIMEOUT_SECONDS = 180.0
HANDLE_CLOSE_TIMEOUT_SECONDS = 90.0

_EMPTY = "—"
_runtime_handle_dir: Optional[str] = None

# Tipos comuns na coluna Type do Handle
_KNOWN_TYPES = {
    "file",
    "key",
    "section",
    "event",
    "mutant",
    "semaphore",
    "thread",
    "process",
    "token",
    "directory",
    "desktop",
    "windowstation",
    "port",
    "iocompletion",
    "timer",
    "keyedevent",
    "tpworkerfactory",
    "almighty",
}

_RE_HEX_HANDLE = re.compile(r"^0x[0-9a-fA-F]+$|^[0-9a-fA-F]+$")
_RE_SEARCH_LINE = re.compile(
    r"^(?P<proc>.+?)\s+pid:\s+(?P<pid>\d+)\s+type:\s+(?P<type>\S+)"
    r"(?:\s+(?P<user>[^\s]+\\[^\s]+|\S+))?\s+"
    r"(?P<handle>[0-9A-Fa-f]+):\s+(?P<path>.+)$",
    re.I,
)
_RE_NO_MATCH = re.compile(r"no\s+matching\s+handles\s+found", re.I)
_RE_ACCESS = re.compile(r"access\s+is\s+denied|acesso\s+negado", re.I)
_RE_CLOSED = re.compile(r"handle\s+closed|closed\s+handle", re.I)


def normalize_handle_dir(path: str) -> str:
    """Normaliza pasta do Handle (aceita caminho de .exe → diretório)."""
    return normalize_pstools_dir(path)


def _load_handle_dir_from_settings() -> str:
    raw = load_setting(KEY_HANDLE_DIR, "")
    normalized = normalize_handle_dir(str(raw or ""))
    return normalized or DEFAULT_HANDLE_DIR


def get_handle_dir() -> str:
    """Pasta Handle em uso (settings.ini; padrão C:\\Handle)."""
    global _runtime_handle_dir
    if _runtime_handle_dir is None:
        _runtime_handle_dir = _load_handle_dir_from_settings()
    return _runtime_handle_dir


def set_handle_dir(path: str, *, persist: bool = True) -> str:
    """Define a pasta Handle em runtime e persiste o snapshot se pedido."""
    global _runtime_handle_dir
    normalized = normalize_handle_dir(path) or DEFAULT_HANDLE_DIR
    if persist:
        save_portable_settings({KEY_HANDLE_DIR: normalized})
    _runtime_handle_dir = normalized
    return normalized


@dataclass
class RemoteHandle:
    """Handle aberto no processo remoto."""

    process_name: str
    pid: int
    handle: str
    object_type: str = ""
    path: str = ""
    user: str = ""
    raw: str = ""


def resolve_handle_exe(handle_dir: str = "", *, pstools_dir: str = "") -> str:
    """Resolve Handle64/Handle na pasta Handle (preferência 64-bit).

    ``pstools_dir`` permanece na assinatura por compatibilidade e é ignorado.
    """
    del pstools_dir
    base = normalize_handle_dir(handle_dir) or get_handle_dir()
    candidate = resolve_pstools_tool(base, HANDLE_NAMES)
    if candidate and os.path.isfile(candidate):
        return candidate
    return os.path.join(base or DEFAULT_HANDLE_DIR, HANDLE_NAMES[0])


def handle_available(handle_dir: str = "", *, pstools_dir: str = "") -> bool:
    path = resolve_handle_exe(handle_dir, pstools_dir=pstools_dir)
    return bool(path) and os.path.isfile(path)


def probe_handle(handle_dir: Optional[str] = None) -> Dict[str, object]:
    """Status da pasta Handle. A escolha 64→32 na execução é ``resolve_handle_exe``."""
    base = normalize_handle_dir(handle_dir or get_handle_dir()) or DEFAULT_HANDLE_DIR
    dir_ok = os.path.isdir(base)
    if dir_ok:
        tool = _probe_tool_variants(base, "Handle", HANDLE_NAMES)
    else:
        tool = {
            "label": "Handle",
            "names": list(HANDLE_NAMES),
            "path": os.path.join(base, HANDLE_NAMES[0]),
            "found": False,
            "found_64": False,
            "found_32": False,
            "path_64": "",
            "path_32": "",
        }
    return {
        "dir": base,
        "dir_ok": dir_ok,
        "tool": tool,
        "tools": [tool],
        "ok_count": 1 if tool.get("found") else 0,
        "total": 1,
        "healthy": dir_ok and bool(tool.get("found")),
    }


def psexec_available(pstools_dir: str = "") -> bool:
    path = resolve_psexec_exe(pstools_dir or get_pstools_dir())
    return bool(path) and os.path.isfile(path)


def build_handle_local_argv(
    exe: str,
    *,
    search: str = "",
    list_all: bool = False,
    show_user: bool = True,
    csv_tabs: bool = True,
    close_handle: str = "",
    close_pid: Optional[int] = None,
    confirm_close: bool = False,
    nobanner: bool = True,
    accepteula: bool = True,
) -> List[str]:
    """
    Argv do Handle **local** (sem PsExec).

    Uso oficial (v5.0)::
        handle [[-a [-l]] [-v|-vt] [-u] | [-c <handle> [-y]] | [-s]]
               [-p <process>|<pid>] [name] [-nobanner]
    """
    if not exe:
        return []
    args = [exe]
    if accepteula:
        args.append("-accepteula")
    if nobanner:
        args.append("-nobanner")

    close_id = (close_handle or "").strip()
    if close_id:
        if close_pid is None:
            return []
        args.extend(["-c", _normalize_handle_id(close_id), "-p", str(int(close_pid))])
        if not confirm_close:
            args.append("-y")
        return args

    if show_user:
        args.append("-u")
    if csv_tabs:
        args.append("-vt")
    else:
        args.append("-v")

    needle = (search or "").strip()
    if needle:
        args.append(needle)
    elif not list_all:
        return []
    return args


def build_remote_handle_argv(
    handle_exe: str,
    host: str,
    *,
    psexec_exe: str = "",
    search: str = "",
    list_all: bool = False,
    show_user: bool = True,
    close_handle: str = "",
    close_pid: Optional[int] = None,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> List[str]:
    """
    PsExec + Handle com cópia temporária (``-c -f``) e elevação ``-h``.

    O PsExec remove a cópia após a execução — sem deixar Handle instalado
    permanentemente no remoto.
    """
    host_n = (host or "").strip().strip("\\")
    handle_path = handle_exe or resolve_handle_exe()
    px = psexec_exe or resolve_psexec_exe(pstools_dir or get_pstools_dir())
    if not host_n or not handle_path or not px:
        return []

    remote = build_handle_local_argv(
        handle_path,
        search=search,
        list_all=list_all,
        show_user=show_user,
        csv_tabs=True,
        close_handle=close_handle,
        close_pid=close_pid,
        confirm_close=False,
        nobanner=True,
        accepteula=True,
    )
    if not remote:
        return []

    creds = CredentialContext(user=user or "", password=password or "")
    return build_psexec_argv(
        psexec_exe=px,
        host=host_n,
        remote_argv=remote,
        creds=creds,
        extra_flags=["-accepteula", "-nobanner", "-h", "-c", "-f"],
        include_password=True,
    )


def _normalize_handle_id(value: str) -> str:
    v = (value or "").strip()
    if v.lower().startswith("0x"):
        v = v[2:]
    v = v.strip().upper()
    # Mantém hex significativo (Handle aceita 21C ou 0000021C; preferimos curto).
    stripped = v.lstrip("0")
    return stripped or "0"


def _looks_like_type(value: str) -> bool:
    return (value or "").strip().casefold() in _KNOWN_TYPES


def _looks_like_handle(value: str) -> bool:
    return bool(_RE_HEX_HANDLE.match((value or "").strip()))


def _parse_pid(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _row_from_fields(
    fields: Sequence[str], *, has_user_col: bool, raw: str
) -> Optional[RemoteHandle]:
    """
    Mapeia colunas CSV.

    Sem ``-u`` (header correto): Process, PID, Type, Handle, Name
    Com ``-u`` (header bugado no v5.0): dados reais são
    Process, PID, Type, User, Handle, Name
    """
    cols = [c.strip() for c in fields]
    if len(cols) < 4:
        return None
    process = cols[0]
    pid = _parse_pid(cols[1])
    if pid is None or not process:
        return None

    object_type = ""
    user = ""
    handle = ""
    path = ""

    if has_user_col and len(cols) >= 6:
        # Heurística: se col[2] é tipo e col[4] é handle → layout corrigido
        if _looks_like_type(cols[2]) and _looks_like_handle(cols[4]):
            object_type = cols[2]
            user = cols[3]
            handle = _normalize_handle_id(cols[4])
            path = cols[5]
            if len(cols) > 6:
                path = "\t".join(cols[5:])
        elif _looks_like_handle(cols[3]):
            # Layout conforme header impresso (raro)
            user = cols[2]
            handle = _normalize_handle_id(cols[3])
            object_type = cols[4] if len(cols) > 4 else ""
            path = cols[6] if len(cols) > 6 else (cols[5] if len(cols) > 5 else "")
        else:
            object_type = cols[2]
            user = cols[3]
            handle = _normalize_handle_id(cols[4]) if len(cols) > 4 else ""
            path = cols[5] if len(cols) > 5 else ""
    else:
        # Process, PID, Type, Handle, Name
        object_type = cols[2] if len(cols) > 2 else ""
        handle = _normalize_handle_id(cols[3]) if len(cols) > 3 else ""
        path = cols[4] if len(cols) > 4 else ""
        if len(cols) > 5:
            path = "\t".join(cols[4:])

    if not handle:
        return None
    return RemoteHandle(
        process_name=process,
        pid=pid,
        handle=handle.upper(),
        object_type=object_type or _EMPTY,
        path=path.strip() or _EMPTY,
        user=user.strip(),
        raw=raw,
    )


def parse_handle_csv(text: str) -> List[RemoteHandle]:
    """Interpreta saída ``-v`` / ``-vt`` do Handle v5.0.

    Preferência por split em tab (``-vt``): caminhos do Handle quase nunca
    contêm ``\\t``, e o ``csv`` do Python falha com newlines embutidos na
    saída completa via PsExec.
    """
    rows: List[RemoteHandle] = []
    if not (text or "").strip():
        return rows

    sample = ""
    for line in text.splitlines():
        if line.strip():
            sample = line
            break
    use_tabs = "\t" in sample

    header_seen = False
    has_user = False

    if use_tabs:
        for raw_line in text.splitlines():
            line = raw_line.strip("\r")
            if not line.strip():
                continue
            fields = [c.strip() for c in line.split("\t")]
            first = fields[0].strip() if fields else ""
            low_first = first.casefold()
            if low_first == "process" or (
                low_first.startswith("process") and len(fields) >= 4
            ):
                header_seen = True
                has_user = any(f.casefold() == "user" for f in fields)
                continue
            if low_first.startswith("nthandle") or low_first.startswith("copyright"):
                continue
            if low_first.startswith("sysinternals"):
                continue
            if _RE_NO_MATCH.search(first):
                continue
            # Ruído típico do PsExec misturado no stdout
            if low_first.startswith("connecting to") or low_first.startswith(
                "starting "
            ):
                continue
            if first.startswith("\\\\") and "psexec" in low_first:
                continue
            use_user = has_user
            if not header_seen:
                use_user = len(fields) >= 6 and _looks_like_type(fields[2])
            item = _row_from_fields(fields, has_user_col=use_user, raw=line)
            if item is not None:
                rows.append(item)
        return rows

    # Delimitador vírgula (``-v``): csv com newline='' + ignora linhas ruins
    try:
        stream = io.StringIO(text, newline="")
        reader = csv.reader(stream, delimiter=",")
        for fields in reader:
            if not fields or not any(f.strip() for f in fields):
                continue
            first = fields[0].strip()
            low_join = ",".join(f.strip() for f in fields).casefold()
            if first.casefold() == "process" or low_join.startswith("process,"):
                header_seen = True
                has_user = any(f.strip().casefold() == "user" for f in fields)
                continue
            if first.casefold().startswith("nthandle") or first.casefold().startswith(
                "copyright"
            ):
                continue
            if _RE_NO_MATCH.search(first):
                continue
            use_user = has_user
            if not header_seen:
                use_user = len(fields) >= 6 and _looks_like_type(fields[2])
            raw = ",".join(fields)
            item = _row_from_fields(fields, has_user_col=use_user, raw=raw)
            if item is not None:
                rows.append(item)
    except csv.Error:
        # Fallback bruto por linha se o csv.reader abortar no meio
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or "," not in line:
                continue
            fields = [c.strip() for c in line.split(",")]
            if fields and fields[0].casefold() == "process":
                has_user = any(f.casefold() == "user" for f in fields)
                continue
            use_user = has_user or (
                len(fields) >= 6 and _looks_like_type(fields[2])
            )
            item = _row_from_fields(fields, has_user_col=use_user, raw=line)
            if item is not None:
                rows.append(item)
    return rows


def parse_handle_text(text: str) -> List[RemoteHandle]:
    """Fallback: linhas de modo pesquisa clássico (pid:/type:)."""
    rows: List[RemoteHandle] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _RE_NO_MATCH.search(line):
            continue
        m = _RE_SEARCH_LINE.match(line)
        if not m:
            continue
        pid = _parse_pid(m.group("pid"))
        if pid is None:
            continue
        rows.append(
            RemoteHandle(
                process_name=m.group("proc").strip(),
                pid=pid,
                handle=_normalize_handle_id(m.group("handle")).upper(),
                object_type=m.group("type").strip() or _EMPTY,
                path=m.group("path").strip() or _EMPTY,
                user=(m.group("user") or "").strip(),
                raw=line,
            )
        )
    return rows


def parse_handle_output(text: str) -> List[RemoteHandle]:
    """Tenta CSV tabular; se vazio, tenta o formato texto."""
    t = text or ""
    if _RE_NO_MATCH.search(t) and "pid:" not in t.casefold() and "\t" not in t:
        # Só a mensagem de vazio
        if not any(
            line.strip() and not _RE_NO_MATCH.search(line)
            for line in t.splitlines()
            if line.strip()
            and not line.lower().startswith("nthandle")
            and not line.lower().startswith("copyright")
        ):
            return []
    try:
        csv_rows = parse_handle_csv(t)
    except (csv.Error, ValueError):
        csv_rows = []
    if csv_rows:
        return csv_rows
    return parse_handle_text(t)


def parse_handle_close_result(text: str) -> Tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, "Sem resposta do Handle."
    if _RE_CLOSED.search(t):
        return True, t.splitlines()[0].strip()
    low = t.lower()
    if "error closing" in low or "unable to close" in low:
        return False, t.splitlines()[0].strip()
    if _RE_ACCESS.search(t):
        return False, "Acesso negado ao fechar o handle."
    if is_handle_usage_text(t):
        return False, "Handle devolveu Usage ao tentar fechar."
    # PsExec pode devolver exit 0 sem texto claro
    if "error" in low or "failed" in low or "falha" in low:
        return False, t.splitlines()[0].strip()
    return True, t.splitlines()[0].strip() if t else "Handle fechado."


def is_handle_usage_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return ("usage:" in t and "handle" in t) or (
        "-vt" in t and "csv output" in t and "-nobanner" in t
    )


def classify_handle_error(text: str, returncode: int = 0) -> str:
    t = (text or "").strip()
    low = t.lower()
    if is_handle_usage_text(t):
        return "usage"
    # Handle v5.0 devolve exit 1 quando a pesquisa não acha nada.
    if _RE_NO_MATCH.search(low):
        return "ok"
    if _RE_ACCESS.search(low):
        return "access_denied"
    if "could not start" in low and "handle" in low:
        return "copy_failed"
    if "the network path was not found" in low:
        return "smb_unavailable"
    if "logon failure" in low or "falha de logon" in low:
        return "bad_credentials"
    if "rpc" in low and "unavailable" in low:
        return "rpc_unavailable"
    if "timed out" in low or "tempo limite" in low:
        return "timeout"
    if "psexec" in low and ("not found" in low or "não encontrado" in low):
        return "psexec_missing"
    if returncode != 0 and t:
        return "failed"
    if returncode != 0:
        return "failed"
    return "ok"


def friendly_handle_error(kind: str, detail: str = "") -> str:
    messages = {
        "usage": "Handle devolveu a tela de Usage (argumentos inválidos).",
        "access_denied": "Acesso negado ao pesquisar/fechar handles.",
        "copy_failed": "Falha ao copiar/executar o Handle no host remoto via PsExec.",
        "smb_unavailable": "SMB indisponível para executar o Handle remoto.",
        "bad_credentials": "Credencial inválida para o PsExec/Handle.",
        "rpc_unavailable": "RPC indisponível no host remoto.",
        "timeout": "Handle excedeu o tempo limite.",
        "tool_missing": (
            "Handle não encontrado (Configurações → Handle)."
        ),
        "psexec_missing": "PsExec não encontrado — necessário para Handle remoto.",
        "handle_missing": "Handle não encontrado para o PID informado.",
        "process_gone": "Processo encerrado durante a consulta.",
        "failed": "Falha ao executar Handle via PsExec.",
    }
    base = messages.get(kind, messages["failed"])
    extra = (detail or "").strip()
    if extra and extra.casefold() not in base.casefold():
        return f"{base} {extra}"
    return base


def filter_handles(rows: Sequence[RemoteHandle], needle: str) -> List[RemoteHandle]:
    q = (needle or "").strip().casefold()
    if not q:
        return list(rows)
    out: List[RemoteHandle] = []
    for row in rows:
        blob = " ".join(
            [
                row.process_name,
                str(row.pid),
                row.handle,
                row.object_type,
                row.path,
                row.user,
                row.raw,
            ]
        ).casefold()
        if q in blob:
            out.append(row)
    return out
