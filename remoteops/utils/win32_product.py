"""Consulta complementar Win32_Product (WMI), fora do caminho da varredura.

A Instalação em Lote **não** chama isto quando o Remote Registry falha:
host inacessível + powershell.exe era bloqueado pelo EDR (Fortinet).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, List, Optional

from remoteops.core.win_cmd import CREATE_NO_WINDOW
from remoteops.utils.psinfo import HostInventoryStatus, InstalledApp

WIN32_PRODUCT_TIMEOUT_SECONDS = 90.0
MIN_WIN32_PRODUCT_TIMEOUT_SECONDS = 30.0

_SELECT = "Select-Object Name, Version, Vendor, IdentifyingNumber | ConvertTo-Json -Compress"


def apps_from_win32_json(payload: str) -> List[InstalledApp]:
    """Converte o JSON do Win32_Product em InstalledApp (Name/Version/Vendor)."""
    text = (payload or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if data is None:
        return []
    if isinstance(data, dict):
        items: List[Any] = [data]
    elif isinstance(data, list):
        items = data
    else:
        return []
    apps: List[InstalledApp] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        version = str(item.get("Version") or "").strip()
        vendor = str(item.get("Vendor") or "").strip()
        code = str(item.get("IdentifyingNumber") or "").strip()
        display_line = f"{name} {version}".strip() if version else name
        apps.append(
            InstalledApp(
                display_name=name,
                version=version,
                publisher=vendor,
                display_line=display_line,
                product_code=code,
                uninstall_string="",
                quiet_uninstall_string="",
                is_msi=bool(code),
                arch="",
            )
        )
    return apps


def list_remote_win32_products(
    host: str,
    *,
    timeout: Optional[float] = None,
    user: str = "",
    password: str = "",
) -> HostInventoryStatus:
    """Lista produtos MSI no host via Get-WmiObject Win32_Product."""
    h = (host or "").strip().strip("\\")
    if not h:
        return HostInventoryStatus(
            host="",
            ok=False,
            apps=[],
            error_kind="invalid_host",
            message="Host inválido ou vazio.",
            stage="validate",
        )

    try:
        seconds = float(timeout) if timeout else WIN32_PRODUCT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        seconds = WIN32_PRODUCT_TIMEOUT_SECONDS
    seconds = max(MIN_WIN32_PRODUCT_TIMEOUT_SECONDS, seconds)

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$h = $env:RO_W32_HOST; $u = $env:RO_W32_USER; $pw = $env:RO_W32_PASS; "
        "if ($u) { "
        "$sec = ConvertTo-SecureString $pw -AsPlainText -Force; "
        "$cred = New-Object System.Management.Automation.PSCredential ($u, $sec); "
        f"Get-WmiObject -Class Win32_Product -ComputerName $h -Credential $cred | {_SELECT} "
        "} else { "
        f"Get-WmiObject -Class Win32_Product -ComputerName $h | {_SELECT} "
        "}"
    )
    env = os.environ.copy()
    env["RO_W32_HOST"] = h
    env["RO_W32_USER"] = (user or "").strip()
    env["RO_W32_PASS"] = password if (user or "").strip() else ""
    creationflags = CREATE_NO_WINDOW if CREATE_NO_WINDOW else 0
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=seconds,
            creationflags=creationflags,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return HostInventoryStatus(
            host=h,
            ok=False,
            apps=[],
            error_kind="timed_out",
            message=f"Win32_Product excedeu {int(seconds)}s.",
            stage="timeout",
        )
    except OSError as exc:
        return HostInventoryStatus(
            host=h,
            ok=False,
            apps=[],
            error_kind="internal_error",
            message=f"Falha ao iniciar PowerShell: {exc}",
            stage="spawn",
        )
    finally:
        env["RO_W32_PASS"] = ""

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        return HostInventoryStatus(
            host=h,
            ok=True,
            apps=apps_from_win32_json(out),
            error_kind="",
            message="",
            stage="enumerate",
        )
    detail = err or out or f"Win32_Product falhou (exit {proc.returncode})."
    return HostInventoryStatus(
        host=h,
        ok=False,
        apps=[],
        error_kind="remote_registry",
        message=detail[:400],
        stage="enumerate",
    )
