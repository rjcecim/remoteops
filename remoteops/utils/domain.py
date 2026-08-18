"""USERDOMAIN de um host Windows e composição do campo Usuário do PsExec (-u)."""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

from remoteops.core.win_cmd import CREATE_NO_WINDOW
from remoteops.utils.ping import is_valid_host, normalize_host

NBTSTAT_TIMEOUT_SEC = 3.0
_WORKGROUP_NAMES = frozenset({"workgroup", "mshome", "grupodetrabalho"})
LOCAL_PREFIX = ".\\"

# NetGetJoinInformation
_NETSETUP_WORKGROUP = 2
_NETSETUP_DOMAIN = 3


@dataclass(frozen=True)
class HostUserDomain:
    """Equivalente remoto de ``$env:USERDOMAIN`` (NetBIOS)."""

    name: str = ""
    computer: str = ""
    dns: str = ""
    is_workgroup: bool = False
    source: str = ""

    @property
    def prefix(self) -> str:
        name = (self.name or "").strip().strip("\\")
        return f"{name}\\" if name else ""


def is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address((host or "").strip().strip("[]"))
        return True
    except ValueError:
        return False


def netbios_from_dns(dns: str) -> str:
    """Primeiro rótulo do sufixo DNS, no estilo NetBIOS (máx. 15)."""
    label = (dns or "").strip().strip(".").split(".", 1)[0].strip()
    if not label:
        return ""
    cleaned = "".join(ch for ch in label if ch.isalnum() or ch in "-_")
    return cleaned[:15].upper()


def userdomain_prefix(name: str) -> str:
    cleaned = (name or "").strip().strip("\\")
    return f"{cleaned}\\" if cleaned else ""


def domain_hint_from_hostname(host: str) -> HostUserDomain:
    """Sufixo DNS embutido no nome (``pc.contoso.local``) — sem rede."""
    h = normalize_host(host)
    if not h or is_ip_address(h) or "." not in h:
        return HostUserDomain()
    dns = h.split(".", 1)[1].strip().strip(".")
    if not dns or is_ip_address(dns):
        return HostUserDomain()
    return HostUserDomain(
        name=netbios_from_dns(dns),
        dns=dns,
        is_workgroup=False,
        source="hostname",
    )


def split_user_field(text: str) -> Tuple[str, str]:
    """Separa prefixo (``DOMÍNIO\\`` ou ``.\\``) e o restante do usuário."""
    raw = text or ""
    if raw.startswith(LOCAL_PREFIX):
        return LOCAL_PREFIX, raw[len(LOCAL_PREFIX) :]
    if "\\" not in raw:
        return "", raw
    prefix, _, user = raw.partition("\\")
    return f"{prefix}\\", user


def is_prefix_only(text: str) -> bool:
    prefix, user = split_user_field(text)
    return bool(prefix) and not (user or "").strip()


def effective_auth_username(text: str) -> str:
    """Valor de ``-u``: vazio se o campo só tem o prefixo, sem usuário."""
    raw = (text or "").strip()
    if not raw or is_prefix_only(raw):
        return ""
    return raw


def rewrite_user_field_for_local(text: str, userdomain: str) -> str:
    """
    Se o usuário digitou ``.\\``, troca o prefixo de domínio por conta local.

    Exemplos (domínio CONTOSO):
        ``CONTOSO\\.\\`` → ``.\\``
        ``CONTOSO\\.\\joao`` → ``.\\joao``
        ``.\\CONTOSO\\joao`` → ``.\\joao``
        ``.\\joao`` → ``.\\joao``
    """
    if not text:
        return text
    disc = userdomain_prefix(userdomain)

    if text.startswith(LOCAL_PREFIX):
        rest = text[len(LOCAL_PREFIX) :]
        if disc and rest.startswith(disc):
            rest = rest[len(disc) :]
        while rest.startswith(LOCAL_PREFIX):
            rest = rest[len(LOCAL_PREFIX) :]
        return LOCAL_PREFIX + rest

    if disc and text.startswith(disc):
        rest = text[len(disc) :]
        if rest.startswith(LOCAL_PREFIX):
            return LOCAL_PREFIX + rest[len(LOCAL_PREFIX) :]
    return text


def apply_discovered_userdomain(
    text: str,
    discovered: str,
    previous: str = "",
) -> Optional[str]:
    """
    Novo texto do campo quando o USERDOMAIN chega ou muda.

    Não mexe em conta local (``.\\``), UPN (``user@domínio``) nem em
    ``OUTRO\\user`` digitado à mão. Devolve ``None`` se não deve alterar.
    """
    discovered = (discovered or "").strip().strip("\\")
    if not discovered:
        return None

    raw = text or ""
    stripped = raw.strip()
    prefix, user = split_user_field(raw)
    new_prefix = userdomain_prefix(discovered)
    old_prefix = userdomain_prefix(previous)

    if prefix == LOCAL_PREFIX:
        return None
    if "@" in stripped and "\\" not in stripped:
        return None
    if not stripped:
        return new_prefix
    if not prefix:
        return new_prefix + raw
    if prefix == new_prefix or (old_prefix and prefix == old_prefix):
        return new_prefix + user
    return None


def clear_discovered_prefix(text: str, previous: str) -> Optional[str]:
    """Remove o prefixo automático se o host foi limpo e não há usuário."""
    old_prefix = userdomain_prefix(previous)
    if not old_prefix:
        return None
    prefix, user = split_user_field(text or "")
    if prefix != old_prefix:
        return None
    if (user or "").strip():
        return None
    return ""


def parse_nbtstat_names(output: str) -> Tuple[str, str]:
    """Extrai (GROUP ``<00>``, UNIQUE ``<00>``) da saída do ``nbtstat``."""
    group = ""
    unique = ""
    if not output:
        return group, unique
    group_markers = ("GROUP", "GRUPO")
    unique_markers = ("UNIQUE", "ÚNICO", "UNICO")
    for raw in output.splitlines():
        line = raw.strip()
        if "<00>" not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        name = parts[0].strip()
        if not name or name.startswith(("<", "___")):
            continue
        if not is_valid_host(name):
            continue
        upper = line.upper()
        is_unique = any(m in upper for m in unique_markers)
        is_group = any(m in upper for m in group_markers)
        if is_unique and not unique:
            unique = name
        elif is_group and not group:
            group = name
    return group, unique


def _decode_console(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("oem", "mbcs", "cp850", "utf-8"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _run_nbtstat(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=NBTSTAT_TIMEOUT_SEC,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return ""
    return _decode_console(result.stdout or b"")


def _host_ip(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ""
    if not infos:
        return ""
    return str(infos[0][4][0] or "")


def _dns_suffix_of(host: str) -> str:
    try:
        fqdn = (socket.getfqdn(host) or "").strip().strip(".")
    except OSError:
        return ""
    if not fqdn or is_ip_address(fqdn) or "." not in fqdn:
        return ""
    suffix = fqdn.split(".", 1)[1].strip().strip(".")
    if not suffix or is_ip_address(suffix):
        return ""
    return suffix


def nbtstat_userdomain(host: str) -> HostUserDomain:
    """USERDOMAIN via NetBIOS: domínio (GROUP) ou nome do PC (workgroup)."""
    h = normalize_host(host)
    if not is_valid_host(h):
        return HostUserDomain()

    group, unique = parse_nbtstat_names(_run_nbtstat(["nbtstat", "-a", h]))
    if not group and not unique:
        ip = h if is_ip_address(h) else _host_ip(h)
        if ip and ip.casefold() != h.casefold():
            group, unique = parse_nbtstat_names(_run_nbtstat(["nbtstat", "-A", ip]))

    workgroup = bool(group) and group.casefold() in _WORKGROUP_NAMES
    if workgroup:
        name = unique or ""
    else:
        name = group or unique or ""
    if not name:
        return HostUserDomain()
    return HostUserDomain(
        name=name,
        computer=unique,
        is_workgroup=workgroup,
        source="nbtstat",
    )


def _netapi_userdomain(host: str) -> HostUserDomain:
    """NetGetJoinInformation + NetWkstaGetInfo (RPC do host)."""
    if sys.platform != "win32":
        return HostUserDomain()
    h = normalize_host(host)
    if not is_valid_host(h):
        return HostUserDomain()

    import ctypes
    from ctypes import wintypes

    netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
    netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
    netapi32.NetApiBufferFree.restype = wintypes.DWORD
    netapi32.NetGetJoinInformation.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(ctypes.c_uint),
    ]
    netapi32.NetGetJoinInformation.restype = wintypes.DWORD

    class WKSTA_INFO_100(ctypes.Structure):
        _fields_ = [
            ("wki100_platform_id", wintypes.DWORD),
            ("wki100_computername", wintypes.LPWSTR),
            ("wki100_langroup", wintypes.LPWSTR),
            ("wki100_ver_major", wintypes.DWORD),
            ("wki100_ver_minor", wintypes.DWORD),
        ]

    netapi32.NetWkstaGetInfo.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    netapi32.NetWkstaGetInfo.restype = wintypes.DWORD

    join_name = ""
    join_status = 0
    computer = ""
    langroup = ""

    for server in (rf"\\{h}", h):
        name_buf = wintypes.LPWSTR()
        status_buf = ctypes.c_uint(0)
        rc = netapi32.NetGetJoinInformation(
            server, ctypes.byref(name_buf), ctypes.byref(status_buf)
        )
        if rc == 0 and name_buf:
            try:
                join_name = str(name_buf.value or "").strip()
                join_status = int(status_buf.value)
            finally:
                netapi32.NetApiBufferFree(name_buf)

        buf = ctypes.c_void_p()
        rc = netapi32.NetWkstaGetInfo(server, 100, ctypes.byref(buf))
        if rc == 0 and buf:
            try:
                info = ctypes.cast(buf, ctypes.POINTER(WKSTA_INFO_100)).contents
                computer = str(info.wki100_computername or "").strip()
                langroup = str(info.wki100_langroup or "").strip()
            finally:
                netapi32.NetApiBufferFree(buf)

        if join_name or computer or langroup:
            break

    if not join_name and not computer and not langroup:
        return HostUserDomain()

    workgroup = join_status == _NETSETUP_WORKGROUP or (
        bool(langroup) and langroup.casefold() in _WORKGROUP_NAMES and join_status != _NETSETUP_DOMAIN
    )
    if join_status == _NETSETUP_DOMAIN and join_name:
        name = join_name
        workgroup = False
    elif workgroup:
        name = computer or join_name
    else:
        name = join_name or langroup or computer
    if not name:
        return HostUserDomain()
    return HostUserDomain(
        name=name,
        computer=computer,
        is_workgroup=workgroup,
        source="netapi",
    )


def lookup_host_userdomain(host: str) -> HostUserDomain:
    """
    Descobre o ``$env:USERDOMAIN`` do host remoto.

    Domínio associado → NetBIOS do domínio; workgroup → nome do computador.
    Ordem: NetAPI → nbtstat → FQDN DNS → sufixo no nome digitado.
    """
    h = normalize_host(host)
    if not is_valid_host(h):
        return HostUserDomain()

    hint = domain_hint_from_hostname(h)
    netapi = _netapi_userdomain(h)
    if netapi.name:
        return HostUserDomain(
            name=netapi.name,
            computer=netapi.computer,
            dns=_dns_suffix_of(h) or hint.dns,
            is_workgroup=netapi.is_workgroup,
            source=netapi.source,
        )

    nbt = nbtstat_userdomain(h)
    if nbt.name:
        return HostUserDomain(
            name=nbt.name,
            computer=nbt.computer,
            dns=_dns_suffix_of(h) or hint.dns,
            is_workgroup=nbt.is_workgroup,
            source=nbt.source,
        )

    dns = _dns_suffix_of(h) or hint.dns
    if dns:
        return HostUserDomain(
            name=netbios_from_dns(dns),
            dns=dns,
            is_workgroup=False,
            source="dns" if not hint.name else hint.source,
        )
    return hint
