"""Funções puras da consulta de impressoras instaladas no host remoto.

Sem I/O de rede, sem Qt e sem PsExec. Usado pelo serviço, pelo Registro Remoto
e pelos testes unitários.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.printers import (
    NetworkPrinter,
    active_sessions,
    encode_powershell,
    is_session_active,
    powershell_encoded_argv,
    ps_single_quote,
)
from remoteops.utils.sessions import RemoteSession

TYPE_LOCAL = "Local"
TYPE_USB = "USB"
TYPE_TCPIP = "TCP/IP"
TYPE_VIRTUAL = "Virtual"
TYPE_USER_NET = "Rede — usuário"
TYPE_COMPUTER_NET = "Rede — computador"

SOURCE_SPOOLER = "spooler"
SOURCE_USER_REG = "user_reg"
SOURCE_COMPUTER_REG = "computer_reg"
SOURCE_LOCAL_REG = "local_reg"

MSG_NO_ACTIVE_SESSION = (
    "Nenhum usuário com sessão ativa foi encontrado no host remoto."
)
MSG_MULTI_SESSION = "Há mais de uma sessão ativa. Selecione o usuário desejado."
MSG_IDENTIFYING = "Identificando o usuário da sessão ativa…"
MSG_OFFLINE = "Host remoto precisa estar Online."
MSG_INVALID_HOST = "Host inválido."
MSG_CANCELLED = "A consulta foi cancelada."
MSG_TIMEOUT = "A consulta excedeu o tempo limite."
MSG_WTS_FAILED = "Não foi possível consultar as sessões pela API WTS."
MSG_REMOTE_REGISTRY = "O serviço de Registro Remoto não está disponível."
MSG_REGISTRY_DENIED = "Acesso negado ao Registro Remoto."
MSG_SPOOLER = "O spooler remoto não respondeu."
MSG_GET_PRINTER = "Get-Printer não conseguiu consultar o computador remoto."
MSG_PARTIAL_USER = (
    "Foram encontradas apenas as impressoras do computador; "
    "as conexões do usuário não puderam ser consultadas."
)
MSG_AMBIGUOUS_SID = (
    "Há mais de um perfil de usuário compatível carregado no Registro."
)

_TYPE_RANK = {
    TYPE_USER_NET: 50,
    TYPE_COMPUTER_NET: 40,
    TYPE_USB: 35,
    TYPE_TCPIP: 30,
    TYPE_VIRTUAL: 20,
    TYPE_LOCAL: 10,
    "": 0,
}

_USER_SID_RE = re.compile(r"^S-1-5-21-\d+-\d+-\d+-\d+$", re.IGNORECASE)
_UNC_RE = re.compile(
    r"^\\\\+([^\\/]+)[/\\]+([^\\/]+)$",
    re.IGNORECASE,
)
_FRIENDLY_RE = re.compile(
    r"^(.+?)\s+(?:em|on)\s+([^\\/]+)$",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_SAFE_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)$"
)
_USB_HINTS = ("usb", "dot4", "usbprint")
_TCP_HINTS = ("wsd", "ip_", "tcp", "standard tcp", "ipp", "lpr")
_VIRTUAL_HINTS = (
    "pdf",
    "xps",
    "onenote",
    "fax",
    "portprompt",
    "nul:",
    "microsoft print to pdf",
    "microsoft xps",
    "send to onenote",
)
_LOCAL_PORT_HINTS = ("lpt", "com", "file:", "fileport")


@dataclass
class InstalledPrintersPayload:
    """Estrutura serializável da consulta de impressoras instaladas."""

    ok: bool = False
    partial: bool = False
    host: str = ""
    username: str = ""
    session_id: Optional[int] = None
    printers: List[NetworkPrinter] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""
    generation: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "partial": bool(self.partial),
            "host": self.host or "",
            "username": self.username or "",
            "session_id": self.session_id,
            "printers": [printer_to_mapping(p) for p in self.printers],
            "warnings": list(self.warnings or []),
            "error": self.error or "",
        }


def printer_to_mapping(printer: NetworkPrinter) -> dict:
    return {
        "Name": printer.name or "",
        "ShareName": printer.share_name or "",
        "DriverName": printer.driver_name or "",
        "PortName": printer.port_name or "",
        "Location": printer.location or "",
        "Comment": printer.comment or "",
        "Published": bool(printer.published),
        "Type": printer.printer_type or "",
        "Default": bool(printer.is_default),
    }


def payload_from_mapping(data: dict) -> InstalledPrintersPayload:
    raw = data if isinstance(data, dict) else {}
    printers: List[NetworkPrinter] = []
    for item in raw.get("printers") or []:
        if isinstance(item, NetworkPrinter):
            printers.append(item)
        elif isinstance(item, dict):
            printers.append(printer_from_mapping(item))
    session_id = raw.get("session_id")
    if session_id is not None:
        try:
            session_id = int(session_id)
        except (TypeError, ValueError):
            session_id = None
    return InstalledPrintersPayload(
        ok=bool(raw.get("ok")),
        partial=bool(raw.get("partial")),
        host=str(raw.get("host") or ""),
        username=str(raw.get("username") or ""),
        session_id=session_id,
        printers=printers,
        warnings=[str(w) for w in (raw.get("warnings") or []) if str(w).strip()],
        error=str(raw.get("error") or ""),
        generation=int(raw.get("generation") or 0),
    )


def printer_from_mapping(item: dict) -> NetworkPrinter:
    type_val = item.get("Type") or item.get("PrinterType") or item.get("printer_type") or ""
    if isinstance(type_val, dict):
        type_val = type_val.get("value") or type_val.get("Value") or ""
    return NetworkPrinter(
        name=str(item.get("Name") or item.get("name") or "").strip(),
        share_name=str(item.get("ShareName") or item.get("share_name") or "").strip(),
        driver_name=str(item.get("DriverName") or item.get("driver_name") or "").strip(),
        port_name=str(item.get("PortName") or item.get("port_name") or "").strip(),
        location=str(item.get("Location") or item.get("location") or "").strip(),
        comment=str(item.get("Comment") or item.get("comment") or "").strip(),
        published=_as_bool(item.get("Published") or item.get("published")),
        printer_type=str(type_val or "").strip(),
        is_default=_as_bool(item.get("Default") or item.get("IsDefault") or item.get("is_default")),
    )


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().casefold()
    return text in {"1", "true", "yes", "sim"}


def assert_safe_computer_name(host: str) -> str:
    """Valida o hostname antes de interpolar em PowerShell."""
    name = normalize_host(host)
    if not name or not is_valid_host(name):
        raise ValueError(MSG_INVALID_HOST)
    if any(ch in name for ch in ";`$(){}[]!#,&\n\r\t\\/"):
        raise ValueError(MSG_INVALID_HOST)
    if _IPV4_RE.fullmatch(name):
        parts = name.split(".")
        if all(0 <= int(p) <= 255 for p in parts):
            return name
        raise ValueError(MSG_INVALID_HOST)
    if not _SAFE_HOST_RE.fullmatch(name):
        raise ValueError(MSG_INVALID_HOST)
    return name


def build_list_computer_printers_script(host: str, output_path: str) -> str:
    """Lista filas do computador remoto via ``Get-Printer -ComputerName`` (PowerShell local)."""
    name = assert_safe_computer_name(host)
    path_raw = (output_path or "").strip()
    if not path_raw:
        raise ValueError("Caminho do JSON temporário vazio.")
    srv = ps_single_quote(name)
    path = ps_single_quote(path_raw)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$WarningPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        f"$out = {path}\n"
        "function Emit-Printers($items) {\n"
        "  $rows = @($items)\n"
        "  if ($rows.Count -eq 0) { $json = '[]' }\n"
        "  elseif ($rows.Count -eq 1) {\n"
        "    $json = '[' + ($rows[0] | ConvertTo-Json -Compress -Depth 4) + ']'\n"
        "  } else {\n"
        "    $json = ($rows | ConvertTo-Json -Compress -Depth 4)\n"
        "  }\n"
        "  [System.IO.File]::WriteAllText($out, [string]$json,"
        " [System.Text.UTF8Encoding]::new($false))\n"
        "  Write-Output ('{\"ok\":true,\"count\":' + $rows.Count + '}')\n"
        "}\n"
        "try {\n"
        "  Import-Module PrintManagement -ErrorAction SilentlyContinue | Out-Null\n"
        "  if (-not (Get-Command Get-Printer -ErrorAction SilentlyContinue)) {\n"
        "    throw 'O cmdlet Get-Printer não está disponível (módulo PrintManagement).'\n"
        "  }\n"
        f"  $rows = @(Get-Printer -ComputerName {srv} |\n"
        "    Select-Object Name,ShareName,DriverName,PortName,Location,"
        "Comment,Published,Type,Default)\n"
        "  Emit-Printers $rows\n"
        "  exit 0\n"
        "} catch {\n"
        "  Write-Error $_.Exception.Message\n"
        "  exit 1\n"
        "}\n"
    )


def local_list_computer_printers_argv(host: str, output_path: str) -> List[str]:
    """Argv do PowerShell local — consulta o spooler remoto sem PsExec."""
    script = build_list_computer_printers_script(host, output_path)
    return powershell_encoded_argv(
        encode_powershell(script),
        execution_policy="Bypass",
    )


def parse_connection_key(leaf: str) -> dict:
    """Interpreta ``,,PRINT01,Financeiro`` → servidor, compartilhamento e UNC."""
    raw = (leaf or "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        return {"leaf": raw, "server": "", "share": "", "unc": ""}
    server, share = parts[0], parts[1]
    return {
        "leaf": raw,
        "server": server,
        "share": share,
        "unc": normalize_unc(f"\\\\{server}\\{share}"),
    }


def normalize_unc(value: str) -> str:
    """Normaliza ``\\\\servidor\\compartilhamento`` (case preservado no valor)."""
    text = (value or "").strip().replace("/", "\\")
    if not text:
        return ""
    match = _UNC_RE.match(text)
    if match:
        return f"\\\\{match.group(1)}\\{match.group(2)}"
    collapsed = re.sub(r"\\+", r"\\", text)
    if collapsed.startswith("\\"):
        collapsed = "\\" + collapsed
    match = _UNC_RE.match(collapsed)
    if match:
        return f"\\\\{match.group(1)}\\{match.group(2)}"
    return ""


def unc_from_friendly_name(name: str) -> str:
    """``Financeiro em PRINT01`` / ``Financeiro on PRINT01`` → UNC."""
    text = (name or "").strip()
    if not text:
        return ""
    match = _FRIENDLY_RE.match(text)
    if not match:
        return ""
    share = match.group(1).strip().strip("\\")
    server = match.group(2).strip().strip("\\")
    if not share or not server:
        return ""
    if share.casefold() in {"printer", "impressora"}:
        return ""
    return normalize_unc(f"\\\\{server}\\{share}")


def parse_device_value(device: str) -> str:
    """Primeira parte de ``Device`` (impressora padrão do usuário)."""
    text = (device or "").strip()
    if not text:
        return ""
    return text.split(",", 1)[0].strip()


def split_account(value: str) -> Tuple[str, str]:
    """Devolve ``(domínio, usuário)`` a partir de ``DOMÍNIO\\user`` ou ``user@dns``."""
    text = (value or "").strip()
    if not text:
        return "", ""
    if "\\" in text:
        domain, user = text.split("\\", 1)
        return domain.strip(), user.strip()
    if "@" in text:
        user, domain = text.split("@", 1)
        return domain.strip(), user.strip()
    return "", text


def session_account(session: Optional[RemoteSession]) -> str:
    """``DOMÍNIO\\usuario`` a partir da sessão WTS — sem dica da conta administrativa."""
    if session is None:
        return ""
    user = (session.username or "").strip()
    domain = (getattr(session, "domain", "") or "").strip()
    embedded_domain, short = split_account(user)
    login = short or user
    used_domain = embedded_domain or domain
    if not login:
        return ""
    if used_domain:
        return f"{used_domain}\\{login}"
    return login


def is_user_sid_key(name: str) -> bool:
    text = (name or "").strip()
    if text.casefold().endswith("_classes"):
        return False
    return bool(_USER_SID_RE.fullmatch(text))


def _norm(value: str) -> str:
    return (value or "").strip().casefold()


def _domain_matches(want: str, env: dict) -> bool:
    target = _norm(want)
    if not target:
        return True
    userdomain = _norm(env.get("USERDOMAIN") or "")
    dns = _norm(env.get("USERDNSDOMAIN") or "")
    if target == userdomain:
        return True
    if dns and (target == dns or dns.startswith(target + ".")):
        return True
    if dns and target == dns.split(".", 1)[0]:
        return True
    return False


def _session_name_matches(want: str, env: dict) -> bool:
    target = _norm(want)
    if not target:
        return True
    current = _norm(env.get("SESSIONNAME") or "")
    if not current:
        return True
    return target == current


def resolve_user_sid(
    hku_entries: Sequence[dict],
    *,
    username: str,
    domain: str = "",
    session_name: str = "",
) -> Tuple[str, str]:
    """Resolve o SID do usuário ativo a partir de snapshots de ``HKEY_USERS``.

    Cada entrada: ``{"sid": "S-1-5-21-...", "USERNAME": "...", "USERDOMAIN": "...",
    "USERDNSDOMAIN": "...", "SESSIONNAME": "..."}``.

    Retorna ``(sid, erro)``. Erro de ambiguidade quando mais de um SID bate.
    """
    embedded_domain, short = split_account(username)
    login = short or (username or "").strip()
    used_domain = (embedded_domain or domain or "").strip()
    if not login:
        return "", "Usuário da sessão ativa não informado."

    matches: List[str] = []
    for entry in hku_entries or ():
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("sid") or "").strip()
        if not is_user_sid_key(sid):
            continue
        env_user = str(entry.get("USERNAME") or "").strip()
        if _norm(env_user) != _norm(login):
            continue
        if not _domain_matches(used_domain, entry):
            continue
        if not _session_name_matches(session_name, entry):
            continue
        matches.append(sid)

    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0], ""
    if len(unique) > 1:
        return "", MSG_AMBIGUOUS_SID
    return "", "Não foi possível localizar o perfil do usuário ativo no Registro."


def printer_is_default(printer: NetworkPrinter, default_name: str) -> bool:
    """True se o valor ``Device`` (ou o próprio objeto) identificar a padrão."""
    if printer.is_default:
        return True
    target = (default_name or "").strip()
    if not target:
        return False
    want = _norm(target)
    want_unc = _norm(normalize_unc(target) or unc_from_friendly_name(target) or target)
    candidates = [
        printer.name,
        printer.share_name,
        printer.port_name,
        normalize_unc(printer.name),
        normalize_unc(printer.port_name),
        unc_from_friendly_name(printer.name),
    ]
    for item in candidates:
        text = (item or "").strip()
        if not text:
            continue
        if _norm(text) == want:
            return True
        unc = _norm(normalize_unc(text) or unc_from_friendly_name(text) or "")
        if want_unc and unc and unc == want_unc:
            return True
    return False


def classify_printer_type(
    *,
    source: str = "",
    spooler_type: str = "",
    port_name: str = "",
    name: str = "",
) -> str:
    """Classifica a origem da impressora. Não inventa dados ausentes."""
    if source == SOURCE_USER_REG:
        return TYPE_USER_NET
    if source == SOURCE_COMPUTER_REG:
        return TYPE_COMPUTER_NET

    port = (port_name or "").strip()
    label = (name or "").strip()
    combined = f"{port} {label} {spooler_type or ''}".casefold()
    unc = normalize_unc(label) or normalize_unc(port)
    spool = str(spooler_type or "").strip()
    spool_cf = spool.casefold()

    if any(h in combined for h in _USB_HINTS):
        return TYPE_USB
    if any(h in combined for h in _TCP_HINTS) or _looks_like_ip_port(port):
        return TYPE_TCPIP
    if any(h in combined for h in _VIRTUAL_HINTS):
        return TYPE_VIRTUAL
    if unc or spool_cf in {"1", "connection", "connections"}:
        if source == SOURCE_SPOOLER:
            return TYPE_COMPUTER_NET
        return TYPE_COMPUTER_NET
    if any(port.casefold().startswith(h) for h in _LOCAL_PORT_HINTS):
        return TYPE_LOCAL
    if spool_cf in {"0", "local", "2", "contact"}:
        return TYPE_LOCAL
    if source == SOURCE_LOCAL_REG:
        return TYPE_LOCAL
    return TYPE_LOCAL if (port or label) else ""


def _looks_like_ip_port(port: str) -> bool:
    text = (port or "").strip()
    if not text:
        return False
    body = text.split(":", 1)[0]
    if body.upper().startswith("IP_"):
        body = body[3:]
    return bool(_IPV4_RE.fullmatch(body))


def preferred_printer_name(first: str, second: str) -> str:
    """Preserva o nome mais informativo (UNC > amigável > curto)."""
    a = (first or "").strip()
    b = (second or "").strip()
    if not a:
        return b
    if not b:
        return a
    score_a = _name_rank(a)
    score_b = _name_rank(b)
    if score_a != score_b:
        return a if score_a > score_b else b
    return a if len(a) >= len(b) else b


def _name_rank(name: str) -> int:
    text = (name or "").strip()
    if normalize_unc(text):
        return 40
    if unc_from_friendly_name(text):
        return 20
    if "\\" in text:
        return 15
    return 5


def identity_key(printer: NetworkPrinter) -> str:
    """Chave canônica case-insensitive para deduplicação."""
    unc = (
        normalize_unc(printer.name)
        or normalize_unc(printer.port_name)
        or unc_from_friendly_name(printer.name)
    )
    if unc:
        return "unc:" + unc.casefold()
    share = (printer.share_name or "").strip()
    if share:
        server = ""
        port_unc = normalize_unc(printer.port_name)
        if port_unc:
            server = port_unc.strip("\\").split("\\", 1)[0]
        if server:
            return "unc:" + f"\\\\{server}\\{share}".casefold()
        return "share:" + share.casefold()
    name = (printer.name or "").strip()
    port = (printer.port_name or "").strip()
    if name and port:
        return "nameport:" + name.casefold() + "|" + port.casefold()
    if name:
        return "name:" + name.casefold()
    if port:
        return "port:" + port.casefold()
    return "empty"


def merge_printer_pair(current: NetworkPrinter, incoming: NetworkPrinter) -> NetworkPrinter:
    """Mescla duplicatas: preenche vazios, preserva Default e o tipo mais específico."""

    def pick(old: str, new: str) -> str:
        return (old or "").strip() or (new or "").strip()

    type_name = current.printer_type
    incoming_type = incoming.printer_type
    if _TYPE_RANK.get(incoming_type, 0) > _TYPE_RANK.get(type_name, 0):
        type_name = incoming_type
    return NetworkPrinter(
        name=preferred_printer_name(current.name, incoming.name),
        share_name=pick(current.share_name, incoming.share_name),
        driver_name=pick(current.driver_name, incoming.driver_name),
        port_name=pick(current.port_name, incoming.port_name),
        location=pick(current.location, incoming.location),
        comment=pick(current.comment, incoming.comment),
        published=bool(current.published or incoming.published),
        printer_type=type_name,
        is_default=bool(current.is_default or incoming.is_default),
    )


def connection_to_printer(
    parsed: dict,
    *,
    source: str,
    default_name: str = "",
) -> Optional[NetworkPrinter]:
    unc = str((parsed or {}).get("unc") or "").strip()
    share = str((parsed or {}).get("share") or "").strip()
    if not unc:
        return None
    printer = NetworkPrinter(
        name=unc,
        share_name=share,
        driver_name="",
        port_name=unc,
        location="",
        comment="",
        published=False,
        printer_type=classify_printer_type(source=source, port_name=unc, name=unc),
        is_default=False,
    )
    if printer_is_default(printer, default_name):
        printer = NetworkPrinter(
            name=printer.name,
            share_name=printer.share_name,
            driver_name=printer.driver_name,
            port_name=printer.port_name,
            location=printer.location,
            comment=printer.comment,
            published=printer.published,
            printer_type=printer.printer_type,
            is_default=True,
        )
    return printer


def printers_from_connection_leaves(
    leaves: Iterable[str],
    *,
    source: str,
    default_name: str = "",
) -> List[NetworkPrinter]:
    out: List[NetworkPrinter] = []
    for leaf in leaves or ():
        parsed = parse_connection_key(str(leaf or ""))
        printer = connection_to_printer(parsed, source=source, default_name=default_name)
        if printer is not None:
            out.append(printer)
    return out


def apply_default_flag(
    printers: Sequence[NetworkPrinter],
    default_name: str,
) -> List[NetworkPrinter]:
    """Marca a impressora padrão pelo valor ``Device`` do usuário."""
    out: List[NetworkPrinter] = []
    for printer in printers:
        if printer_is_default(printer, default_name):
            if printer.is_default:
                out.append(printer)
                continue
            out.append(
                NetworkPrinter(
                    name=printer.name,
                    share_name=printer.share_name,
                    driver_name=printer.driver_name,
                    port_name=printer.port_name,
                    location=printer.location,
                    comment=printer.comment,
                    published=printer.published,
                    printer_type=printer.printer_type,
                    is_default=True,
                )
            )
        else:
            out.append(printer)
    return out


def classify_existing_printer(printer: NetworkPrinter, source: str) -> NetworkPrinter:
    """Reclassifica um objeto já parseado sem perder os demais campos."""
    return NetworkPrinter(
        name=printer.name,
        share_name=printer.share_name,
        driver_name=printer.driver_name,
        port_name=printer.port_name,
        location=printer.location,
        comment=printer.comment,
        published=printer.published,
        printer_type=classify_printer_type(
            source=source,
            spooler_type=printer.printer_type,
            port_name=printer.port_name,
            name=printer.name,
        ),
        is_default=printer.is_default,
    )


def assemble_installed_printers(
    *,
    spooler: Sequence[NetworkPrinter] = (),
    user_connections: Sequence = (),
    computer_connections: Sequence = (),
    local_fallback: Sequence = (),
    default_name: str = "",
) -> List[NetworkPrinter]:
    """Une spooler, conexões /in, /ga e fallback local, com deduplicação."""
    spooler_typed = [classify_existing_printer(p, SOURCE_SPOOLER) for p in spooler or ()]
    user_ps = _printers_from_connection_rows(user_connections, SOURCE_USER_REG, default_name)
    computer_ps = _printers_from_connection_rows(
        computer_connections, SOURCE_COMPUTER_REG, default_name
    )
    local_ps: List[NetworkPrinter] = []
    for item in local_fallback or ():
        if isinstance(item, NetworkPrinter):
            local_ps.append(classify_existing_printer(item, SOURCE_LOCAL_REG))
        elif isinstance(item, dict):
            local_ps.append(
                classify_existing_printer(printer_from_mapping(item), SOURCE_LOCAL_REG)
            )
    merged = merge_installed_printers(spooler_typed, user_ps, computer_ps, local_ps)
    return apply_default_flag(merged, default_name)


def _printers_from_connection_rows(
    rows: Sequence,
    source: str,
    default_name: str,
) -> List[NetworkPrinter]:
    out: List[NetworkPrinter] = []
    for row in rows or ():
        parsed = None
        if isinstance(row, dict):
            if row.get("unc") or row.get("server"):
                parsed = {
                    "leaf": str(row.get("leaf") or ""),
                    "server": str(row.get("server") or ""),
                    "share": str(row.get("share") or ""),
                    "unc": str(row.get("unc") or "") or normalize_unc(
                        f"\\\\{row.get('server') or ''}\\{row.get('share') or ''}"
                    ),
                }
            else:
                parsed = parse_connection_key(
                    str(row.get("leaf") or row.get("Name") or "")
                )
        else:
            parsed = parse_connection_key(str(row or ""))
        printer = connection_to_printer(parsed or {}, source=source, default_name=default_name)
        if printer is not None:
            out.append(printer)
    return out


def merge_installed_printers(
    *groups: Sequence[NetworkPrinter],
) -> List[NetworkPrinter]:
    """Deduplica entre spooler, conexões /in e /ga."""
    merged: Dict[str, NetworkPrinter] = {}
    order: List[str] = []
    for group in groups:
        for printer in group or ():
            if printer is None:
                continue
            key = identity_key(printer)
            if key == "empty":
                continue
            if key in merged:
                merged[key] = merge_printer_pair(merged[key], printer)
            else:
                merged[key] = printer
                order.append(key)
    return [merged[k] for k in order]


def classify_computer_printer_error(
    text: str,
    exit_code: int = 0,
    *,
    host: str = "",
) -> str:
    low = (text or "").casefold()
    name = normalize_host(host)
    if any(
        m in low
        for m in (
            "access is denied",
            "acesso negado",
            "0x80070005",
            "error 5",
            "logon failure",
            "1326",
        )
    ):
        return f"Acesso negado ao computador remoto {name}." if name else "Acesso negado ao computador remoto."
    if any(
        m in low
        for m in (
            "rpc server is unavailable",
            "servidor rpc",
            "0x800706ba",
            "the rpc server",
            "falha de rpc",
            "spooler",
        )
    ):
        return MSG_SPOOLER
    if "timeout" in low or "tempo limite" in low or exit_code in (2, 1460):
        return MSG_TIMEOUT
    if any(
        m in low
        for m in (
            "could not find",
            "cannot find",
            "não foi possível encontrar",
            "nao foi possivel encontrar",
            "network path was not found",
            "caminho de rede",
        )
    ):
        return MSG_GET_PRINTER
    stripped = (text or "").strip()
    if not stripped:
        return MSG_GET_PRINTER
    if stripped.lstrip()[:1] in "{[":
        return MSG_GET_PRINTER
    return stripped[:400] or MSG_GET_PRINTER


def result_belongs_to_current(
    *,
    result_host: str,
    current_host: str,
    result_generation: int,
    current_generation: int,
) -> bool:
    """Ignora resultado atrasado de outro host ou de uma consulta cancelada."""
    if int(result_generation) != int(current_generation):
        return False
    left = normalize_host(result_host).casefold()
    right = normalize_host(current_host).casefold()
    return bool(left) and left == right


def installed_query_readiness(
    *,
    host: str,
    online: bool,
    sessions: Sequence[RemoteSession],
    selected_session: Optional[RemoteSession],
    sessions_loading: bool,
) -> Tuple[str, Optional[RemoteSession]]:
    """Decide se a aba Instaladas pode consultar e com qual sessão.

    Retorna ``(mensagem, sessão)``. Sessão None → não iniciar worker.
    """
    target = normalize_host(host)
    if not target or not is_valid_host(target):
        return MSG_INVALID_HOST, None
    if not online:
        return MSG_OFFLINE, None
    if sessions_loading:
        return MSG_IDENTIFYING, None
    active = active_sessions(sessions)
    if not active:
        return MSG_NO_ACTIVE_SESSION, None
    if len(active) == 1:
        return "", active[0]
    if selected_session is None or not is_session_active(selected_session):
        return MSG_MULTI_SESSION, None
    return "", selected_session


def display_or_dash(value: str) -> str:
    text = (value or "").strip()
    return text if text else "—"
