"""Enumeração de sessões interativas de um host Windows remoto.

Usa a API WTS (mesmo backend de ``query session /server:``). Credenciais
opcionais autenticam em ``\\\\host\\IPC$`` só durante a consulta.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from remoteops.core.win_cmd import CREATE_NO_WINDOW
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


@dataclass(frozen=True)
class RemoteSession:
    session_id: int
    name: str = ""
    username: str = ""
    state: str = ""

    def label(self) -> str:
        parts = [str(self.session_id)]
        if self.name:
            parts.append(self.name)
        if self.state:
            parts.append(self.state)
        if self.username:
            parts.append(self.username)
        return " — ".join(parts)


class WTS_SESSION_INFOW(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.DWORD),
        ("pWinStationName", wintypes.LPWSTR),
        ("State", wintypes.DWORD),
    ]


class NETRESOURCEW(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
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
        sessions.append(
            RemoteSession(
                session_id=session_id,
                name=name,
                username=username,
                state=state,
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


def _wts_username(wts, handle, session_id: int) -> str:
    buf = wintypes.LPWSTR()
    nbytes = wintypes.DWORD()
    try:
        ok = wts.WTSQuerySessionInformationW(
            handle, int(session_id), 5, ctypes.byref(buf), ctypes.byref(nbytes)
        )
    except OverflowError:
        return ""
    if not ok or not buf:
        return ""
    try:
        return str(buf.value or "").strip()
    finally:
        wts.WTSFreeMemory(buf)


def _enumerate_wts(host: str) -> List[RemoteSession]:
    try:
        wts = _wtsapi()
        handle = wts.WTSOpenServerW(host)
        if not handle:
            return []
        info_ptr = ctypes.POINTER(WTS_SESSION_INFOW)()
        count = wintypes.DWORD(0)
        sessions: List[RemoteSession] = []
        try:
            ok = wts.WTSEnumerateSessionsW(
                handle, 0, 1, ctypes.byref(info_ptr), ctypes.byref(count)
            )
            if not ok or not info_ptr:
                return []
            for i in range(int(count.value)):
                item = info_ptr[i]
                name = str(item.pWinStationName or "").strip()
                sessions.append(
                    RemoteSession(
                        session_id=int(item.SessionId),
                        name=name,
                        username=_wts_username(wts, handle, int(item.SessionId)),
                        state=WTS_CONNECT_STATES.get(int(item.State), str(item.State)),
                    )
                )
        finally:
            if info_ptr:
                wts.WTSFreeMemory(info_ptr)
            wts.WTSCloseServer(handle)
        sessions.sort(key=lambda s: s.session_id)
        return sessions
    except Exception:
        return []


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
    creationflags = CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
            shell=False,
        )
    except Exception:
        return []
    finally:
        creds.clear()
    text = f"{result.stdout or ''}{result.stderr or ''}"
    return parse_query_session_output(text)


def _query_session_cli(host: str) -> List[RemoteSession]:
    creationflags = CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    for argv in (
        ["query", "session", f"/server:{host}"],
        ["qwinsta", f"/server:{host}"],
    ):
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=creationflags,
                shell=False,
            )
        except Exception:
            continue
        text = f"{result.stdout or ''}{result.stderr or ''}"
        found = parse_query_session_output(text)
        if found:
            return found
    return []


def _ipc_connect(host: str, user: str, password: str) -> bool:
    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    nr = NETRESOURCEW()
    nr.dwType = 1  # RESOURCETYPE_DISK
    nr.lpRemoteName = f"\\\\{host}\\IPC$"
    # 4 = CONNECT_TEMPORARY
    rc = mpr.WNetAddConnection2W(ctypes.byref(nr), password or None, user or None, 4)
    # 0 = ok; 85 = já conectado; 1219 = credencial já existente na sessão
    return rc in (0, 85, 1219)


def _ipc_disconnect(host: str) -> None:
    try:
        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        mpr.WNetCancelConnection2W(f"\\\\{host}\\IPC$", 0, True)
    except Exception:
        pass


def list_remote_sessions(
    host: str,
    *,
    user: str = "",
    password: str = "",
) -> Tuple[List[RemoteSession], str]:
    """
    Lista sessões do host.

    Retorna ``(sessoes, erro)``. ``erro`` vazio significa sucesso
    (a lista pode ser vazia se o host não tiver sessões).
    """
    host = normalize_host(host)
    if not host or not is_valid_host(host):
        return [], "Host inválido."

    connected = False
    user = (user or "").strip()
    try:
        if user:
            connected = _ipc_connect(host, user, password or "")
        sessions = _enumerate_wts(host)
        if not sessions:
            sessions = _query_session_cli(host)
        if not sessions:
            sessions = _query_session_psexec(host, user=user, password=password)
        if sessions:
            return sessions, ""
        if user and not connected:
            return [], "Não foi possível autenticar para listar as sessões."
        return [], "Não foi possível listar as sessões do host (RPC/permissão)."
    except Exception as exc:
        return [], str(exc) or "Falha ao consultar sessões."
    finally:
        if connected:
            _ipc_disconnect(host)


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
