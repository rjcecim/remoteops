"""Funções puras de impressoras de rede (UNC, PrintUIEntry, JSON, sessões).

Sem I/O de rede e sem Qt. O host do servidor vem das configurações —
não há nome padrão embutido no código.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from remoteops.core.win_cmd import argv_to_cmd_line
from remoteops.utils.sessions import RemoteSession

SCOPE_USER = "user"
SCOPE_ALL = "all"

PRINTUI_IN = "in"
PRINTUI_GA = "ga"
PRINTUI_Y = "y"
PRINTUI_DN = "dn"

TASK_NAME_PREFIX = "RemoteOps_Printer_"

_ACTIVE_STATES = frozenset(
    {
        "ativa",
        "active",
        "conectada",
        "connected",
    }
)


@dataclass(frozen=True)
class NetworkPrinter:
    name: str = ""
    share_name: str = ""
    driver_name: str = ""
    port_name: str = ""
    location: str = ""
    comment: str = ""
    published: bool = False

    @property
    def is_shared(self) -> bool:
        return bool((self.share_name or "").strip())


def print_server_host(server: str) -> str:
    """Host sem barras; vazio se não houver servidor."""
    return (server or "").strip().strip("\\")


def print_server_unc(server: str) -> str:
    """Monta ``\\\\servidor``. ``server`` vazio → ``ValueError``."""
    name = print_server_host(server)
    if not name:
        raise ValueError("Servidor de impressão vazio.")
    return f"\\\\{name}"


def printer_unc(share_name: str, server: str) -> str:
    """Monta ``\\\\server\\ShareName``. Nunca usa ``Name`` no lugar de ``ShareName``."""
    share = (share_name or "").strip().strip("\\")
    if not share:
        raise ValueError("ShareName vazio: a impressora não está compartilhada.")
    return f"{print_server_unc(server)}\\{share}"


def has_share_name(share_name: str) -> bool:
    return bool((share_name or "").strip().strip("\\"))


def ps_single_quote(value: str) -> str:
    """Literal PowerShell entre aspas simples (``'`` duplicado)."""
    return "'" + (value or "").replace("'", "''") + "'"


def encode_powershell(script: str) -> str:
    """UTF-16LE Base64 para ``-EncodedCommand`` (sem senha no script)."""
    return base64.b64encode((script or "").encode("utf-16-le")).decode("ascii")


def new_operation_id() -> str:
    return uuid.uuid4().hex


def scheduled_task_name(operation_id: str) -> str:
    op = re.sub(r"[^A-Za-z0-9_-]", "", operation_id or "") or new_operation_id()
    return f"{TASK_NAME_PREFIX}{op}"


def result_file_name(operation_id: str) -> str:
    op = re.sub(r"[^A-Za-z0-9_-]", "", operation_id or "") or new_operation_id()
    return f"RemoteOps_Printer_{op}.json"


def printui_argv(flag: str, unc: str) -> List[str]:
    """Argv lógico do PrintUIEntry silencioso. ``flag`` é ``in`` / ``ga`` / ``y`` / ``dn``."""
    op = (flag or "").strip().lstrip("/").lower()
    path = (unc or "").strip()
    if not path:
        raise ValueError("UNC da impressora vazio.")
    if op not in {PRINTUI_IN, PRINTUI_GA, PRINTUI_Y, PRINTUI_DN}:
        raise ValueError(f"Parâmetro PrintUIEntry inválido: {flag}")
    return ["rundll32.exe", "printui.dll,PrintUIEntry", f"/{op}", f"/n{path}", "/q"]


def printui_command_line(flag: str, unc: str) -> str:
    return argv_to_cmd_line(printui_argv(flag, unc))


def effective_set_default(scope: str, requested: bool) -> bool:
    """``/y`` só existe no contexto do usuário; nunca é global."""
    return bool(requested) and (scope or "").strip().lower() == SCOPE_USER


def should_connect_active_user(scope: str, has_active_user: bool) -> bool:
    """No modo todos, ``/ga`` + ``/in`` no usuário Active são complementares."""
    return (scope or "").strip().lower() == SCOPE_ALL and bool(has_active_user)


def should_prepare_driver(driver_present: bool) -> bool:
    return not bool(driver_present)


def can_proceed_to_connect(*, driver_ready: bool) -> bool:
    return bool(driver_ready)


def is_session_active(session: Optional[RemoteSession]) -> bool:
    if session is None:
        return False
    state = (session.state or "").strip().casefold()
    return state in _ACTIVE_STATES


def interactive_sessions(sessions: Sequence[RemoteSession]) -> List[RemoteSession]:
    return [s for s in sessions if (s.username or "").strip()]


def active_sessions(sessions: Sequence[RemoteSession]) -> List[RemoteSession]:
    return [s for s in interactive_sessions(sessions) if is_session_active(s)]


def select_default_active_session(
    sessions: Sequence[RemoteSession],
) -> Optional[RemoteSession]:
    """Uma sessão Active → escolhe; várias → None (ComboBox); desconectadas não."""
    found = active_sessions(sessions)
    if len(found) == 1:
        return found[0]
    return None


def qualify_interactive_user(username: str, domain_hint: str = "") -> str:
    """Garante ``DOMÍNIO\\usuário`` quando o WTS devolve só o login."""
    user = (username or "").strip()
    if not user:
        return ""
    if "\\" in user or "@" in user:
        return user
    hint = (domain_hint or "").strip().strip("\\")
    if hint and hint not in {".", "LOCAL"} and not hint.casefold().startswith("workgroup"):
        return f"{hint}\\{user}"
    return user


def printer_matches_query(printer: NetworkPrinter, query: str) -> bool:
    q = (query or "").strip().casefold()
    if not q:
        return True
    haystack = " ".join(
        [
            printer.name or "",
            printer.share_name or "",
            printer.driver_name or "",
            printer.location or "",
            printer.comment or "",
        ]
    ).casefold()
    return q in haystack


def decode_console_bytes(data: bytes) -> str:
    """Delega ao codec canónico em ``remoteops.core.console_codec``."""
    from remoteops.core.console_codec import decode_console_bytes as _decode

    return _decode(data)


def extract_json_value(text: str):
    """Extrai objeto ou array JSON de stdout misto (objeto único vs lista)."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch not in "{[":
            continue
        try:
            obj, _end = decoder.raw_decode(raw[i:])
            return obj
        except json.JSONDecodeError:
            continue
    return None


def coerce_mapping_list(obj) -> List[dict]:
    if obj is None:
        return []
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    if isinstance(obj, dict):
        return [obj]
    return []


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().casefold()
    return text in {"1", "true", "yes", "sim"}


def parse_printers_json(text: str) -> List[NetworkPrinter]:
    """Zero / uma / várias impressoras, acentos e objeto único vs array."""
    obj = extract_json_value(text)
    printers: List[NetworkPrinter] = []
    for item in coerce_mapping_list(obj):
        printers.append(
            NetworkPrinter(
                name=str(item.get("Name") or "").strip(),
                share_name=str(item.get("ShareName") or "").strip(),
                driver_name=str(item.get("DriverName") or "").strip(),
                port_name=str(item.get("PortName") or "").strip(),
                location=str(item.get("Location") or "").strip(),
                comment=str(item.get("Comment") or "").strip(),
                published=_as_bool(item.get("Published")),
            )
        )
    return printers


def read_printers_catalog_file(path: str) -> List[NetworkPrinter]:
    """Lê o JSON temporário gravado pelo PowerShell (UTF-8 / UTF-16)."""
    target = (path or "").strip()
    if not target:
        raise ValueError("Caminho do JSON temporário vazio.")
    with open(target, "rb") as fh:
        raw = fh.read()
    return parse_printers_json(decode_console_bytes(raw))


def classify_printer_error(
    text: str,
    exit_code: int = 0,
    *,
    server: str = "",
) -> str:
    low = (text or "").casefold()
    unc = ""
    host = print_server_host(server)
    if host:
        unc = f"\\\\{host}"
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
        if unc:
            return f"Acesso negado ao {unc}."
        return "Acesso negado ao servidor de impressão."
    if any(
        m in low
        for m in (
            "rpc server is unavailable",
            "servidor rpc",
            "0x800706ba",
            "the rpc server",
            "falha de rpc",
        )
    ):
        return "Falha de RPC."
    if "timeout" in low or "tempo limite" in low or exit_code in (2, 1460):
        if "tarefa" in low or "scheduled" in low or "interactive" in low:
            return "Timeout aguardando tarefa do usuário."
        if unc:
            return f"Timeout consultando o servidor de impressão {unc}."
        return "Timeout consultando o servidor de impressão."
    if any(
        m in low
        for m in (
            "could not find",
            "cannot find",
            "não foi possível encontrar",
            "nao foi possivel encontrar",
            "o servidor de impressão",
            "print server",
            "network path was not found",
            "caminho de rede",
        )
    ):
        return "Servidor de impressão inacessível."
    if "printuientry" in low or "rundll32" in low:
        return "PrintUIEntry retornou erro."
    stripped = (text or "").strip()
    if not stripped:
        return "Falha na operação de impressora."
    if stripped.lstrip()[:1] in "{[":
        return "Falha ao consultar o servidor de impressão."
    return stripped[:400]


def assert_text_has_no_secrets(text: str, passwords: Optional[Iterable[str]] = None) -> None:
    blob = text or ""
    for pwd in passwords or ():
        if pwd and pwd in blob:
            raise AssertionError("senha presente em texto sanitizado")


def build_list_printers_script(server: str = "", output_path: str = "") -> str:
    """Lista via ``Get-Printer -ComputerName`` e grava JSON em arquivo temporário.

    O catálogo não vai para stdout: pipe do Windows satura (~64KB) e o
    PowerShell trava no meio da lista. O processo pai lê o arquivo e apaga.
    """
    name = print_server_host(server)
    if not name:
        raise ValueError("Servidor de impressão vazio.")
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
        "    Select-Object Name,ShareName,DriverName,PortName,Location,Comment,Published)\n"
        "  Emit-Printers $rows\n"
        "  exit 0\n"
        "} catch {\n"
        "  Write-Error $_.Exception.Message\n"
        "  exit 1\n"
        "}\n"
    )


def build_driver_query_script(driver_name: str) -> str:
    name = ps_single_quote(driver_name or "")
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "try {\n"
        f"  $null = Get-PrinterDriver -Name {name} -ErrorAction Stop\n"
        "  Write-Output '{\"present\":true}'\n"
        "  exit 0\n"
        "} catch {\n"
        "  Write-Output '{\"present\":false}'\n"
        "  exit 0\n"
        "}\n"
    )


def build_driver_prepare_script(*, unc: str, driver_name: str) -> str:
    """Conexão temporária elevada só para preparar o driver; depois remove ``/dn``."""
    unc_lit = ps_single_quote(unc)
    driver_lit = ps_single_quote(driver_name or "")
    in_args = printui_argv(PRINTUI_IN, unc)
    dn_args = printui_argv(PRINTUI_DN, unc)
    in_joined = ",".join(ps_single_quote(a) for a in in_args[1:])
    dn_joined = ",".join(ps_single_quote(a) for a in dn_args[1:])
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        f"$unc = {unc_lit}\n"
        f"$driver = {driver_lit}\n"
        "function Test-DriverPresent {\n"
        "  try { $null = Get-PrinterDriver -Name $driver -ErrorAction Stop; return $true }\n"
        "  catch { return $false }\n"
        "}\n"
        "if (Test-DriverPresent) {\n"
        "  Write-Output '{\"ok\":true,\"prepared\":false,\"present\":true}'\n"
        "  exit 0\n"
        "}\n"
        f"$in = Start-Process -FilePath 'rundll32.exe' -ArgumentList @({in_joined})"
        " -Wait -PassThru -WindowStyle Hidden\n"
        "Start-Sleep -Seconds 2\n"
        "$present = Test-DriverPresent\n"
        f"$dn = Start-Process -FilePath 'rundll32.exe' -ArgumentList @({dn_joined})"
        " -Wait -PassThru -WindowStyle Hidden\n"
        "$payload = [ordered]@{\n"
        "  ok = [bool]$present\n"
        "  prepared = $true\n"
        "  present = [bool]$present\n"
        "  inExit = [int]$in.ExitCode\n"
        "  dnExit = [int]$dn.ExitCode\n"
        "}\n"
        "Write-Output ($payload | ConvertTo-Json -Compress)\n"
        "if (-not $present) { exit 1 }\n"
        "exit 0\n"
    )


def build_computer_connect_script(unc: str) -> str:
    args = printui_argv(PRINTUI_GA, unc)
    joined = ",".join(ps_single_quote(a) for a in args[1:])
    unc_lit = ps_single_quote(unc)
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        f"$unc = {unc_lit}\n"
        f"$p = Start-Process -FilePath 'rundll32.exe' -ArgumentList @({joined})"
        " -Wait -PassThru -WindowStyle Hidden\n"
        "$code = [int]$p.ExitCode\n"
        "$payload = [ordered]@{ ok = ($code -eq 0); exitCode = $code; unc = $unc }\n"
        "Write-Output ($payload | ConvertTo-Json -Compress)\n"
        "if ($code -ne 0) { exit $code }\n"
        "exit 0\n"
    )


def build_user_wrapper_script(
    *,
    unc: str,
    result_filename: str,
    set_default: bool = False,
) -> str:
    """Roda no token do usuário: ``/in`` (+ ``/y``) e grava JSON mínimo, sem senha."""
    in_args = printui_argv(PRINTUI_IN, unc)
    in_joined = ",".join(ps_single_quote(a) for a in in_args[1:])
    y_block = ""
    if set_default:
        y_args = printui_argv(PRINTUI_Y, unc)
        y_joined = ",".join(ps_single_quote(a) for a in y_args[1:])
        y_block = (
            f"$y = Start-Process -FilePath 'rundll32.exe' -ArgumentList @({y_joined})"
            " -Wait -PassThru -WindowStyle Hidden\n"
            "$yCode = [int]$y.ExitCode\n"
        )
    else:
        y_block = "$yCode = $null\n"
    unc_lit = ps_single_quote(unc)
    file_lit = ps_single_quote(result_filename)
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        f"$unc = {unc_lit}\n"
        f"$out = Join-Path $env:PUBLIC {file_lit}\n"
        "function Test-UserPrinter($name) {\n"
        "  try {\n"
        "    $p = Get-Printer -Name $name -ErrorAction SilentlyContinue\n"
        "    if ($p) { return $true }\n"
        "  } catch {}\n"
        "  try {\n"
        "    $hit = @(Get-Printer -ErrorAction SilentlyContinue |\n"
        "      Where-Object { $_.Name -eq $name })\n"
        "    if ($hit.Count -gt 0) { return $true }\n"
        "  } catch {}\n"
        "  return $false\n"
        "}\n"
        "try {\n"
        f"  $p = Start-Process -FilePath 'rundll32.exe' -ArgumentList @({in_joined})"
        " -Wait -PassThru -WindowStyle Hidden\n"
        "  $code = [int]$p.ExitCode\n"
        f"  {y_block}"
        "  $found = Test-UserPrinter $unc\n"
        "  $payload = [ordered]@{\n"
        "    ok = [bool]$found\n"
        "    exitCode = $code\n"
        "    defaultExit = $yCode\n"
        "    connected = [bool]$found\n"
        "  }\n"
        "  ($payload | ConvertTo-Json -Compress) | Set-Content -LiteralPath $out"
        " -Encoding UTF8\n"
        "} catch {\n"
        "  $payload = [ordered]@{ ok = $false; exitCode = -1;"
        " error = [string]$_.Exception.Message }\n"
        "  ($payload | ConvertTo-Json -Compress) | Set-Content -LiteralPath $out"
        " -Encoding UTF8\n"
        "}\n"
    )


def build_user_task_orchestrator_script(
    *,
    username: str,
    operation_id: str,
    wrapper_encoded: str,
    timeout_s: int = 90,
) -> str:
    """Registra tarefa InteractiveToken, espera, lê resultado e limpa sempre."""
    task = ps_single_quote(scheduled_task_name(operation_id))
    user = ps_single_quote(username)
    result = ps_single_quote(result_file_name(operation_id))
    encoded = (wrapper_encoded or "").strip()
    if any(ch in encoded for ch in "'\r\n"):
        raise ValueError("EncodedCommand inválido para a tarefa do usuário.")
    timeout = max(15, int(timeout_s))
    return (
        "$ErrorActionPreference = 'Continue'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        f"$taskName = {task}\n"
        f"$userId = {user}\n"
        f"$resultName = {result}\n"
        "$resultPath = Join-Path $env:PUBLIC $resultName\n"
        f"$wrapper = '{encoded}'\n"
        "$arg = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden"
        " -EncodedCommand ' + $wrapper\n"
        "$finished = $false\n"
        "try {\n"
        "  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false"
        " -ErrorAction SilentlyContinue | Out-Null\n"
        "  Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue\n"
        "  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg\n"
        "  $principal = $null\n"
        "  foreach ($logon in @('Interactive','InteractiveToken')) {\n"
        "    try {\n"
        "      $principal = New-ScheduledTaskPrincipal -UserId $userId"
        " -LogonType $logon -RunLevel Limited\n"
        "      break\n"
        "    } catch {}\n"
        "  }\n"
        "  if (-not $principal) { throw 'Falha ao criar principal InteractiveToken.' }\n"
        "  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries"
        f" -ExecutionTimeLimit (New-TimeSpan -Seconds {timeout})\n"
        "  try { $settings.Hidden = $true } catch {}\n"
        "  Register-ScheduledTask -TaskName $taskName -Action $action"
        " -Principal $principal -Settings $settings -Force | Out-Null\n"
        "  Start-ScheduledTask -TaskName $taskName\n"
        f"  $deadline = (Get-Date).AddSeconds({timeout})\n"
        "  do {\n"
        "    Start-Sleep -Milliseconds 400\n"
        "    $state = [string](Get-ScheduledTask -TaskName $taskName"
        " -ErrorAction SilentlyContinue).State\n"
        "  } while ($state -eq 'Running' -and (Get-Date) -lt $deadline)\n"
        "  if ($state -eq 'Running') {\n"
        "    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue\n"
        "    Write-Output '{\"ok\":false,\"error\":\"timeout\"}'\n"
        "    exit 2\n"
        "  }\n"
        "  if (Test-Path -LiteralPath $resultPath) {\n"
        "    Write-Output (Get-Content -LiteralPath $resultPath -Raw)\n"
        "    $finished = $true\n"
        "  } else {\n"
        "    Write-Output '{\"ok\":false,\"error\":\"no result file\"}'\n"
        "    exit 3\n"
        "  }\n"
        "} catch {\n"
        "  $msg = [string]$_.Exception.Message\n"
        "  Write-Output (([ordered]@{ ok = $false; error = $msg })"
        " | ConvertTo-Json -Compress)\n"
        "  exit 1\n"
        "} finally {\n"
        "  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false"
        " -ErrorAction SilentlyContinue | Out-Null\n"
        "  Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue\n"
        "}\n"
        "if ($finished) { exit 0 }\n"
    )


def build_artifact_cleanup_script(operation_id: str) -> str:
    task = ps_single_quote(scheduled_task_name(operation_id))
    result = ps_single_quote(result_file_name(operation_id))
    return (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"Unregister-ScheduledTask -TaskName {task} -Confirm:$false"
        " -ErrorAction SilentlyContinue | Out-Null\n"
        f"Remove-Item -LiteralPath (Join-Path $env:PUBLIC {result})"
        " -Force -ErrorAction SilentlyContinue\n"
        "Write-Output '{\"ok\":true}'\n"
    )


STATUS_NOT_SHARED = "Não compartilhada"
STATUS_SHARED = "Compartilhada"

FORBIDDEN_SECURITY_SNIPPETS = (
    "RestrictDriverInstallationToAdministrators",
    "net stop spooler",
    "net start spooler",
    "Restart-Service",
    "PointAndPrint",
    "EnableLUA",
)

_PSEXEC_INTERACTIVE_FLAGS = frozenset({"-i", "/i"})


def powershell_encoded_argv(
    encoded: str,
    *,
    execution_policy: str = "",
) -> List[str]:
    """Argv local/remoto do PowerShell silencioso via ``-EncodedCommand``."""
    blob = (encoded or "").strip()
    if not blob:
        raise ValueError("EncodedCommand vazio.")
    argv = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
    ]
    policy = (execution_policy or "").strip()
    if policy:
        argv.extend(["-ExecutionPolicy", policy])
    argv.extend(["-EncodedCommand", blob])
    return argv


def local_list_printers_argv(
    server: str = "",
    output_path: str = "",
) -> List[str]:
    """Consulta o servidor de impressão a partir desta máquina — sem PsExec."""
    path = (output_path or "").strip()
    if not path:
        raise ValueError("Caminho do JSON temporário vazio.")
    return powershell_encoded_argv(
        encode_powershell(build_list_printers_script(server, path)),
        execution_policy="Bypass",
    )


def elevated_psexec_flags(*, as_system: bool) -> List[str]:
    """Flags administrativas do PsExec. Nunca inclui ``-i``."""
    flags = ["-accepteula", "-nobanner"]
    if as_system:
        flags.append("-s")
    return flags


def reject_psexec_interactive_identity(flags: Sequence[str]) -> List[str]:
    """Garante que ``-i`` não seja usado como substituto do token do usuário."""
    out: List[str] = []
    for item in flags:
        token = str(item).strip()
        if token.lower() in _PSEXEC_INTERACTIVE_FLAGS:
            raise ValueError(
                "PsExec -i não executa no token do usuário conectado; "
                "use tarefa InteractiveToken."
            )
        out.append(token)
    return out


def parse_json_payload(text: str) -> dict:
    obj = extract_json_value(text)
    return obj if isinstance(obj, dict) else {}


def driver_is_present(payload: dict) -> bool:
    return _as_bool(payload.get("present"))


def payload_ok(payload: dict) -> bool:
    if "ok" in payload:
        return _as_bool(payload.get("ok"))
    if "connected" in payload:
        return _as_bool(payload.get("connected"))
    return False


def share_status_label(printer: NetworkPrinter) -> str:
    return STATUS_SHARED if printer.is_shared else STATUS_NOT_SHARED


def install_requires_active_user(scope: str) -> bool:
    return (scope or "").strip().lower() == SCOPE_USER


def can_install_printer(
    *,
    host_online: bool,
    printer: Optional[NetworkPrinter],
    busy: bool,
    scope: str,
    session: Optional[RemoteSession],
    server_configured: bool = True,
) -> bool:
    if not host_online or busy or not server_configured:
        return False
    if printer is None or not printer.is_shared:
        return False
    kind = (scope or "").strip().lower()
    if kind == SCOPE_ALL:
        return True
    if kind == SCOPE_USER:
        return is_session_active(session)
    return False


def install_block_reason(
    *,
    host_online: bool,
    printer: Optional[NetworkPrinter],
    busy: bool,
    scope: str,
    session: Optional[RemoteSession],
    server_configured: bool = True,
) -> str:
    if busy:
        return "Operação em andamento."
    if not host_online:
        return "Host remoto precisa estar Online."
    if not server_configured:
        return "Configure o servidor de impressão em Configurações."
    if printer is None:
        return "Selecione uma impressora."
    if not printer.is_shared:
        return "Não compartilhada"
    if install_requires_active_user(scope) and not is_session_active(session):
        return "Usuário não possui sessão interativa."
    if (scope or "").strip().lower() not in {SCOPE_USER, SCOPE_ALL}:
        return "Escopo inválido."
    return ""


def describe_printui_fields(
    *,
    scope: str,
    unc: str,
    driver_name: str = "",
    username: str = "",
    session_id: Optional[int] = None,
    set_default: bool = False,
    has_active_user: bool = False,
) -> List[tuple[str, str]]:
    """Campos do card de parâmetros — sem senha."""
    kind = (scope or "").strip().lower()
    rows: List[tuple[str, str]] = []
    if kind == SCOPE_ALL:
        rows.append(("Operação", "Instalação por computador"))
        rows.append(("Parâmetro", "/ga"))
    else:
        rows.append(("Operação", "Instalar conexão do usuário"))
        rows.append(("Parâmetro", "/in"))
    rows.append(("Silencioso", "/q"))
    if unc:
        rows.append(("Impressora", unc))
    if kind != SCOPE_ALL:
        if username:
            rows.append(("Usuário", username))
        if session_id is not None:
            rows.append(("Sessão", str(session_id)))
    if driver_name:
        rows.append(("Driver", driver_name))
    if effective_set_default(kind, set_default):
        rows.append(("Após instalação", "/y"))
    if kind == SCOPE_ALL and has_active_user:
        rows.append(("Sessão ativa", "também aplica /in no token do usuário"))
    return rows


def logical_printui_preview(
    *,
    scope: str,
    unc: str,
    set_default: bool = False,
) -> str:
    """Comando lógico do PrintUIEntry (não inclui PsExec nem senha)."""
    if not (unc or "").strip():
        return ""
    kind = (scope or "").strip().lower()
    lines = []
    if kind == SCOPE_ALL:
        lines.append(printui_command_line(PRINTUI_GA, unc))
    else:
        lines.append(printui_command_line(PRINTUI_IN, unc))
        if effective_set_default(kind, set_default):
            lines.append(printui_command_line(PRINTUI_Y, unc))
    return "\n".join(lines)


def collected_printer_scripts() -> List[str]:
    """Scripts gerados — usados para auditoria de segurança nos testes."""
    unc = printer_unc("HP_FIN_03", "printserver")
    wrapper = build_user_wrapper_script(
        unc=unc, result_filename=result_file_name("abc"), set_default=True
    )
    return [
        build_list_printers_script(
            "printserver",
            output_path=r"C:\Temp\RemoteOps_Printers_abc.json",
        ),
        build_driver_query_script("HP Universal Printing PCL 6"),
        build_driver_prepare_script(unc=unc, driver_name="HP Universal Printing PCL 6"),
        build_computer_connect_script(unc),
        wrapper,
        build_user_task_orchestrator_script(
            username=r"TCE\joao.silva",
            operation_id="abc",
            wrapper_encoded=encode_powershell(wrapper),
        ),
        build_artifact_cleanup_script("abc"),
    ]
