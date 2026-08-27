"""Leitura remota do Registro de impressoras (winreg.ConnectRegistry).

Destinado ao processo filho isolado. Não importa Qt, não recebe senha e não
usa PsExec. Apenas dicionários/listas/primitivos na saída.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from remoteops.utils.installed_printers import (
    MSG_AMBIGUOUS_SID,
    MSG_REGISTRY_DENIED,
    MSG_REMOTE_REGISTRY,
    is_user_sid_key,
    parse_connection_key,
    parse_device_value,
    resolve_user_sid,
    split_account,
)
from remoteops.utils.ping import is_valid_host, normalize_host

_AUTH_WINERRORS = frozenset({5, 86, 1326, 1327, 1330, 1789, 2202})
_UNREACHABLE_WINERRORS = frozenset({51, 53, 64, 67, 1231, 10051, 10060, 10061, 10065})
_REMOTE_REGISTRY_WINERRORS = frozenset({1707, 1722, 1753})

_HKU_CONNECTIONS = r"{sid}\Printers\Connections"
_HKU_WINDOWS = r"{sid}\Software\Microsoft\Windows NT\CurrentVersion\Windows"
_HKU_VOLATILE = r"{sid}\Volatile Environment"
_HKLM_CONNECTIONS = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Print\Connections"
_HKLM_PRINTERS = r"SYSTEM\CurrentControlSet\Control\Print\Printers"


def _winerror_code(exc: BaseException) -> Optional[int]:
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return winerror
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        return errno
    return None


def classify_registry_connect_error(exc: OSError) -> Tuple[str, str]:
    code = _winerror_code(exc)
    if code in _AUTH_WINERRORS:
        return "auth", MSG_REGISTRY_DENIED
    if code in _UNREACHABLE_WINERRORS:
        return "unreachable", MSG_REMOTE_REGISTRY
    if code in _REMOTE_REGISTRY_WINERRORS:
        return "remote_registry", MSG_REMOTE_REGISTRY
    if code == 5:
        return "auth", MSG_REGISTRY_DENIED
    return "remote_registry", MSG_REMOTE_REGISTRY


def _reg_str(key, value_name: str) -> str:
    import winreg

    try:
        value, _typ = winreg.QueryValueEx(key, value_name)
        return str(value or "").strip()
    except OSError:
        return ""


def _enum_subkeys(key) -> List[str]:
    import winreg

    names: List[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
    return names


def _open_key(root, path: str, access: int):
    import winreg

    try:
        return winreg.OpenKey(root, path, 0, access)
    except OSError:
        return None


def _read_volatile_env(hku, sid: str, access: int) -> Dict[str, str]:
    key = _open_key(hku, _HKU_VOLATILE.format(sid=sid), access)
    if key is None:
        return {"sid": sid}
    try:
        return {
            "sid": sid,
            "USERNAME": _reg_str(key, "USERNAME"),
            "USERDOMAIN": _reg_str(key, "USERDOMAIN"),
            "USERDNSDOMAIN": _reg_str(key, "USERDNSDOMAIN"),
            "SESSIONNAME": _reg_str(key, "SESSIONNAME"),
        }
    finally:
        try:
            key.Close()
        except OSError:
            pass


def _enum_connection_leaves(root, path: str, access: int) -> List[dict]:
    key = _open_key(root, path, access)
    if key is None:
        return []
    try:
        rows: List[dict] = []
        for leaf in _enum_subkeys(key):
            parsed = parse_connection_key(leaf)
            if parsed.get("unc"):
                rows.append(parsed)
        return rows
    finally:
        try:
            key.Close()
        except OSError:
            pass


def _read_default_device(hku, sid: str, access: int) -> str:
    key = _open_key(hku, _HKU_WINDOWS.format(sid=sid), access)
    if key is None:
        return ""
    try:
        return parse_device_value(_reg_str(key, "Device"))
    finally:
        try:
            key.Close()
        except OSError:
            pass


def _read_local_printers(hklm, access: int) -> List[dict]:
    key = _open_key(hklm, _HKLM_PRINTERS, access)
    if key is None:
        return []
    try:
        rows: List[dict] = []
        for name in _enum_subkeys(key):
            sub = _open_key(key, name, access)
            if sub is None:
                continue
            try:
                share = _reg_str(sub, "Share Name")
                rows.append(
                    {
                        "Name": _reg_str(sub, "Name") or name,
                        "ShareName": share,
                        "DriverName": _reg_str(sub, "Printer Driver"),
                        "PortName": _reg_str(sub, "Port"),
                        "Location": _reg_str(sub, "Location"),
                        "Comment": _reg_str(sub, "Description"),
                        "Published": False,
                        "Type": "Local",
                        "Default": False,
                    }
                )
            finally:
                try:
                    sub.Close()
                except OSError:
                    pass
        return rows
    finally:
        try:
            key.Close()
        except OSError:
            pass


def collect_remote_printers_registry(
    host: str,
    *,
    username: str = "",
    domain: str = "",
    session_name: str = "",
    include_local_fallback: bool = False,
) -> dict:
    """Consulta HKU/HKLM no host remoto. Retorno só com tipos JSON-safe."""
    import winreg

    name = normalize_host(host)
    payload = {
        "ok": False,
        "host": name,
        "error": "",
        "error_kind": "",
        "winerror": None,
        "user_sid": "",
        "default_name": "",
        "user_connections": [],
        "computer_connections": [],
        "local_printers": [],
        "hku_entries": [],
        "warnings": [],
    }
    if not name or not is_valid_host(name):
        payload["error"] = "Host inválido."
        payload["error_kind"] = "invalid_host"
        return payload

    access = winreg.KEY_READ
    hku = None
    hklm = None
    try:
        try:
            hku = winreg.ConnectRegistry(rf"\\{name}", winreg.HKEY_USERS)
        except OSError as exc:
            kind, msg = classify_registry_connect_error(exc)
            payload["error_kind"] = kind
            payload["error"] = msg
            payload["winerror"] = _winerror_code(exc)
            return payload
        try:
            hklm = winreg.ConnectRegistry(rf"\\{name}", winreg.HKEY_LOCAL_MACHINE)
        except OSError as exc:
            kind, msg = classify_registry_connect_error(exc)
            payload["error_kind"] = kind
            payload["error"] = msg
            payload["winerror"] = _winerror_code(exc)
            return payload

        sid_names = [n for n in _enum_subkeys(hku) if is_user_sid_key(n)]
        entries = [_read_volatile_env(hku, sid, access) for sid in sid_names]
        payload["hku_entries"] = entries

        embedded_domain, short = split_account(username)
        login = short or (username or "").strip()
        used_domain = embedded_domain or (domain or "").strip()
        sid, sid_error = resolve_user_sid(
            entries,
            username=login,
            domain=used_domain,
            session_name=session_name,
        )
        if sid:
            payload["user_sid"] = sid
            payload["default_name"] = _read_default_device(hku, sid, access)
            payload["user_connections"] = _enum_connection_leaves(
                hku, _HKU_CONNECTIONS.format(sid=sid), access
            )
        elif sid_error:
            payload["warnings"].append(sid_error)

        payload["computer_connections"] = _enum_connection_leaves(
            hklm, _HKLM_CONNECTIONS, access
        )
        if include_local_fallback:
            payload["local_printers"] = _read_local_printers(hklm, access)

        payload["ok"] = True
        if sid_error and sid_error == MSG_AMBIGUOUS_SID:
            payload["error_kind"] = "ambiguous_sid"
            payload["error"] = sid_error
        return payload
    except OSError as exc:
        kind, msg = classify_registry_connect_error(exc)
        payload["error_kind"] = kind
        payload["error"] = msg
        payload["winerror"] = _winerror_code(exc)
        return payload
    except Exception as exc:  # noqa: BLE001 — borda do processo filho
        payload["error_kind"] = "internal_error"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    finally:
        for hive in (hku, hklm):
            if hive is None:
                continue
            try:
                hive.Close()
            except OSError:
                pass
