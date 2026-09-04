from __future__ import annotations

import base64
import json
import subprocess
import uuid
from typing import Any, List, Optional, Sequence, Tuple

from remoteops.core.console_codec import decode_best_effort
from remoteops.core.powershell_options import encode_powershell_command
from remoteops.core.win_cmd import run_captured
from remoteops.services.ops import CredentialContext, build_psexec_argv, resolve_psexec_exe
from remoteops.utils.pstools import get_pstools_dir
from remoteops.winget.result_file import build_remote_paths, read_remote_result_file

DEFAULT_REMOTE_PS_TIMEOUT = 90.0

_PSEXEC_EXTRA_FLAGS = ["-accepteula", "-nobanner", "-h", "-s"]
_B64_MARK = "__REMOTEOPS_B64__"

# Sem console (PyInstaller windowed) o PowerShell 5 mistura progresso/encoding no stdout.
_PS_STDOUT_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    "$OutputEncoding = [Console]::OutputEncoding\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
)


def wrap_remote_ps_script(script: str, result_path: str = "") -> str:
    """Garante UTF-8, silencia progresso e, se houver caminho, grava JSON em arquivo."""
    body = (script or "").strip()
    text = _PS_STDOUT_PREAMBLE + body
    path = (result_path or "").strip()
    if not path:
        return text
    lit = "'" + path.replace("'", "''") + "'"
    return (
        _PS_STDOUT_PREAMBLE
        + f"$__roFile = {lit}\n"
        "$__roBuf = New-Object System.IO.StringWriter\n"
        "$__roPrev = $null\n"
        "try { $__roPrev = [Console]::Out; [Console]::SetOut($__roBuf) } catch {}\n"
        "$__roPipe = $null\n"
        "try {\n"
        "  $__roPipe = . {\n"
        f"{body}\n"
        "  }\n"
        "} finally {\n"
        "  try { if ($null -ne $__roPrev) { [Console]::SetOut($__roPrev) } } catch {}\n"
        "}\n"
        "$__roFromPipe = ''\n"
        "if ($null -ne $__roPipe) {\n"
        "  $__roFromPipe = (($__roPipe | ForEach-Object { [string]$_ })"
        " -join [Environment]::NewLine).Trim()\n"
        "}\n"
        "$__roFromConsole = $__roBuf.ToString().Trim()\n"
        "$__roText = $__roFromPipe\n"
        "if (-not $__roText) { $__roText = $__roFromConsole }\n"
        "if ($__roText) {\n"
        "  try {\n"
        "    [System.IO.File]::WriteAllText("
        "$__roFile, $__roText, [System.Text.UTF8Encoding]::new($false))\n"
        "  } catch {}\n"
        f"  Write-Output ('{_B64_MARK}' + "
        "[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($__roText)))\n"
        "}\n"
    )


def build_remote_powershell_argv(
    host: str,
    script: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
    extra_flags: Optional[Sequence[str]] = None,
    result_path: str = "",
) -> List[str]:
    """Monta argv PsExec → powershell.exe -EncodedCommand (execução local no remoto).

    ``-EncodedCommand`` evita que aspas do script (ex.: ``-Filter "…"``) sejam
    destruídas pelo PsExec quando o app roda como .exe sem console.
    """
    psexec = resolve_psexec_exe(pstools_dir or get_pstools_dir())
    encoded = encode_powershell_command(
        wrap_remote_ps_script(script, result_path=result_path)
    )
    remote_argv = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    creds = CredentialContext(user=user or "", password=password or "")
    return build_psexec_argv(
        psexec_exe=psexec,
        host=host,
        remote_argv=remote_argv,
        creds=creds,
        extra_flags=list(extra_flags or _PSEXEC_EXTRA_FLAGS),
        include_password=True,
    )


def run_remote_powershell(
    host: str,
    script: str,
    *,
    user: str = "",
    password: str = "",
    timeout: float = DEFAULT_REMOTE_PS_TIMEOUT,
    pstools_dir: str = "",
) -> Tuple[Optional[Any], str]:
    """
    Executa PowerShell no host remoto via PsExec e tenta interpretar stdout como JSON.

    No .exe sem console o stdout do PsExec corta JSON longo. O script grava o
    resultado em arquivo (ADMIN$/C$) e também emite Base64 ASCII.

    Retorna (dados, erro). ``dados`` é dict/list ou None.
    """
    h = (host or "").strip().strip("\\")
    if not h:
        return None, "Host inválido."

    run_id = "RemoteOps_" + uuid.uuid4().hex[:16]
    artifacts = build_remote_paths(h, run_id)
    argv = build_remote_powershell_argv(
        h,
        script,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
        result_path=artifacts.json_path,
    )
    try:
        proc = run_captured(argv, timeout=max(5.0, float(timeout)))
    except subprocess.TimeoutExpired:
        return None, f"Consulta excedeu {int(timeout)}s."
    except FileNotFoundError:
        return None, "PsExec não encontrado na pasta PSTools configurada."
    except OSError as exc:
        return None, f"Falha ao iniciar PsExec: {exc}"

    out = decode_best_effort(proc.stdout or b"").strip()
    err = decode_best_effort(proc.stderr or b"").strip()
    file_json = read_remote_result_file(
        artifacts.json_admin,
        artifacts.json_c,
        attempts=8,
        sleep_s=0.15,
    )
    raw = (file_json or "").strip() or extract_b64_json(out) or out

    if not raw:
        if proc.returncode != 0:
            return None, _shorten_ps_error(err or f"PowerShell remoto falhou (exit {proc.returncode}).")
        return None, _shorten_ps_error(err or "Resposta vazia do PowerShell remoto.")

    data, parse_err = parse_json_output(raw)
    if parse_err:
        if proc.returncode != 0:
            return None, _shorten_ps_error(err or parse_err)
        return None, parse_err
    if proc.returncode != 0 and data is None:
        return None, _shorten_ps_error(err or f"PowerShell remoto falhou (exit {proc.returncode}).")
    return data, err.strip()


def extract_b64_json(text: str) -> str:
    """Extrai JSON UTF-8 de um marcador ASCII ``__REMOTEOPS_B64__``."""
    raw = text or ""
    idx = raw.find(_B64_MARK)
    if idx < 0:
        return ""
    blob = raw[idx + len(_B64_MARK) :].strip().splitlines()[0].strip()
    if not blob:
        return ""
    try:
        return base64.b64decode(blob).decode("utf-8")
    except Exception:
        return ""


def parse_json_output(text: str) -> Tuple[Optional[Any], str]:
    """Tenta parsear JSON; tolera BOM, NULs, lixo e vários objetos concatenados."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return None, "Resposta vazia."
    marked = extract_b64_json(raw)
    if marked:
        raw = marked.lstrip("\ufeff").strip()
    if "\x00" in raw:
        raw = raw.replace("\x00", "").strip()
        if not raw:
            return None, "Resposta vazia."
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass
    values = _decode_all_json_values(raw)
    if not values:
        return None, _non_json_error(raw)
    if len(values) == 1:
        return values[0], ""
    merged: List[Any] = []
    for value in values:
        if isinstance(value, list):
            merged.extend(value)
        else:
            merged.append(value)
    return merged, ""


def _decode_all_json_values(raw: str) -> List[Any]:
    """Extrai todos os valores JSON raiz (array, objeto ou vários objetos seguidos)."""
    decoder = json.JSONDecoder()
    values: List[Any] = []
    idx = 0
    n = len(raw)
    while idx < n:
        while idx < n and raw[idx] not in "{[":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            idx += 1
            continue
        values.append(obj)
        idx += max(end, 1)
    return values


def _non_json_error(raw: str) -> str:
    preview = " ".join((raw or "").split())
    if not preview:
        return "Resposta não é JSON válido."
    return f"Resposta não é JSON válido: {preview[:180]}"


def _shorten_ps_error(err: str) -> str:
    t = (err or "").strip()
    if not t:
        return "Não foi possível consultar."
    low = t.lower()
    if "access is denied" in low or "acesso negado" in low:
        return "Acesso negado. Verifique usuário/senha na aba PsExec."
    if "rpc server is unavailable" in low or "servidor rpc" in low:
        return "Host inacessível (RPC indisponível)."
    if "timed out" in low or "timeout" in low:
        return "Tempo esgotado na consulta remota."
    for line in t.splitlines():
        s = line.strip()
        if s and not s.startswith("+") and not s.startswith("at "):
            return s[:280]
    return t[:280]
