from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PsInfoHotfix:
    id: str
    installed: str
    description: str = ""


@dataclass
class PsInfoResult:
    host: str
    header: List[str]
    system: Dict[str, str]
    applications: List[str]
    disks_raw: List[str]
    hotfixes: List[PsInfoHotfix]
    raw_text: str
    tool_version: str = ""


@dataclass
class PsInfoDiskRow:
    volume: str
    type: str
    format: str
    label: str
    size: str
    free: str
    free_pct: str
    used: str = ""
    size_bytes: int = 0
    free_bytes: int = 0
    used_bytes: int = 0
    free_pct_value: Optional[float] = None


# Ordem e agrupamento do card Sistema (chaves originais do PsInfo).
SYSTEM_FIELD_GROUPS: List[tuple[str, List[str]]] = [
    (
        "Sistema operacional",
        [
            "Kernel version",
            "Kernel build number",
            "Product type",
            "Product version",
            "Service pack",
            "Install date",
            "Activation status",
            "Expiration date",
            "System root",
            "Uptime",
            "IE version",
        ],
    ),
    (
        "Hardware",
        [
            "Processors",
            "Processor type",
            "Processor speed",
            "Physical memory",
            "Video driver",
        ],
    ),
    (
        "Registro",
        [
            "Registered owner",
            "Registered organization",
        ],
    ),
]

SYSTEM_FIELD_LABELS_PT: Dict[str, str] = {
    "Kernel version": "Versão do kernel",
    "Kernel build number": "Build do kernel",
    "Product type": "Tipo do produto",
    "Product version": "Versão do produto",
    "Service pack": "Service pack",
    "Install date": "Data de instalação",
    "Activation status": "Status de ativação",
    "Expiration date": "Data de expiração",
    "System root": "Pasta do sistema",
    "Uptime": "Tempo ligado",
    "IE version": "Versão do IE",
    "Processors": "Processadores",
    "Processor type": "Tipo do processador",
    "Processor speed": "Velocidade do processador",
    "Physical memory": "Memória física",
    "Video driver": "Driver de vídeo",
    "Registered owner": "Proprietário registrado",
    "Registered organization": "Organização registrada",
}

_PSINFO_VERSION_RE = re.compile(r"PsInfo\s+v?([\d.]+)", re.IGNORECASE)
_HOTFIX_HEADER_RE = re.compile(r"OS\s+Hot\s*Fix", re.IGNORECASE)
_HOTFIX_LINE_RE = re.compile(
    r"^(?P<id>(?:KB|Q)?\d[\w.-]*)\s+(?P<date>\S.*\S|\S+)\s*$",
    re.IGNORECASE,
)


@dataclass
class InstalledApp:
    display_name: str
    version: str
    publisher: str
    display_line: str
    product_code: str
    uninstall_string: str
    quiet_uninstall_string: str
    is_msi: bool
    arch: str  # "64" | "32"


@dataclass
class HostInventoryStatus:
    """Resultado tipado da consulta de inventário remoto via Remote Registry."""

    host: str
    ok: bool
    apps: List[InstalledApp] = field(default_factory=list)
    # "": sucesso; invalid_host | unreachable | auth | remote_registry |
    # timed_out | cancelled | internal_error
    error_kind: str = ""
    message: str = ""
    winerror: Optional[int] = None
    # validate | connect | enumerate | spawn | ipc | timeout | cancel | child
    stage: str = ""


_GUID_RE = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"
)
_UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

# Win32 codes usados para classificar falhas de ConnectRegistry / Remote Registry
_AUTH_WINERRORS = frozenset({5, 86, 1326, 1327, 1330, 1789, 2202})
_UNREACHABLE_WINERRORS = frozenset(
    {51, 53, 64, 67, 1231, 10051, 10060, 10061, 10065}
)
_REMOTE_REGISTRY_WINERRORS = frozenset({1707, 1722, 1753})


def _strip_host(host: str) -> str:
    h = (host or "").strip()
    # Aceita entrada como "\\\\HOST" ou "HOST"
    return h.strip("\\").strip()


def build_psinfo_target(host: str) -> str:
    h = _strip_host(host)
    return f"\\\\{h}" if h else ""


def build_psinfo_argv(
    exe: str,
    host: str,
    *,
    include_disks: bool = True,
    include_hotfixes: bool = False,
    include_software: bool = False,
    nobanner: bool = True,
    user: str = "",
    password: str = "",
) -> List[str]:
    """
    Monta argv do PsInfo v1.79+.

    Uso oficial: ``psinfo [-h] [-s] [-d] ... [\\\\computer [-u user [-p pass]]]``
    Switches **antes** do alvo. ``-accepteula`` não existe no PsInfo
    (passá-lo pode fazer o utilitário só imprimir o help).
    """
    target = build_psinfo_target(host)
    if not exe or not target:
        return []
    args = [exe]
    if include_hotfixes:
        args.append("-h")
    if include_software:
        args.append("-s")
    if include_disks:
        args.append("-d")
    if nobanner:
        args.append("-nobanner")
    args.append(target)
    u = (user or "").strip()
    if u:
        args.extend(["-u", u])
        if (password or "").strip():
            args.extend(["-p", password])
    return args


def is_psinfo_usage_text(text: str) -> bool:
    """True se a saída for a tela de Usage (comando malformado)."""
    t = (text or "").lower()
    return "usage: psinfo" in t or ("psinfo returns information" in t and "-nobanner" in t)


def _reg_str(sub, value_name: str) -> str:
    import winreg

    try:
        value, _ = winreg.QueryValueEx(sub, value_name)
        text = str(value or "").strip()
        # Remove nulos/controles que quebram cmd/Qt
        return "".join(ch for ch in text if ch >= " " or ch in "\t")
    except OSError:
        return ""


def _detect_msi(sub_key: str, uninstall_string: str) -> tuple[bool, str]:
    key = (sub_key or "").strip()
    if _GUID_RE.fullmatch(key):
        return True, key
    us = uninstall_string or ""
    if "msiexec" in us.lower():
        m = _GUID_RE.search(us)
        if m:
            return True, m.group(0)
    return False, ""


def _app_identity_key(app: InstalledApp) -> Tuple[str, str, str]:
    return (
        (app.display_name or "").casefold(),
        (app.version or "").casefold(),
        (app.publisher or "").casefold(),
    )


def _dedup_key(app: InstalledApp) -> Tuple:
    """Chave de deduplicação: product_code (MSI) ou (nome, versão, publisher, arch)."""
    pc = (app.product_code or "").strip()
    if pc:
        return ("pc", pc.casefold())
    return ("nvpa",) + _app_identity_key(app) + ((app.arch or "").casefold(),)


def _apps_from_uninstall(root, access: int, arch: str) -> List[InstalledApp]:
    """Lê a view Uninstall indicada; deduplica por product_code ou (nome, versão, publisher, arch)."""
    import winreg

    apps: List[InstalledApp] = []
    seen: Dict[Tuple, InstalledApp] = {}
    try:
        uninstall = winreg.OpenKey(root, _UNINSTALL_KEY, 0, access)
    except OSError:
        return apps
    try:
        i = 0
        while True:
            try:
                sub_name = winreg.EnumKey(uninstall, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(uninstall, sub_name) as sub:
                    name = _reg_str(sub, "DisplayName")
                    if not name:
                        continue
                    version = _reg_str(sub, "DisplayVersion")
                    publisher = _reg_str(sub, "Publisher")
                    uninstall_string = _reg_str(sub, "UninstallString")
                    quiet = _reg_str(sub, "QuietUninstallString")
                    is_msi, product_code = _detect_msi(sub_name, uninstall_string)
                    display_line = f"{name} {version}".strip() if version else name
                    app = InstalledApp(
                        display_name=name,
                        version=version,
                        publisher=publisher,
                        display_line=display_line,
                        product_code=product_code,
                        uninstall_string=uninstall_string,
                        quiet_uninstall_string=quiet,
                        is_msi=is_msi,
                        arch=arch,
                    )
                    key = _dedup_key(app)
                    prev = seen.get(key)
                    # Preferir entrada que já tem versão preenchida
                    if prev is None or (version and not prev.version):
                        seen[key] = app
            except OSError:
                continue
    finally:
        try:
            uninstall.Close()
        except OSError:
            pass
    return list(seen.values())


def _with_arch_suffix(app: InstalledApp, arch_label: str) -> InstalledApp:
    """Acrescenta (64-bit)/(32-bit) no nome quando o DisplayName não indica arquitetura."""
    label = f"({arch_label})"
    if label.casefold() in app.display_name.casefold():
        return app
    new_name = f"{app.display_name} {label}"
    display_line = f"{new_name} {app.version}".strip() if app.version else new_name
    return InstalledApp(
        display_name=new_name,
        version=app.version,
        publisher=app.publisher,
        display_line=display_line,
        product_code=app.product_code,
        uninstall_string=app.uninstall_string,
        quiet_uninstall_string=app.quiet_uninstall_string,
        is_msi=app.is_msi,
        arch=app.arch,
    )


def _merge_arch_views(apps_64: List[InstalledApp], apps_32: List[InstalledApp]) -> List[InstalledApp]:
    """
    Une views 64/32:
    - com product_code: uma entrada (preferência 64);
    - sem product_code: (nome, versão, publisher) em ambas as views → manter ambas com sufixo;
    - demais: manter como estão.
    """
    by_pc: Dict[str, InstalledApp] = {}
    for app in apps_64:
        pc = (app.product_code or "").strip()
        if pc:
            by_pc[pc.casefold()] = app
    for app in apps_32:
        pc = (app.product_code or "").strip()
        if pc:
            key = pc.casefold()
            if key not in by_pc:
                by_pc[key] = app

    non_64 = [a for a in apps_64 if not (a.product_code or "").strip()]
    non_32 = [a for a in apps_32 if not (a.product_code or "").strip()]

    map_64: Dict[Tuple[str, str, str], InstalledApp] = {}
    for app in non_64:
        map_64[_app_identity_key(app)] = app
    map_32: Dict[Tuple[str, str, str], InstalledApp] = {}
    for app in non_32:
        map_32[_app_identity_key(app)] = app

    keys_64 = set(map_64)
    keys_32 = set(map_32)

    out: List[InstalledApp] = list(by_pc.values())
    for k in keys_64 - keys_32:
        out.append(map_64[k])
    for k in keys_32 - keys_64:
        out.append(map_32[k])
    for k in keys_64 & keys_32:
        out.append(_with_arch_suffix(map_64[k], "64-bit"))
        out.append(_with_arch_suffix(map_32[k], "32-bit"))

    # Dedup final estável (product_code / uninstall + linha + arch)
    seen: set[tuple[str, str, str]] = set()
    unique: List[InstalledApp] = []
    for app in sorted(out, key=lambda a: a.display_line.casefold()):
        key = (
            app.display_line.casefold(),
            app.arch,
            app.product_code or app.uninstall_string,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(app)
    return unique


def _winerror_code(exc: BaseException) -> Optional[int]:
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return winerror
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        return errno
    return None


def _classify_connect_error(exc: OSError) -> Tuple[str, str]:
    """Mapeia OSError do ConnectRegistry para error_kind + mensagem curta."""
    code = _winerror_code(exc)
    detail = str(exc).strip() or (f"WinError {code}" if code is not None else "erro de registro remoto")

    if code in _AUTH_WINERRORS:
        return "auth", detail
    if code in _UNREACHABLE_WINERRORS:
        return "unreachable", detail
    if code in _REMOTE_REGISTRY_WINERRORS:
        return "remote_registry", detail
    # Falhas de conexão remota sem código conhecido: tratar como Remote Registry / RPC
    return "remote_registry", detail


def list_remote_installed_apps_status(host: str) -> HostInventoryStatus:
    """
    Lista aplicativos instalados no host via Remote Registry (HKLM Uninstall),
    unindo as views 64-bit e 32-bit (Wow6432Node), com classificação de erro.
    """
    import winreg

    h = _strip_host(host)
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
        root = winreg.ConnectRegistry(rf"\\{h}", winreg.HKEY_LOCAL_MACHINE)
    except OSError as exc:
        kind, msg = _classify_connect_error(exc)
        return HostInventoryStatus(
            host=h,
            ok=False,
            apps=[],
            error_kind=kind,
            message=msg,
            winerror=_winerror_code(exc),
            stage="connect",
        )

    try:
        apps_64 = _apps_from_uninstall(root, winreg.KEY_READ | winreg.KEY_WOW64_64KEY, "64")
        apps_32 = _apps_from_uninstall(root, winreg.KEY_READ | winreg.KEY_WOW64_32KEY, "32")
    except OSError as exc:
        return HostInventoryStatus(
            host=h,
            ok=False,
            apps=[],
            error_kind="remote_registry",
            message=str(exc).strip() or "Falha ao enumerar Uninstall.",
            winerror=_winerror_code(exc),
            stage="enumerate",
        )
    except Exception as exc:  # noqa: BLE001 — classificar como internal_error
        return HostInventoryStatus(
            host=h,
            ok=False,
            apps=[],
            error_kind="internal_error",
            message=f"{type(exc).__name__}: {exc}",
            stage="enumerate",
        )
    finally:
        try:
            root.Close()
        except OSError:
            pass

    return HostInventoryStatus(
        host=h,
        ok=True,
        apps=_merge_arch_views(apps_64, apps_32),
        error_kind="",
        message="",
        stage="enumerate",
    )


def list_remote_installed_apps_ex(host: str) -> tuple[bool, List[InstalledApp]]:
    """
    Lista aplicativos instalados no host via Remote Registry (HKLM Uninstall),
    unindo as views 64-bit e 32-bit (Wow6432Node).

    Retorna (ok, apps):
    - ok=False se o host estiver inacessível / ConnectRegistry falhar;
    - ok=True com lista (possivelmente vazia) quando a conexão remoto funcionou.
    """
    status = list_remote_installed_apps_status(host)
    return status.ok, status.apps


def list_remote_installed_apps(host: str) -> List[InstalledApp]:
    """
    Lista aplicativos instalados no host via Remote Registry (HKLM Uninstall),
    unindo as views 64-bit e 32-bit (Wow6432Node).

    display_line no formato PsInfo: "DisplayName DisplayVersion".
    Em falha de conexão retorna lista vazia (compatível com uso anterior).
    """
    _ok, apps = list_remote_installed_apps_ex(host)
    return apps


def extract_uninstall_executable(uninstall_string: str) -> str:
    """
    Extrai o caminho do executável de um UninstallString.
    Trata caminhos sem aspas com espaços (ex.: C:\\Program Files\\WinRAR\\uninstall.exe).
    """
    s = (uninstall_string or "").strip()
    if not s:
        return ""
    if s.lower().startswith("msiexec"):
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        if end > 1:
            return s[1:end]
    lower = s.lower()
    for ext in (".exe", ".cmd", ".bat"):
        idx = lower.find(ext)
        if idx != -1:
            return s[: idx + len(ext)].strip()
    try:
        parts = shlex.split(s, posix=False)
    except ValueError:
        parts = s.split()
    if not parts:
        return ""
    return parts[0].strip().strip('"')


def quote_uninstall_command(cmd: str) -> str:
    """Garante aspas no executável quando o caminho tem espaços (necessário p/ PsExec)."""
    s = (cmd or "").strip()
    if not s:
        return s
    if s.lower().startswith("msiexec"):
        return s
    if s.startswith('"'):
        return s
    exe = extract_uninstall_executable(s)
    if not exe or " " not in exe:
        return s
    if s.startswith(exe):
        rest = s[len(exe) :].lstrip()
        return f'"{exe}"' + (f" {rest}" if rest else "")
    return f'"{exe}"'


def build_uninstall_remote_cmd(app: InstalledApp, extra_params: str = "") -> str:
    """
    Monta o comando remoto de desinstalação (já com aspas corretas).
    MSI: msiexec /x '{GUID}' /qn /norestart [extras]
    EXE com extras: "exe" + extras
    EXE sem extras: QuietUninstallString ou UninstallString (aspas se necessário)
    """
    extra = (extra_params or "").strip()

    if app.is_msi and app.product_code:
        # Aspas duplas no GUID: mais seguro no cmd.exe do que aspas simples
        cmd = f'msiexec /x "{app.product_code}" /qn /norestart'
        if extra:
            cmd = f"{cmd} {extra}"
        return cmd

    base = (app.quiet_uninstall_string or "").strip() or (app.uninstall_string or "").strip()
    if not base:
        raise ValueError("Este aplicativo não possui string de desinstalação no registro.")

    if extra:
        exe = extract_uninstall_executable(base)
        if not exe:
            raise ValueError("Não foi possível obter o executável de desinstalação deste aplicativo.")
        quoted = f'"{exe}"' if (" " in exe and not exe.startswith('"')) else exe
        return f"{quoted} {extra}"

    return quote_uninstall_command(base)


def describe_uninstall(app: InstalledApp, extra_params: str = "") -> str:
    """Texto curto para tooltip: tipo + comando (truncado para o limite do Qt)."""
    kind = "MSI" if app.is_msi and app.product_code else "EXE"
    try:
        cmd = build_uninstall_remote_cmd(app, extra_params)
    except ValueError as exc:
        return f"{kind}: {exc}"
    # Evita "Application text must be shorter than 32768 characters" e tooltips gigantes
    if len(cmd) > 400:
        cmd = cmd[:397] + "..."
    return f"{kind}: {cmd}"


def _extract_tool_version(header_lines: List[str]) -> str:
    for line in header_lines:
        m = _PSINFO_VERSION_RE.search(line or "")
        if m:
            return m.group(1)
    return ""


def parse_psinfo_output(text: str, host: str = "") -> PsInfoResult:
    """
    Faz parse do stdout do PsInfo (Sysinternals).
    Suporta:
    - bloco "System information for \\\\HOST:" seguido de pares "Chave: Valor"
    - seção "Applications:" (lista)
    - tabela de volumes (``-d``)
    - hotfixes (``-h``, seção "OS Hot Fix Installed")
    """
    raw = text or ""
    lines = raw.splitlines()

    header: List[str] = []
    system: Dict[str, str] = {}
    applications: List[str] = []
    disks_raw: List[str] = []
    hotfixes: List[PsInfoHotfix] = []

    in_system = False
    in_apps = False
    in_disks = False
    in_hotfixes = False

    def _reset_sections(*, system: bool = False, apps: bool = False, disks: bool = False, hotfixes: bool = False):
        nonlocal in_system, in_apps, in_disks, in_hotfixes
        in_system = system
        in_apps = apps
        in_disks = disks
        in_hotfixes = hotfixes

    for ln in lines:
        s = ln.rstrip("\n\r")
        stripped = s.strip()
        if not stripped:
            if in_disks:
                disks_raw.append(s)
            continue

        if (
            stripped.startswith("PsInfo v")
            or stripped.startswith("PsInfo ")
            or "Sysinternals" in stripped
            or "www.sysinternals.com" in stripped
        ):
            header.append(s)
            continue

        if stripped.startswith("System information for"):
            _reset_sections(system=True)
            header.append(s)
            continue

        if stripped == "Applications:":
            _reset_sections(apps=True)
            continue

        if _HOTFIX_HEADER_RE.search(stripped):
            _reset_sections(hotfixes=True)
            continue

        # Heurística para iniciar tabela de discos: cabeçalho do PsInfo -d
        if stripped.startswith("Volume") and "Free" in stripped and "Format" in stripped:
            _reset_sections(disks=True)
            disks_raw.append(s)
            continue

        if in_hotfixes:
            m = _HOTFIX_LINE_RE.match(stripped)
            if m:
                hotfixes.append(
                    PsInfoHotfix(id=m.group("id").strip(), installed=m.group("date").strip())
                )
            continue

        if in_apps:
            applications.append(stripped)
            continue

        if in_disks:
            # Proteção: hotfix pode aparecer sem cabeçalho claro em alguns builds
            if _HOTFIX_HEADER_RE.search(stripped):
                _reset_sections(hotfixes=True)
                continue
            disks_raw.append(s)
            continue

        if in_system:
            # Formato: "Kernel version:            Windows 10 Pro, ..."
            if ":" in s:
                key, val = s.split(":", 1)
                system[key.strip()] = val.strip()
            else:
                header.append(s)
            continue

        header.append(s)

    return PsInfoResult(
        host=_strip_host(host),
        header=header,
        system=system,
        applications=applications,
        disks_raw=disks_raw,
        hotfixes=hotfixes,
        raw_text=raw,
        tool_version=_extract_tool_version(header),
    )


def _parse_hotfix_json(out: str) -> tuple[List[PsInfoHotfix], str]:
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], "Resposta inválida do Get-HotFix."

    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        return [], "Formato inesperado do Get-HotFix."

    items: List[PsInfoHotfix] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hid = str(row.get("HotFixID") or "").strip()
        if not hid:
            continue
        items.append(
            PsInfoHotfix(
                id=hid,
                installed=str(row.get("InstalledOn") or "").strip(),
                description=str(row.get("Description") or "").strip(),
            )
        )
    items.sort(key=lambda x: x.id.lower())
    return items, ""


def _shorten_hotfix_error(err: str) -> str:
    t = err or ""
    low = t.lower()
    if "0x80070005" in t or "e_accessdenied" in low or "acesso negado" in low or "access is denied" in low:
        return (
            "Acesso negado no Get-HotFix (WMI). "
            "Preencha Usuário/Senha em Autenticação (aba PsExec) e tente de novo."
        )
    # primeira linha útil, sem stack do PowerShell
    for line in t.splitlines():
        s = line.strip()
        if s and not s.startswith("+") and not s.startswith("at "):
            return s[:240]
    return t[:240] if t else "Falha no Get-HotFix."


_GET_HOTFIX_SELECT = (
    "Select-Object HotFixID, Description, "
    "@{N='InstalledOn';E={ if ($null -ne $_.InstalledOn) { "
    "try { ([datetime]$_.InstalledOn).ToString('yyyy-MM-dd') } "
    "catch { [string]$_.InstalledOn } } else { '' } }} | "
    "ConvertTo-Json -Compress"
)


def list_remote_hotfixes(
    host: str,
    *,
    user: str = "",
    password: str = "",
    timeout: float = 60.0,
    pstools_dir: str = "",
) -> tuple[List[PsInfoHotfix], str]:
    """
    Lista hotfixes remotos.

    1) ``Get-HotFix -ComputerName`` (com ``-Credential`` se houver usuário/senha)
    2) Se acesso negado: ``PsExec`` rodando ``Get-HotFix`` localmente no remoto

    Retorna ``(lista, nota_ou_erro)``.
    """
    import os

    h = _strip_host(host)
    if not h:
        return [], "Host inválido."

    u = (user or "").strip()
    p = password or ""

    # Credenciais via env (não vão na linha de comando do -Command)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$h = $env:RO_HF_HOST; $u = $env:RO_HF_USER; $pw = $env:RO_HF_PASS; "
        "if ($u) { "
        "$sec = ConvertTo-SecureString $pw -AsPlainText -Force; "
        "$cred = New-Object System.Management.Automation.PSCredential ($u, $sec); "
        f"Get-HotFix -ComputerName $h -Credential $cred | {_GET_HOTFIX_SELECT} "
        "} else { "
        f"Get-HotFix -ComputerName $h | {_GET_HOTFIX_SELECT} "
        "}"
    )
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    env = os.environ.copy()
    env["RO_HF_HOST"] = h
    env["RO_HF_USER"] = u
    env["RO_HF_PASS"] = p if u else ""

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
            timeout=max(5.0, float(timeout)),
            creationflags=creationflags,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return [], f"Get-HotFix excedeu {int(timeout)}s."
    except OSError as exc:
        return [], f"Falha ao iniciar PowerShell: {exc}"
    finally:
        env["RO_HF_PASS"] = ""

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        items, parse_err = _parse_hotfix_json(out)
        if items:
            return items, ""
        if parse_err and proc.returncode == 0:
            return [], parse_err

    short = _shorten_hotfix_error(err or f"Get-HotFix falhou (exit {proc.returncode}).")
    access_denied = "acesso negado" in short.lower() or "0x80070005" in (err or "")

    # Fallback: Get-HotFix local no remoto via PsExec (usa credenciais do formulário)
    if access_denied or not out:
        items_px, err_px = _list_hotfixes_via_psexec(
            h,
            user=u,
            password=p,
            timeout=timeout,
            pstools_dir=pstools_dir,
        )
        if items_px:
            return items_px, "via PsExec"
        if err_px:
            return [], f"{short} | Fallback PsExec: {err_px}"
    return [], short


def _list_hotfixes_via_psexec(
    host: str,
    *,
    user: str = "",
    password: str = "",
    timeout: float = 90.0,
    pstools_dir: str = "",
) -> tuple[List[PsInfoHotfix], str]:
    """Executa Get-HotFix no host remoto (consulta local) via PsExec."""
    try:
        from remoteops.services.ops import (
            CredentialContext,
            build_psexec_argv,
            resolve_psexec_exe,
        )
        from remoteops.utils.pstools import get_pstools_dir
    except Exception as exc:
        return [], f"PsExec indisponível ({exc})."

    psexec = resolve_psexec_exe(pstools_dir or get_pstools_dir())
    remote_cmd = (
        "Get-HotFix | "
        + _GET_HOTFIX_SELECT
    )
    remote_argv = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        remote_cmd,
    ]
    creds = CredentialContext(user=user or "", password=password or "")
    argv = build_psexec_argv(
        psexec_exe=psexec,
        host=host,
        remote_argv=remote_argv,
        creds=creds,
        extra_flags=["-accepteula", "-nobanner", "-h", "-s"],
        include_password=True,
    )
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(15.0, float(timeout)),
            creationflags=creationflags,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return [], f"PsExec/Get-HotFix excedeu {int(timeout)}s."
    except OSError as exc:
        return [], str(exc)
    finally:
        creds.clear()

    out = (proc.stdout or "").strip()
    # PsExec mistura banner/erros; tenta achar JSON no stdout
    json_blob = out
    if not json_blob.startswith("{") and not json_blob.startswith("["):
        for i, ch in enumerate(out):
            if ch in "{[":
                json_blob = out[i:].strip()
                break
    if json_blob:
        items, parse_err = _parse_hotfix_json(json_blob)
        if items:
            return items, ""
        if parse_err and proc.returncode == 0:
            return [], parse_err
    err = (proc.stderr or "").strip() or (out[:200] if out else "")
    return [], _shorten_hotfix_error(err or f"PsExec exit {proc.returncode}")


def format_key_values(system: Dict[str, str], order: Optional[List[str]] = None) -> List[tuple[str, str]]:
    if not system:
        return []
    if not order:
        return sorted(system.items(), key=lambda kv: kv[0].lower())
    out: List[tuple[str, str]] = []
    remaining = dict(system)
    for k in order:
        if k in remaining:
            out.append((k, remaining.pop(k)))
    for k in sorted(remaining.keys(), key=lambda x: x.lower()):
        out.append((k, remaining[k]))
    return out


def extract_psinfo_host(result: PsInfoResult) -> str:
    """Host do cabeçalho 'System information for \\\\HOST:' ou do campo result.host."""
    marker = "system information for"
    for line in result.header or []:
        s = (line or "").strip()
        low = s.lower()
        if not low.startswith(marker):
            continue
        # Não usar split("for"): quebraria em "infor…mation"
        rest = s[len(marker) :].strip().rstrip(":").strip()
        return rest.strip("\\").strip() or result.host
    return (result.host or "").strip()


def format_system_display(
    system: Dict[str, str],
    *,
    host: str = "",
    tool_version: str = "",
    hotfix_count: Optional[int] = None,
    labels: Optional[Dict[str, str]] = None,
) -> List[tuple[str, str, str]]:
    """
    Retorna linhas (grupo, rótulo, valor) para o card Sistema.
    Inclui Host e agrupa SO / Hardware / Registro; campos extras vão em "Outros".
    """
    labels = labels or SYSTEM_FIELD_LABELS_PT
    remaining = dict(system or {})
    rows: List[tuple[str, str, str]] = []

    host_disp = (host or "").strip().strip("\\")
    if host_disp:
        rows.append(("Geral", "Host", host_disp))
    if tool_version:
        rows.append(("Geral", "PsInfo", f"v{tool_version}"))
    if hotfix_count is not None:
        rows.append(("Geral", "Hotfixes", str(hotfix_count)))

    known_keys = {k for _, keys in SYSTEM_FIELD_GROUPS for k in keys}
    for group_name, keys in SYSTEM_FIELD_GROUPS:
        for key in keys:
            if key not in remaining:
                continue
            val = remaining.pop(key)
            rows.append((group_name, labels.get(key, key), val))

    for key in sorted(remaining.keys(), key=lambda x: x.lower()):
        if key in known_keys:
            continue
        rows.append(("Outros", labels.get(key, key), remaining[key]))

    return rows


_SIZE_RE = re.compile(
    r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(B|KB|MB|GB|TB|PB)\s*$", re.IGNORECASE
)
_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
    "PB": 1024**5,
}


def parse_size_to_bytes(text: str) -> int:
    """Converte '277.68 GB' / '809.7 MB' em bytes (0 se inválido)."""
    s = (text or "").strip()
    if not s:
        return 0
    m = _SIZE_RE.match(s)
    if not m:
        return 0
    num = m.group(1).replace(",", ".")
    try:
        value = float(num)
    except ValueError:
        return 0
    unit = m.group(2).upper()
    return int(value * _UNITS.get(unit, 0))


def format_bytes_compact(num_bytes: int) -> str:
    """Formata bytes no estilo PsInfo (ex.: 277.68 GB)."""
    if num_bytes <= 0:
        return ""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{num_bytes} B"


def parse_free_pct(text: str) -> Optional[float]:
    s = (text or "").strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _enrich_disk_row(row: PsInfoDiskRow) -> PsInfoDiskRow:
    size_b = parse_size_to_bytes(row.size)
    free_b = parse_size_to_bytes(row.free)
    used_b = max(0, size_b - free_b) if size_b > 0 else 0
    pct = parse_free_pct(row.free_pct)
    if pct is None and size_b > 0:
        pct = (free_b / size_b) * 100.0
    used = format_bytes_compact(used_b) if used_b > 0 else ("" if size_b <= 0 else "0 B")
    if not row.free_pct and pct is not None:
        row.free_pct = f"{pct:.1f}%"
    row.size_bytes = size_b
    row.free_bytes = free_b
    row.used_bytes = used_b
    row.used = used
    row.free_pct_value = pct
    return row


def parse_disks_table(disks_raw: List[str]) -> List[PsInfoDiskRow]:
    """
    Converte a tabela do PsInfo -d em linhas estruturadas.
    Aceita linhas completas e incompletas (ex.: ``A: Removable 0%``).
    """
    if not disks_raw:
        return []

    rows: List[PsInfoDiskRow] = []
    for line in disks_raw:
        s = (line or "").rstrip()
        if not s.strip():
            continue
        if s.strip().startswith("Volume"):
            continue

        parts = [p for p in s.split() if p]
        if not parts:
            continue

        volume = parts[0]
        # Linha mínima: "A: Removable 0%" ou "I: CD-ROM 0%"
        if len(parts) == 3 and parts[-1].endswith("%"):
            rows.append(
                _enrich_disk_row(
                    PsInfoDiskRow(
                        volume=volume,
                        type=parts[1],
                        format="",
                        label="",
                        size="",
                        free="",
                        free_pct=parts[2],
                    )
                )
            )
            continue

        if len(parts) < 4:
            continue

        # Completa: C: Fixed NTFS LABEL 476.10 GB 277.68 GB 58.3%
        if len(parts) >= 7 and parts[-1].endswith("%"):
            free_pct = parts[-1]
            free = " ".join(parts[-3:-1])
            size = " ".join(parts[-5:-3])
            vol_type = parts[1]
            fmt = parts[2]
            label = " ".join(parts[3:-5])
            rows.append(
                _enrich_disk_row(
                    PsInfoDiskRow(
                        volume=volume,
                        type=vol_type,
                        format=fmt,
                        label=label,
                        size=size,
                        free=free,
                        free_pct=free_pct,
                    )
                )
            )
            continue

        # Ex.: "H: CD-ROM CDFS JEDIOUTCAST 633.6 MB 0%" (sem livre absoluto)
        if parts[-1].endswith("%") and len(parts) >= 5:
            free_pct = parts[-1]
            # tenta "633.6 MB" antes do %
            if len(parts) >= 6 and parts[-2].upper() in _UNITS:
                size = " ".join(parts[-3:-1])
                mid = parts[1:-3]
            else:
                size = ""
                mid = parts[1:-1]
            vol_type = mid[0] if mid else ""
            fmt = mid[1] if len(mid) > 1 else ""
            label = " ".join(mid[2:]) if len(mid) > 2 else ""
            rows.append(
                _enrich_disk_row(
                    PsInfoDiskRow(
                        volume=volume,
                        type=vol_type,
                        format=fmt,
                        label=label,
                        size=size,
                        free="",
                        free_pct=free_pct,
                    )
                )
            )

    return rows


def prepare_disks_for_display(
    rows: List[PsInfoDiskRow],
    *,
    system_root: str = "",
    hide_empty_media: bool = True,
) -> tuple[List[PsInfoDiskRow], Optional[PsInfoDiskRow], str]:
    """
    Ordena Fixed primeiro, opcionalmente oculta Removable/CD-ROM sem tamanho,
    calcula totais Fixed e identifica volume do System root.

    Retorna (linhas, totais_fixed|None, volume_sistema).
    """
    root_vol = ""
    root = (system_root or "").strip()
    if root:
        # C:\WINDOWS -> C:
        letter = root[0:2] if len(root) >= 2 and root[1] == ":" else ""
        root_vol = letter.upper() if letter else ""

    filtered: List[PsInfoDiskRow] = []
    for row in rows:
        kind = (row.type or "").strip().lower()
        if hide_empty_media and kind in {"removable", "cd-rom", "cdrom"}:
            if not row.size_bytes and not row.format and not row.label:
                # Mantém se tiver % livre útil? linhas "0%" vazias → ocultar
                pct = row.free_pct_value
                if pct is None or pct <= 0:
                    continue
        filtered.append(row)

    def _sort_key(r: PsInfoDiskRow):
        kind = (r.type or "").strip().lower()
        rank = 0 if kind == "fixed" else 1 if kind == "remote" else 2
        return (rank, (r.volume or "").upper())

    filtered.sort(key=_sort_key)

    fixed = [r for r in filtered if (r.type or "").strip().lower() == "fixed" and r.size_bytes > 0]
    totals: Optional[PsInfoDiskRow] = None
    if fixed:
        size_b = sum(r.size_bytes for r in fixed)
        free_b = sum(r.free_bytes for r in fixed)
        used_b = max(0, size_b - free_b)
        pct = (free_b / size_b) * 100.0 if size_b else None
        totals = PsInfoDiskRow(
            volume="Total",
            type="Fixed",
            format="",
            label=f"{len(fixed)} volume(s)",
            size=format_bytes_compact(size_b),
            free=format_bytes_compact(free_b),
            free_pct=f"{pct:.1f}%" if pct is not None else "",
            used=format_bytes_compact(used_b),
            size_bytes=size_b,
            free_bytes=free_b,
            used_bytes=used_b,
            free_pct_value=pct,
        )

    return filtered, totals, root_vol
