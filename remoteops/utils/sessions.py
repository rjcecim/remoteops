"""Enumeração de sessões interativas de um host Windows remoto.

Usa a API WTS (mesmo backend de ``query session /server:``). Credenciais
opcionais autenticam em ``\\\\host\\IPC$`` só durante a consulta.

O fallback PsExec NÃO é automático: só ocorre com
``allow_psexec_fallback=True``. A aba Impressoras nunca o utiliza.
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.win_cmd import run_captured
from remoteops.utils.ipc_auth import connect_ipc, release_ipc
from remoteops.utils.ping import is_valid_host, normalize_host

WTS_CONNECT_STATES = {
    0: "Ativa",
    1: "Conectada",
    2: "ConnectQuery",
    3: "Shadow",
    4: "Desconectada",
    5: "Ociosa",
    6: "Listen",
    7: "Reset",
    8: "Down",
    9: "Init",
}

_HEADER_RE = re.compile(
    r"sessionname|username|sess[aã]o|nome de usu|estado|\bid\b",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"connecting to|starting |exited on|error code|connecting\.\.\.|started",
    re.IGNORECASE,
)


# WTS_INFO_CLASS
WTSUserName = 5
WTSDomainName = 7


@dataclass(frozen=True)
class RemoteSession:
    session_id: int
    name: str = ""
    username: str = ""
    state: str = ""
    domain: str = ""

    def label(self) -> str:
        """Rótulo único para ComboBox (PsExec e Mensagem).

        Com usuário: ``0102052 — Console — ID 3 — Ativa``
        Sem usuário: ``Services — ID 0 — Desconectada``
        """
        parts: List[str] = []
        user = (self.username or "").strip()
        if user:
            parts.append(user)
        name = (self.name or "").strip()
        if name:
            parts.append(name)
        parts.append(f"ID {self.session_id}")
        state = (self.state or "").strip()
        if state:
            parts.append(state)
        return " — ".join(parts)


class WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("pWinStationName", wintypes.LPWSTR),
        ("State", wintypes.DWORD),
    ]


def parse_query_session_output(text: str) -> List[RemoteSession]:
    """Interpreta a saída de ``query session`` / ``qwinsta`` (EN/PT)."""
    sessions: List[RemoteSession] = []
    seen: set[int] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _NOISE_RE.search(line):
            continue
        if _HEADER_RE.search(line) and not any(ch.isdigit() for ch in line):
            continue
        if line.lower().startswith("sessionname") or line.lower().startswith("sessão"):
            continue
        line = line.lstrip(">").strip()
        tokens = line.split()
        id_idx = next((i for i, tok in enumerate(tokens) if tok.isdigit()), None)
        if id_idx is None:
            continue
        session_id = int(tokens[id_idx])
        if session_id in seen:
            continue
        seen.add(session_id)
        name = tokens[0] if id_idx > 0 else ""
        username = " ".join(tokens[1:id_idx]) if id_idx > 1 else ""
        state = tokens[id_idx + 1] if id_idx + 1 < len(tokens) else ""
        domain = ""
        if "\\" in username:
            domain, username = username.split("\\", 1)
        sessions.append(
            RemoteSession(
                session_id=session_id,
                name=name,
                username=username,
                state=state,
                domain=domain,
            )
        )
    return sessions


_WTS_HANDLE = ctypes.c_void_p


def _wtsapi():
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    wts.WTSOpenServerW.argtypes = [wintypes.LPCWSTR]
    wts.WTSOpenServerW.restype = _WTS_HANDLE
    wts.WTSCloseServer.argtypes = [_WTS_HANDLE]
    wts.WTSCloseServer.restype = None
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wts.WTSFreeMemory.restype = None
    wts.WTSEnumerateSessionsW.argtypes = [
        _WTS_HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFOW)),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wts.WTSEnumerateSessionsW.restype = wintypes.BOOL
    wts.WTSQuerySessionInformationW.argtypes = [
        _WTS_HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    return wts


def _wts_query_string(wts, handle, session_id: int, info_class: int) -> str:
    buf = wintypes.LPWSTR()
    nbytes = wintypes.DWORD()
    try:
        ok = wts.WTSQuerySessionInformationW(
            handle, int(session_id), int(info_class), ctypes.byref(buf), ctypes.byref(nbytes)
        )
    except OverflowError:
        return ""
    if not ok or not buf:
        return ""
    try:
        return str(buf.value or "").strip()
    finally:
        wts.WTSFreeMemory(buf)


def _wts_username(wts, handle, session_id: int) -> str:
    return _wts_query_string(wts, handle, session_id, WTSUserName)


def _wts_domain(wts, handle, session_id: int) -> str:
    return _wts_query_string(wts, handle, session_id, WTSDomainName)


def _enumerate_wts(host: str) -> Tuple[List[RemoteSession], str]:
    try:
        wts = _wtsapi()
        handle = wts.WTSOpenServerW(host)
        if not handle:
            return [], "Não foi possível consultar as sessões pela API WTS."
        info_ptr = ctypes.POINTER(WTS_SESSION_INFOW)()
        count = wintypes.DWORD(0)
        sessions: List[RemoteSession] = []
        try:
            ok = wts.WTSEnumerateSessionsW(
                handle, 0, 1, ctypes.byref(info_ptr), ctypes.byref(count)
            )
            if not ok or not info_ptr:
                return [], "Não foi possível consultar as sessões pela API WTS."
            for i in range(int(count.value)):
                item = info_ptr[i]
                name = str(item.pWinStationName or "").strip()
                username = _wts_username(wts, handle, int(item.SessionId))
                domain = _wts_domain(wts, handle, int(item.SessionId))
                if "\\" in username:
                    embedded, username = username.split("\\", 1)
                    domain = domain or embedded
                sessions.append(
                    RemoteSession(
                        session_id=int(item.SessionId),
                        name=name,
                        username=username,
                        state=WTS_CONNECT_STATES.get(int(item.State), str(item.State)),
                        domain=domain,
                    )
                )
        finally:
            if info_ptr:
                wts.WTSFreeMemory(info_ptr)
            wts.WTSCloseServer(handle)
        sessions.sort(key=lambda s: s.session_id)
        return sessions, ""
    except Exception:
        return [], "Não foi possível consultar as sessões pela API WTS."


def _query_session_psexec(
    host: str,
    *,
    user: str = "",
    password: str = "",
) -> List[RemoteSession]:
    """Consulta ``query session`` no próprio host via PsExec (mais confiável)."""
    try:
        from remoteops.services.ops import (
            CredentialContext,
            build_psexec_argv,
            resolve_psexec_exe,
        )
        from remoteops.utils.pstools import get_pstools_dir
    except Exception:
        return []

    psexec = resolve_psexec_exe(get_pstools_dir())
    creds = CredentialContext(user=user or "", password=password or "")
    argv = build_psexec_argv(
        psexec_exe=psexec,
        host=host,
        remote_argv=["query", "session"],
        creds=creds,
        extra_flags=["-accepteula", "-nobanner", "-s"],
        include_password=True,
    )
    try:
        result = run_captured(argv, timeout=20)
    except Exception:
        return []
    finally:
        creds.clear()
    text = (
        decode_console_bytes(result.stdout or b"")
        + decode_console_bytes(result.stderr or b"")
    )
    return parse_query_session_output(text)


def _query_session_cli(host: str) -> Tuple[List[RemoteSession], str]:
    last_error = ""
    for argv in (
        ["query", "session", f"/server:{host}"],
        ["qwinsta", f"/server:{host}"],
    ):
        try:
            result = run_captured(argv, timeout=8)
        except Exception as exc:
            last_error = str(exc) or last_error
            continue
        text = (
            decode_console_bytes(result.stdout or b"")
            + decode_console_bytes(result.stderr or b"")
        )
        found = parse_query_session_output(text)
        if found:
            return found, ""
        if text.strip():
            last_error = text.strip()[:240]
    return [], last_error


def list_remote_sessions(
    host: str,
    *,
    user: str = "",
    password: str = "",
    allow_psexec_fallback: bool = False,
) -> Tuple[List[RemoteSession], str]:
    """
    Lista sessões do host.

    Retorna ``(sessoes, erro)``. ``erro`` vazio significa sucesso
    (a lista pode ser vazia se o host não tiver sessões).

    ``allow_psexec_fallback`` é ``False`` por omissão. A aba Impressoras
    nunca habilita esse fallback.
    """
    host = normalize_host(host)
    if not host or not is_valid_host(host):
        return [], "Host inválido."

    auth = connect_ipc(host, user or "", password or "")
    try:
        if auth.conflict:
            return [], auth.error
        sessions, wts_error = _enumerate_wts(host)
        if sessions:
            return sessions, ""
        sessions, cli_error = _query_session_cli(host)
        if sessions:
            return sessions, ""
        if allow_psexec_fallback:
            sessions = _query_session_psexec(host, user=user, password=password)
            if sessions:
                return sessions, ""
        if user and not auth.connected and auth.error:
            return [], auth.error
        if wts_error:
            return [], wts_error
        if cli_error:
            return [], "Não foi possível consultar as sessões pela API WTS."
        return [], "Não foi possível consultar as sessões pela API WTS."
    except Exception as exc:
        return [], str(exc) or "Falha ao consultar sessões."
    finally:
        release_ipc(auth)


def session_choices(sessions: Sequence[RemoteSession]) -> List[RemoteSession]:
    """Cópia estável, sem duplicar IDs."""
    seen: set[int] = set()
    out: List[RemoteSession] = []
    for item in sessions:
        if item.session_id in seen:
            continue
        seen.add(item.session_id)
        out.append(item)
    return out
