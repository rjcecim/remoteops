"""Conexão temporária a ``\\\\HOST\\IPC$`` para autenticar RPC (WTS, Registro, spooler).

Não coloca senha em linha de comando, JSON, log ou arquivo. Encerrar apenas a
conexão criada por este fluxo — nunca apagar uma conexão preexistente.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

ERROR_SUCCESS = 0
ERROR_ALREADY_ASSIGNED = 85
ERROR_SESSION_CREDENTIAL_CONFLICT = 1219

_CONNECT_TEMPORARY = 4  # CONNECT_TEMPORARY
_RESOURCETYPE_DISK = 1


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


@dataclass(frozen=True)
class IpcAuthResult:
    """Resultado de ``WNetAddConnection2W`` em ``\\\\host\\IPC$``."""

    host: str
    skipped: bool = False
    created: bool = False
    preexisting: bool = False
    conflict: bool = False
    winerror: Optional[int] = None
    error: str = ""

    @property
    def connected(self) -> bool:
        """True quando a identidade atual pode ser usada para RPC."""
        if self.conflict or self.error:
            return False
        return self.skipped or self.created or self.preexisting


def ipc_share(host: str) -> str:
    name = (host or "").strip().strip("\\")
    if not name:
        return ""
    return f"\\\\{name}\\IPC$"


def interpret_ipc_result(rc: int, host: str) -> IpcAuthResult:
    """Interpreta o código de ``WNetAddConnection2W`` sem chamar a API."""
    name = (host or "").strip().strip("\\")
    share = ipc_share(name)
    code = int(rc)
    if code == ERROR_SUCCESS:
        return IpcAuthResult(host=name, created=True, winerror=code)
    if code == ERROR_ALREADY_ASSIGNED:
        return IpcAuthResult(host=name, preexisting=True, winerror=code)
    if code == ERROR_SESSION_CREDENTIAL_CONFLICT:
        return IpcAuthResult(
            host=name,
            conflict=True,
            winerror=code,
            error=f"Conflito de credenciais ao acessar {share}.",
        )
    return IpcAuthResult(
        host=name,
        winerror=code,
        error=f"Não foi possível autenticar em {share} (WinError {code}).",
    )


def connect_ipc(host: str, user: str = "", password: str = "") -> IpcAuthResult:
    """Autentica em ``\\\\host\\IPC$`` com ``CONNECT_TEMPORARY``.

    Sem usuário, não chama WNet — a identidade do processo atual é usada.
    ``1219`` nunca é tratado como sucesso (a conexão existente pode ser outra conta).
    """
    name = (host or "").strip().strip("\\")
    if not name:
        return IpcAuthResult(host="", error="Host inválido.")
    account = (user or "").strip()
    if not account:
        return IpcAuthResult(host=name, skipped=True)

    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    nr = NETRESOURCEW()
    nr.dwType = _RESOURCETYPE_DISK
    nr.lpRemoteName = ipc_share(name)
    rc = mpr.WNetAddConnection2W(
        ctypes.byref(nr),
        password or None,
        account,
        _CONNECT_TEMPORARY,
    )
    return interpret_ipc_result(int(rc), name)


def disconnect_ipc(host: str) -> None:
    """Cancela somente ``\\\\host\\IPC$``. Não falha se a conexão já não existir."""
    share = ipc_share(host)
    if not share:
        return
    try:
        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        mpr.WNetCancelConnection2W(share, 0, True)
    except Exception:
        pass


def release_ipc(auth: IpcAuthResult) -> None:
    """Encerra só a conexão criada por ``connect_ipc`` (``created=True``)."""
    if auth is None or not auth.created:
        return
    disconnect_ipc(auth.host)
