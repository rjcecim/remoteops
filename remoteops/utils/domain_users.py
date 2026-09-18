"""Nome completo de conta de domínio via NetUserGetInfo (netapi32).

Não spawna ``net.exe`` / ``net user /domain``: o EDR trata esse LOLBin como
enumeração de AD. A consulta é pontual (um SAM) e fica em cache.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

_SKIP_DOMAINS = frozenset(
    {
        ".",
        "nt authority",
        "nt service",
        "window manager",
        "font driver host",
        "iis apppool",
        "application virtualization",
    }
)
_INVALID_SAM_CHARS = frozenset('\\/[]:|<>+=;,?*"')

_cache_lock = threading.Lock()
_full_name_cache: dict[str, str] = {}
_netapi32 = None

FullNameQuery = Callable[[str, str], str]


def clear_domain_user_cache() -> None:
    with _cache_lock:
        _full_name_cache.clear()


def split_session_accounts(display: str, empty: str = "—") -> list[str]:
    """Parte ``DOMÍNIO\\user  ·  DOMÍNIO\\outro`` em contas individuais."""
    text = (display or "").strip()
    if not text or text == empty:
        return []
    out: list[str] = []
    for part in text.split("·"):
        name = part.strip()
        if not name or name == empty:
            continue
        out.append(name)
    return out


def sam_account_name(account: str) -> str:
    """SAM após ``DOMÍNIO\\`` / ``DOMÍNIO/``; senão o texto inteiro."""
    text = (account or "").strip().strip("\"'")
    if not text:
        return ""
    if "\\" in text:
        text = text.rsplit("\\", 1)[-1]
    elif "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip()


def logon_domain(account: str) -> str:
    """Prefixo ``DOMÍNIO`` de ``DOMÍNIO\\user``; vazio se for builtin/local."""
    raw = (account or "").strip()
    if "\\" not in raw:
        return ""
    domain, _, _user = raw.partition("\\")
    label = domain.strip().strip("\\")
    if not label or _netbios_label(label) in _SKIP_DOMAINS:
        return ""
    return label


def is_safe_sam(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    if name in {".", ".."}:
        return False
    if name.startswith("/") or name.startswith("-"):
        return False
    return not any(ch in _INVALID_SAM_CHARS or ch.isspace() or ord(ch) < 32 for ch in name)


def _netbios_label(name: str) -> str:
    return (name or "").strip().strip("\\").split(".", 1)[0].casefold()


def is_machine_local_account(account: str, hostname: str = "") -> bool:
    """True para ``.\\user``, NT AUTHORITY e ``HOSTNAME\\user`` da própria máquina."""
    raw = (account or "").strip()
    if not raw or "\\" not in raw:
        return False
    domain, _, _user = raw.partition("\\")
    label = _netbios_label(domain)
    if not label or label in _SKIP_DOMAINS:
        return True
    host = _netbios_label(hostname)
    return bool(host) and label == host


def lookup_domain_user_full_name(
    account: str,
    hostname: str = "",
    *,
    query: Optional[FullNameQuery] = None,
) -> str:
    """Nome completo via NetUserGetInfo. Conta local da máquina → vazio."""
    if is_machine_local_account(account, hostname):
        return ""
    sam = sam_account_name(account)
    if not is_safe_sam(sam):
        return ""
    key = sam.casefold()
    with _cache_lock:
        cached = _full_name_cache.get(key)
    if cached is not None:
        return cached
    lookup = query or query_netapi_full_name
    name = lookup(sam, logon_domain(account)) or ""
    name = name.strip()
    with _cache_lock:
        _full_name_cache[key] = name
    return name


def lookup_domain_full_names(
    user_display: str,
    hostname: str = "",
    *,
    empty: str = "—",
    query: Optional[FullNameQuery] = None,
) -> list[str]:
    """Um nome completo por conta do texto de usuário ativo (vazio se não houver)."""
    return [
        lookup_domain_user_full_name(account, hostname, query=query)
        for account in split_session_accounts(user_display, empty=empty)
    ]


def query_netapi_full_name(sam: str, domain: str = "") -> str:
    """Consulta pontual NetUserGetInfo (nível 10). Sem processo filho."""
    if not is_safe_sam(sam):
        return ""
    if domain:
        for server in (domain, rf"\\{domain}"):
            name = _user_info_full_name(server, sam)
            if name:
                return name
    dc = _primary_dc(domain)
    if dc:
        return _user_info_full_name(dc, sam)
    return ""


def _api():
    global _netapi32
    if _netapi32 is not None:
        return _netapi32
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
    netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
    netapi32.NetApiBufferFree.restype = wintypes.DWORD
    netapi32.NetUserGetInfo.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    netapi32.NetUserGetInfo.restype = wintypes.DWORD
    netapi32.NetGetDCName.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    netapi32.NetGetDCName.restype = wintypes.DWORD
    _netapi32 = netapi32
    return netapi32


def _user_info_full_name(server: Optional[str], sam: str) -> str:
    if sys.platform != "win32":
        return ""
    api = _api()
    if api is None:
        return ""
    import ctypes
    from ctypes import wintypes

    class USER_INFO_10(ctypes.Structure):
        _fields_ = [
            ("usri10_name", wintypes.LPWSTR),
            ("usri10_comment", wintypes.LPWSTR),
            ("usri10_usr_comment", wintypes.LPWSTR),
            ("usri10_full_name", wintypes.LPWSTR),
        ]

    buf = ctypes.c_void_p()
    try:
        rc = api.NetUserGetInfo(server or None, sam, 10, ctypes.byref(buf))
    except Exception:
        return ""
    if rc != 0 or not buf:
        return ""
    try:
        info = ctypes.cast(buf, ctypes.POINTER(USER_INFO_10)).contents
        return str(info.usri10_full_name or "").strip()
    except Exception:
        return ""
    finally:
        api.NetApiBufferFree(buf)


def _primary_dc(domain: str = "") -> str:
    if sys.platform != "win32":
        return ""
    api = _api()
    if api is None:
        return ""
    import ctypes

    buf = ctypes.c_void_p()
    try:
        rc = api.NetGetDCName(None, domain or None, ctypes.byref(buf))
    except Exception:
        return ""
    if rc != 0 or not buf:
        return ""
    try:
        return str(ctypes.wstring_at(buf.value) or "").strip()
    except Exception:
        return ""
    finally:
        api.NetApiBufferFree(buf)
