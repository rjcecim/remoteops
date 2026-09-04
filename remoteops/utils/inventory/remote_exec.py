from __future__ import annotations

import json
import subprocess
from typing import Any, List, Optional, Sequence, Tuple

from remoteops.core.console_codec import decode_best_effort
from remoteops.core.powershell_options import encode_powershell_command
from remoteops.core.win_cmd import run_captured
from remoteops.services.ops import CredentialContext, build_psexec_argv, resolve_psexec_exe
from remoteops.utils.pstools import get_pstools_dir

DEFAULT_REMOTE_PS_TIMEOUT = 90.0

_PSEXEC_EXTRA_FLAGS = ["-accepteula", "-nobanner", "-h", "-s"]

# Sem console (PyInstaller windowed) o PowerShell 5 mistura progresso/encoding no stdout.
_PS_STDOUT_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    "$OutputEncoding = [Console]::OutputEncoding\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
)


def wrap_remote_ps_script(script: str) -> str:
    """Garante UTF-8 estável e silencia progresso que polui o JSON no .exe."""
    return _PS_STDOUT_PREAMBLE + (script or "").strip()


def build_remote_powershell_argv(
    host: str,
    script: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
    extra_flags: Optional[Sequence[str]] = None,
) -> List[str]:
    """Monta argv PsExec → powershell.exe -EncodedCommand (execução local no remoto).

    ``-EncodedCommand`` evita que aspas do script (ex.: ``-Filter "…"``) sejam
    destruídas pelo PsExec quando o app roda como .exe sem console.
    """
    psexec = resolve_psexec_exe(pstools_dir or get_pstools_dir())
    encoded = encode_powershell_command(wrap_remote_ps_script(script))
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

    Retorna (dados, erro). ``dados`` é dict/list ou None.
    """
    h = (host or "").strip().strip("\\")
    if not h:
        return None, "Host inválido."

    argv = build_remote_powershell_argv(
        h,
        script,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
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

    if not out:
        if proc.returncode != 0:
            return None, _shorten_ps_error(err or f"PowerShell remoto falhou (exit {proc.returncode}).")
        return None, _shorten_ps_error(err or "Resposta vazia do PowerShell remoto.")

    data, parse_err = parse_json_output(out)
    if parse_err:
        if proc.returncode != 0:
            return None, _shorten_ps_error(err or parse_err)
        return None, parse_err
    if proc.returncode != 0 and data is None:
        return None, _shorten_ps_error(err or f"PowerShell remoto falhou (exit {proc.returncode}).")
    return data, err.strip()


def parse_json_output(text: str) -> Tuple[Optional[Any], str]:
    """Tenta parsear JSON; tolera BOM, NULs (UTF-16 mal decodificado) e lixo ao redor."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        return None, "Resposta vazia."
    if "\x00" in raw:
        raw = raw.replace("\x00", "").strip()
        if not raw:
            return None, "Resposta vazia."
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(raw):
        if ch not in "{[":
            continue
        try:
            obj, _end = decoder.raw_decode(raw[i:])
            return obj, ""
        except json.JSONDecodeError:
            continue
    return None, _non_json_error(raw)


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
