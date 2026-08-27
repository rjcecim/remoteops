"""PsService — argv, parsers, modelos e traduções."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool

PSSERVICE_NAMES: Tuple[str, ...] = ("PsService64.exe", "PsService.exe")

PSSERVICE_QUERY_TIMEOUT_SECONDS = 120.0
PSSERVICE_ACTION_TIMEOUT_SECONDS = 90.0
PSSERVICE_FIND_TIMEOUT_SECONDS = 180.0

SETCONFIG_AUTO = "auto"
SETCONFIG_DEMAND = "demand"
SETCONFIG_DISABLED = "disabled"
SUPPORTED_SETCONFIG = (SETCONFIG_AUTO, SETCONFIG_DEMAND, SETCONFIG_DISABLED)

STATE_LABELS_PT: Dict[str, str] = {
    "STOPPED": "Parado",
    "START_PENDING": "Iniciando",
    "STOP_PENDING": "Parando",
    "RUNNING": "Executando",
    "CONTINUE_PENDING": "Continuando",
    "PAUSE_PENDING": "Pausando",
    "PAUSED": "Pausado",
}

START_TYPE_LABELS_PT: Dict[str, str] = {
    "BOOT_START": "Inicialização (boot)",
    "SYSTEM_START": "Sistema",
    "AUTO_START": "Automático",
    "DEMAND_START": "Manual",
    "DISABLED": "Desabilitado",
    "auto": "Automático",
    "demand": "Manual",
    "disabled": "Desabilitado",
}

START_TYPE_TO_SETCONFIG: Dict[str, str] = {
    "AUTO_START": SETCONFIG_AUTO,
    "DEMAND_START": SETCONFIG_DEMAND,
    "DISABLED": SETCONFIG_DISABLED,
    "auto": SETCONFIG_AUTO,
    "demand": SETCONFIG_DEMAND,
    "disabled": SETCONFIG_DISABLED,
}

SETCONFIG_TO_LABEL: Dict[str, str] = {
    SETCONFIG_AUTO: "Automático",
    SETCONFIG_DEMAND: "Manual",
    SETCONFIG_DISABLED: "Desabilitado",
}

PENDING_STATES = frozenset(
    {"START_PENDING", "STOP_PENDING", "PAUSE_PENDING", "CONTINUE_PENDING"}
)


@dataclass
class RemoteService:
    """Serviço remoto (query e/ou config mesclados)."""

    service_name: str
    display_name: str = ""
    description: str = ""
    state: str = ""
    state_code: Optional[int] = None
    service_type: str = ""
    start_type: str = ""
    start_type_code: Optional[int] = None
    account: str = ""
    binary_path: str = ""
    load_order_group: str = ""
    tag: str = ""
    dependencies: Tuple[str, ...] = ()
    controls_accepted: Tuple[str, ...] = ()
    win32_exit_code: str = ""
    service_exit_code: str = ""
    checkpoint: str = ""
    wait_hint: str = ""
    error_control: str = ""
    host: str = ""

    @property
    def state_label(self) -> str:
        return translate_state(self.state)

    @property
    def start_type_label(self) -> str:
        return translate_start_type(self.start_type)

    @property
    def is_pending(self) -> bool:
        return (self.state or "").upper() in PENDING_STATES

    @property
    def accepts_stop(self) -> bool:
        caps = {c.upper() for c in self.controls_accepted}
        if not caps:
            return True
        return "STOPPABLE" in caps or "ACCEPT_STOP" in caps

    @property
    def accepts_pause(self) -> bool:
        caps = {c.upper() for c in self.controls_accepted}
        if not caps:
            return False
        return "PAUSABLE" in caps or "ACCEPT_PAUSE_CONTINUE" in caps

    @property
    def setconfig_token(self) -> Optional[str]:
        key = (self.start_type or "").strip()
        return START_TYPE_TO_SETCONFIG.get(key) or START_TYPE_TO_SETCONFIG.get(
            key.upper()
        )


@dataclass
class ServiceSecurityInfo:
    service_name: str = ""
    display_name: str = ""
    account: str = ""
    raw_text: str = ""
    entries: List[str] = field(default_factory=list)


@dataclass
class ServiceFindHit:
    computer: str
    detail: str = ""


def build_remote_target(host: str) -> str:
    h = (host or "").strip().strip("\\")
    if not h:
        return ""
    return f"\\\\{h}"


def resolve_psservice_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSSERVICE_NAMES)


def psservice_available(pstools_dir: str = "") -> bool:
    path = resolve_psservice_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def _append_auth(args: List[str], user: str, password: str) -> None:
    u = (user or "").strip()
    if not u:
        return
    args.extend(["-u", u])
    if (password or "").strip():
        args.extend(["-p", password])


def build_psservice_argv(
    exe: str,
    command: str,
    *,
    host: str = "",
    service_name: str = "",
    extra: Optional[Sequence[str]] = None,
    user: str = "",
    password: str = "",
    nobanner: bool = True,
    accepteula: bool = True,
) -> List[str]:
    """
    Monta argv do PsService v2.26.

    Uso oficial::
        psservice [\\\\computer [-u user [-p pass]]] <cmd> <options>

    ``find`` não aceita computador/credenciais na sintaxe documentada.
    """
    if not exe or not (command or "").strip():
        return []
    cmd = command.strip().lower()
    args: List[str] = [exe]
    if accepteula:
        args.append("-accepteula")
    if nobanner:
        args.append("-nobanner")

    if cmd == "find":
        args.append("find")
        if service_name:
            args.append(service_name)
        if extra:
            args.extend(str(x) for x in extra if x is not None and str(x) != "")
        return args

    target = build_remote_target(host)
    if target:
        args.append(target)
        _append_auth(args, user, password)

    args.append(cmd)
    if service_name:
        args.append(service_name)
    if extra:
        args.extend(str(x) for x in extra if x is not None and str(x) != "")
    return args


def translate_state(raw: str) -> str:
    key = (raw or "").strip().upper()
    if not key:
        return "—"
    return STATE_LABELS_PT.get(key, raw.strip())


def translate_start_type(raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        return "—"
    return (
        START_TYPE_LABELS_PT.get(key)
        or START_TYPE_LABELS_PT.get(key.upper())
        or key
    )


def merge_query_and_config(
    query_rows: Sequence[RemoteService],
    config_rows: Sequence[RemoteService],
    *,
    host: str = "",
) -> List[RemoteService]:
    by_name: Dict[str, RemoteService] = {}
    for row in query_rows:
        key = (row.service_name or "").casefold()
        if not key:
            continue
        by_name[key] = replace(row, host=host or row.host)

    for cfg in config_rows:
        key = (cfg.service_name or "").casefold()
        if not key:
            continue
        base = by_name.get(key)
        if base is None:
            by_name[key] = replace(cfg, host=host or cfg.host)
            continue
        by_name[key] = replace(
            base,
            display_name=base.display_name or cfg.display_name,
            description=base.description or cfg.description,
            service_type=cfg.service_type or base.service_type,
            start_type=cfg.start_type or base.start_type,
            start_type_code=(
                cfg.start_type_code
                if cfg.start_type_code is not None
                else base.start_type_code
            ),
            account=cfg.account or base.account,
            binary_path=cfg.binary_path or base.binary_path,
            load_order_group=cfg.load_order_group or base.load_order_group,
            tag=cfg.tag or base.tag,
            dependencies=cfg.dependencies or base.dependencies,
            error_control=cfg.error_control or base.error_control,
            host=host or base.host,
        )

    rows = list(by_name.values())
    rows.sort(key=lambda r: ((r.display_name or r.service_name or "").casefold()))
    return rows


_FIELD_RE = re.compile(r"^\t([A-Z][A-Z0-9_]*)\s*:\s*(.*)$")
_CONTINUATION_RE = re.compile(r"^\t+\s*:\s*(.*)$")
_CONTROLS_RE = re.compile(r"^\t+\s*\(([^)]+)\)\s*$")
_SERVICE_NAME_RE = re.compile(r"^SERVICE_NAME:\s*(.+)\s*$", re.IGNORECASE)
_DISPLAY_NAME_RE = re.compile(r"^DISPLAY_NAME:\s*(.+)\s*$", re.IGNORECASE)
_STATE_VALUE_RE = re.compile(
    r"^(?:(?P<code>\d+)\s+)?(?P<token>[A-Z_]+)\b", re.IGNORECASE
)
_START_VALUE_RE = re.compile(
    r"^(?:(?P<code>\d+)\s+)?(?P<token>[A-Z_]+)\b", re.IGNORECASE
)
_FIND_LINE_RE = re.compile(
    r"^(?:\\\\)?(?P<computer>[A-Za-z0-9._\-]+)\s*(?P<detail>.*)$"
)


def _is_noise_line(line: str) -> bool:
    low = (line or "").strip().lower()
    if not low:
        return True
    if low.startswith("psservice "):
        return True
    if "copyright" in low and "sysinternals" in low:
        return True
    if low.startswith("usage:"):
        return True
    return False


def is_psservice_usage_text(text: str) -> bool:
    t = (text or "").lower()
    return "usage: psservice" in t or (
        "psservice lists or controls services" in t and "<cmd>" in t
    )


def _split_service_blocks(text: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if _SERVICE_NAME_RE.match(line.strip()):
            if current:
                blocks.append("\n".join(current))
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _parse_service_block(block: str) -> Optional[RemoteService]:
    lines = block.splitlines()
    if not lines:
        return None
    m_name = _SERVICE_NAME_RE.match(lines[0].strip())
    if not m_name:
        return None

    service_name = m_name.group(1).strip()
    display_name = ""
    description_parts: List[str] = []
    fields: Dict[str, List[str]] = {}
    controls: List[str] = []
    i = 1
    while i < len(lines):
        line = lines[i]
        if not line.strip() or _is_noise_line(line):
            i += 1
            continue
        m_disp = _DISPLAY_NAME_RE.match(line.strip())
        if m_disp:
            display_name = m_disp.group(1).strip()
            i += 1
            continue
        m_field = _FIELD_RE.match(line)
        if m_field:
            key = m_field.group(1).upper()
            val = (m_field.group(2) or "").strip()
            fields.setdefault(key, [])
            if val:
                fields[key].append(val)
            j = i + 1
            while j < len(lines):
                cont = lines[j]
                m_ctrl = _CONTROLS_RE.match(cont)
                if m_ctrl and key == "STATE":
                    controls = [
                        c.strip() for c in m_ctrl.group(1).split(",") if c.strip()
                    ]
                    j += 1
                    continue
                m_cont = _CONTINUATION_RE.match(cont)
                if m_cont:
                    extra = (m_cont.group(1) or "").strip()
                    if extra:
                        fields[key].append(extra)
                    j += 1
                    continue
                break
            i = j
            continue
        if not line.startswith("\t") and not line.strip().upper().startswith(
            "SERVICE_NAME:"
        ):
            description_parts.append(line.strip())
        i += 1

    def first(key: str) -> str:
        vals = fields.get(key) or []
        return vals[0] if vals else ""

    state_raw = first("STATE")
    state_code: Optional[int] = None
    state_token = ""
    m_state = _STATE_VALUE_RE.match(state_raw)
    if m_state:
        if m_state.group("code"):
            state_code = int(m_state.group("code"))
        state_token = m_state.group("token").upper()
    elif state_raw:
        state_token = state_raw.split()[0].upper()

    start_raw = first("START_TYPE")
    start_code: Optional[int] = None
    start_token = ""
    m_start = _START_VALUE_RE.match(start_raw)
    if m_start:
        if m_start.group("code"):
            start_code = int(m_start.group("code"))
        start_token = m_start.group("token").upper()
    elif start_raw:
        start_token = start_raw.split()[0].upper()

    return RemoteService(
        service_name=service_name,
        display_name=display_name or service_name,
        description=" ".join(description_parts).strip(),
        state=state_token,
        state_code=state_code,
        service_type=first("TYPE"),
        start_type=start_token,
        start_type_code=start_code,
        account=first("SERVICE_START_NAME"),
        binary_path=first("BINARY_PATH_NAME"),
        load_order_group=first("LOAD_ORDER_GROUP") or first("GROUP"),
        tag=first("TAG"),
        dependencies=tuple(fields.get("DEPENDENCIES") or ()),
        controls_accepted=tuple(controls),
        win32_exit_code=first("WIN32_EXIT_CODE"),
        service_exit_code=first("SERVICE_EXIT_CODE"),
        checkpoint=first("CHECKPOINT"),
        wait_hint=first("WAIT_HINT"),
        error_control=first("ERROR_CONTROL"),
    )


def parse_psservice_query(text: str) -> List[RemoteService]:
    rows: List[RemoteService] = []
    for block in _split_service_blocks(text):
        row = _parse_service_block(block)
        if row is not None:
            rows.append(row)
    return rows


def parse_psservice_config(text: str) -> List[RemoteService]:
    return parse_psservice_query(text)


def parse_psservice_depend(text: str) -> Tuple[List[RemoteService], bool]:
    """
    Parseia ``depend``.

    Retorna (lista, empty_confirmed). empty_confirmed=True quando a saída
    afirma explicitamente que não há dependentes.
    """
    t = (text or "").strip()
    low = t.lower()
    if (
        "has no dependent" in low
        or "no dependent services" in low
        or "não possui serviços dependentes" in low
    ):
        return [], True
    return parse_psservice_query(text), False


def parse_psservice_security(text: str) -> ServiceSecurityInfo:
    info = ServiceSecurityInfo(raw_text=text or "")
    entries: List[str] = []
    current = ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        m_name = _SERVICE_NAME_RE.match(stripped)
        if m_name:
            info.service_name = m_name.group(1).strip()
            continue
        m_disp = _DISPLAY_NAME_RE.match(stripped)
        if m_disp:
            info.display_name = m_disp.group(1).strip()
            continue
        if stripped.upper().startswith("ACCOUNT:"):
            info.account = stripped.split(":", 1)[-1].strip()
            continue
        if stripped.startswith("[ALLOW]") or stripped.startswith("[DENY]"):
            if current:
                entries.append(current.strip())
            current = stripped
            continue
        if current and (line.startswith("\t") or (
            stripped
            and not stripped.upper().startswith("SERVICE_")
            and not stripped.upper().startswith("DISPLAY_")
            and not stripped.upper().startswith("SECURITY")
        )):
            current += "\n" + stripped
    if current:
        entries.append(current.strip())
    info.entries = entries
    return info


def parse_psservice_find(text: str) -> List[ServiceFindHit]:
    hits: List[ServiceFindHit] = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _is_noise_line(line):
            continue
        low = line.lower()
        if "no instances" in low or "nenhuma instância" in low:
            continue
        if low.startswith("service ") and "found" in low:
            continue
        if line.upper().startswith("SERVICE_NAME:") or line.upper().startswith(
            "DISPLAY_NAME:"
        ):
            continue
        m = _FIND_LINE_RE.match(line)
        if not m:
            continue
        computer = m.group("computer").strip()
        if not computer or computer.casefold() in seen:
            continue
        if computer.casefold() in {"psservice", "usage", "error", "acesso"}:
            continue
        seen.add(computer.casefold())
        hits.append(
            ServiceFindHit(
                computer=computer,
                detail=(m.group("detail") or "").strip(),
            )
        )
    return hits


def classify_psservice_error(text: str, returncode: int = 1) -> str:
    t = (text or "").lower()
    if "access is denied" in t or "acesso negado" in t:
        return "acesso_negado"
    if "logon failure" in t or "user name or password" in t:
        return "credenciais_invalidas"
    if "network path was not found" in t or "caminho de rede" in t:
        return "host_offline"
    if "does not exist" in t or "cannot open service" in t or "não existe" in t:
        return "servico_inexistente"
    if "already running" in t or "already been started" in t:
        return "ja_executando"
    if "has not been started" in t or "não foi iniciado" in t:
        return "ja_parado"
    if "disabled" in t and ("cannot" in t or "não" in t):
        return "desabilitado"
    if "cannot accept control" in t:
        return "controle_recusado"
    if "dependent services" in t or "serviços dependentes" in t:
        return "dependentes"
    if "timed out" in t or "timeout" in t:
        return "timeout"
    if returncode != 0 and not (text or "").strip():
        return "falha_desconhecida"
    return "erro"


def friendly_error_message(kind: str, detail: str = "") -> str:
    messages = {
        "acesso_negado": "Acesso negado ao gerenciar serviços no host remoto.",
        "credenciais_invalidas": "Credenciais inválidas para o host remoto.",
        "host_offline": "Host inacessível ou caminho de rede não encontrado.",
        "servico_inexistente": "Serviço não encontrado no host remoto.",
        "ja_executando": "O serviço já está em execução.",
        "ja_parado": "O serviço já está parado.",
        "desabilitado": "O serviço está desabilitado e não pode ser iniciado.",
        "controle_recusado": "O serviço não aceita este controle no estado atual.",
        "dependentes": "Há serviços dependentes impedindo a operação.",
        "timeout": "A operação excedeu o tempo limite.",
        "erro": "Falha ao executar PsService.",
        "falha_desconhecida": "Falha ao executar PsService.",
    }
    base = messages.get(kind, messages["erro"])
    extra = (detail or "").strip()
    if extra and kind in {"erro", "falha_desconhecida"}:
        return f"{base} {extra}"
    return base
