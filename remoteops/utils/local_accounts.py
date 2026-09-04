"""Contas locais — modelos, consulta PowerShell e validação.

Sem Qt. Opera somente sobre contas com ``LocalAccount=True``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional, Sequence

from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.psshutdown import is_multi_host_target, validate_power_host

LOCAL_ACCOUNTS_QUERY_SCRIPT = r"""
$ErrorActionPreference = 'Continue'
$rows = @()
try {
  Import-Module Microsoft.PowerShell.LocalAccounts -ErrorAction SilentlyContinue | Out-Null
} catch {}
try {
  if (Get-Command Get-LocalUser -ErrorAction SilentlyContinue) {
    foreach ($u in @(Get-LocalUser -ErrorAction Stop)) {
      $sid = ''
      try { $sid = [string]$u.SID.Value } catch { $sid = [string]$u.SID }
      $rows += [pscustomobject]@{
        Name = [string]$u.Name
        FullName = [string]$u.FullName
        Domain = [string]$env:COMPUTERNAME
        SID = $sid
        Disabled = -not [bool]$u.Enabled
        Lockout = $false
        PasswordChangeable = [bool]$u.UserMayChangePassword
        PasswordExpires = $null -ne $u.PasswordExpires
        PasswordRequired = [bool]$u.PasswordRequired
        Status = $(if ($u.Enabled) { 'OK' } else { 'Degraded' })
      }
    }
  }
} catch { $rows = @() }
if (@($rows).Count -eq 0) {
  foreach ($u in @(Get-CimInstance Win32_UserAccount -Filter 'LocalAccount=True')) {
    $rows += [pscustomobject]@{
      Name = [string]$u.Name
      FullName = [string]$u.FullName
      Domain = [string]$u.Domain
      SID = [string]$u.SID
      Disabled = [bool]$u.Disabled
      Lockout = [bool]$u.Lockout
      PasswordChangeable = [bool]$u.PasswordChangeable
      PasswordExpires = [bool]$u.PasswordExpires
      PasswordRequired = [bool]$u.PasswordRequired
      Status = [string]$u.Status
    }
  }
}
$json = [string](ConvertTo-Json -InputObject @($rows) -Compress -Depth 4)
if ([string]::IsNullOrWhiteSpace($json)) { $json = '[]' }
elseif (@($rows).Count -le 1 -and $json.TrimStart().StartsWith('{')) {
  $json = '[' + $json + ']'
}
if (Get-Command Write-RemoteOpsJson -ErrorAction SilentlyContinue) {
  Write-RemoteOpsJson $json
} else {
  $json
}
""".strip()

_SID_RID_RE = re.compile(r"-(\d+)$")
_ACCOUNT_INVALID_CHARS = frozenset("\r\n\x00")
_OPTION_PREFIX_RE = re.compile(r"^[-/@]")


class BuiltinAccountKind(str, Enum):
    NONE = "none"
    ADMINISTRATOR = "administrator"  # RID 500
    GUEST = "guest"  # RID 501


@dataclass(frozen=True)
class LocalAccount:
    host: str
    name: str
    full_name: str = ""
    domain: str = ""
    sid: str = ""
    rid: Optional[int] = None
    disabled: bool = False
    locked: bool = False
    password_changeable: Optional[bool] = None
    password_expires: Optional[bool] = None
    password_required: Optional[bool] = None
    status: str = ""
    builtin_kind: BuiltinAccountKind = BuiltinAccountKind.NONE

    def __repr__(self) -> str:
        return (
            f"LocalAccount(host={self.host!r}, name={self.name!r}, "
            f"sid={self.sid!r}, rid={self.rid!r}, builtin={self.builtin_kind.value})"
        )

    @property
    def type_label(self) -> str:
        if self.builtin_kind == BuiltinAccountKind.ADMINISTRATOR:
            return "Administrador interno (RID 500)"
        if self.builtin_kind == BuiltinAccountKind.GUEST:
            return "Convidado interno (RID 501)"
        return "Conta local"

    @property
    def state_label(self) -> str:
        parts: List[str] = []
        if self.disabled:
            parts.append("Desabilitada")
        else:
            parts.append("Habilitada")
        if self.locked:
            parts.append("bloqueada")
        return ", ".join(parts) if parts else "—"

    @property
    def password_change_allowed(self) -> bool:
        if self.password_changeable is False:
            return False
        return True

    @property
    def logical_key(self) -> str:
        if self.rid is not None:
            return f"rid:{self.rid}"
        return f"name:{normalize_account_name(self.name)}"


def extract_rid_from_sid(sid: str) -> Optional[int]:
    raw = (sid or "").strip()
    if not raw:
        return None
    match = _SID_RID_RE.search(raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def classify_builtin_kind(rid: Optional[int]) -> BuiltinAccountKind:
    if rid == 500:
        return BuiltinAccountKind.ADMINISTRATOR
    if rid == 501:
        return BuiltinAccountKind.GUEST
    return BuiltinAccountKind.NONE


def normalize_account_name(name: str) -> str:
    return (name or "").strip().casefold()


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "sim"):
        return True
    if text in ("false", "0", "no", "nao", "não"):
        return False
    return None


def _coerce_sid(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("Value") or value.get("value") or "").strip()
    return str(value).strip()


def _normalize_json_list(data: Any) -> List[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if data and all(str(key).isdigit() for key in data.keys()):
            return [item for item in data.values() if isinstance(item, dict)]
        nested = data.get("value")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [data]
    return []


def parse_local_accounts_payload(
    data: Any,
    *,
    host: str,
) -> tuple[List[LocalAccount], str]:
    """Converte JSON do PowerShell em contas locais associadas ao host."""
    host_norm = normalize_host(host)
    if not host_norm:
        return [], "Host inválido."

    items = _normalize_json_list(data)
    accounts: List[LocalAccount] = []
    for item in items:
        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        domain = str(item.get("Domain") or "").strip()
        sid = _coerce_sid(item.get("SID"))
        rid = extract_rid_from_sid(sid)
        builtin = classify_builtin_kind(rid)
        if domain and not _domain_matches_host(domain, host_norm):
            # Defesa: não incluir contas de domínio na listagem.
            continue
        if "\\" in name or "/" in name:
            continue
        accounts.append(
            LocalAccount(
                host=host_norm,
                name=name,
                full_name=str(item.get("FullName") or "").strip(),
                domain=domain,
                sid=sid,
                rid=rid,
                disabled=bool(_coerce_bool(item.get("Disabled"))),
                locked=bool(_coerce_bool(item.get("Lockout"))),
                password_changeable=_coerce_bool(item.get("PasswordChangeable")),
                password_expires=_coerce_bool(item.get("PasswordExpires")),
                password_required=_coerce_bool(item.get("PasswordRequired")),
                status=str(item.get("Status") or "").strip(),
                builtin_kind=builtin,
            )
        )
    accounts.sort(key=lambda a: (a.name or "").casefold())
    return accounts, ""


def _domain_matches_host(domain: str, host: str) -> bool:
    d = (domain or "").strip().strip("\\").casefold()
    h = normalize_host(host).casefold()
    if not d or d in (".", h):
        return True
    if d == h:
        return True
    # Nome NetBIOS do computador costuma coincidir com o host consultado.
    return d == h.split(".")[0]


def validate_local_account_host(host: str) -> List[str]:
    """Mesmas regras do PsShutdown: host único obrigatório."""
    return list(validate_power_host(host))


def validate_account_name(name: str) -> List[str]:
    errors: List[str] = []
    raw = name or ""
    if not raw.strip():
        errors.append("Selecione uma conta local obtida na consulta.")
        return errors
    if any(ch in raw for ch in _ACCOUNT_INVALID_CHARS):
        errors.append("Nome de conta inválido.")
    if "\\" in raw or "/" in raw:
        errors.append("Contas de domínio não podem ser alteradas por este módulo.")
    if _OPTION_PREFIX_RE.match(raw.strip()):
        errors.append("Nome de conta inválido.")
    return errors


def account_belongs_to_host(account: LocalAccount, host: str) -> bool:
    wanted = normalize_host(host)
    if not wanted or not account:
        return False
    return normalize_host(account.host).casefold() == wanted.casefold()


def is_listed_local_account(
    account: LocalAccount,
    listed: Sequence[LocalAccount],
) -> bool:
    if account is None:
        return False
    host_key = normalize_host(account.host).casefold()
    name_key = normalize_account_name(account.name)
    sid_key = (account.sid or "").strip().casefold()
    for item in listed:
        if normalize_host(item.host).casefold() != host_key:
            continue
        if sid_key and (item.sid or "").strip().casefold() == sid_key:
            return True
        if normalize_account_name(item.name) == name_key:
            return True
    return False


def accounts_compatible_for_batch(accounts: Sequence[LocalAccount]) -> tuple[bool, str]:
    if not accounts:
        return False, "Nenhuma conta selecionada."
    keys = {a.logical_key for a in accounts}
    if len(keys) != 1:
        return (
            False,
            "Selecione a mesma conta local nos computadores desejados. "
            "Para alterar outra conta, execute uma nova operação.",
        )
    return True, ""


def validate_new_password(password: str, confirmation: str) -> List[str]:
    errors: List[str] = []
    if password is None or password == "":
        errors.append("Informe a nova senha da conta local.")
    if confirmation is None or confirmation == "":
        errors.append("Confirme a nova senha.")
    if password != confirmation:
        errors.append("A confirmação deve ser idêntica à nova senha.")
    return errors


def validate_admin_credential(user: str, password: str) -> List[str]:
    user = (user or "").strip()
    secret = password or ""
    if user and not secret:
        return [
            "Informe a senha administrativa ou limpe o usuário para usar o contexto Windows atual."
        ]
    return []


def is_probably_local_session_user(
    username: str,
    domain: str,
    *,
    computer_name: str = "",
) -> bool:
    """Heurística para Sessões/Inventário — não substitui a consulta local."""
    user = (username or "").strip()
    if not user:
        return False
    dom = (domain or "").strip().strip("\\")
    if "\\" in user:
        parts = user.split("\\", 1)
        dom = parts[0]
        user = parts[1]
    if not dom:
        return True
    comp = (computer_name or "").strip().strip("\\").casefold()
    dom_cf = dom.casefold()
    if dom_cf in (".", comp, comp.split(".")[0] if comp else ""):
        return True
    return False


def host_is_valid_for_local_ops(host: str) -> bool:
    if is_multi_host_target(host):
        return False
    h = normalize_host(host)
    if not h or h.startswith("-") or not is_valid_host(h):
        return False
    return not bool(validate_local_account_host(host))
