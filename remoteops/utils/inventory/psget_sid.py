from __future__ import annotations

import os
import re
import subprocess
from typing import List, Optional, Tuple

from remoteops.core.console_codec import decode_best_effort
from remoteops.core.win_cmd import run_captured
from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool

PSGETSID_NAMES: Tuple[str, ...] = ("PsGetSid64.exe", "PsGetSid.exe")
PSGETSID_TIMEOUT_SECONDS = 60.0

_SID_RE = re.compile(r"(S-1-[\d-]+)", re.IGNORECASE)
_USAGE_RE = re.compile(r"usage:\s*psgetsid", re.IGNORECASE)
_EULA_RE = re.compile(r"sysinternals\s+software\s+license\s+terms", re.IGNORECASE)
_ACCOUNT_LINE_RE = re.compile(
    r"(?i)^\s*(?:account|sid)\s+for\s+(.+?)(?:\s+is)?\s*:?\s*$"
)


def resolve_psgetsid_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSGETSID_NAMES)


def psgetsid_available(pstools_dir: str = "") -> bool:
    path = resolve_psgetsid_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def build_psgetsid_argv(
    exe: str,
    host: str,
    *,
    account: str = "",
    user: str = "",
    password: str = "",
    nobanner: bool = True,
    accepteula: bool = True,
) -> List[str]:
    """
    Monta argv do PsGetSid.

    Sem conta: SID da conta conectada no remoto (via \\\\host).
    Com conta: PsGetSid \\\\host account
    Credenciais -u/-p após o alvo (padrão PsTools).
    """
    h = (host or "").strip().strip("\\")
    if not exe or not h:
        return []
    args: List[str] = [exe]
    if accepteula:
        args.append("-accepteula")
    if nobanner:
        args.append("-nobanner")
    args.append(f"\\\\{h}")
    acct = (account or "").strip()
    if acct:
        args.append(acct)
    u = (user or "").strip()
    if u:
        args.extend(["-u", u])
        if (password or "").strip():
            args.extend(["-p", password])
    return args


def is_psgetsid_usage_text(text: str) -> bool:
    return bool(_USAGE_RE.search(text or ""))


def is_psgetsid_eula_text(text: str) -> bool:
    """True se a saída for a tela de EULA (falta -accepteula ou 1ª execução)."""
    return bool(_EULA_RE.search(text or ""))


def parse_psgetsid_output(text: str) -> Tuple[str, str]:
    """
    Extrai (nome/conta, SID) da saída do PsGetSid.

    Exemplo::
        PsGetSid v1.45 ...
        Account for TCEPA\\usuario is:
        S-1-5-21-...
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    sid = ""
    m = _SID_RE.search(raw)
    if m:
        sid = m.group(1)
    account = ""
    for line in raw.splitlines():
        am = _ACCOUNT_LINE_RE.match(line.strip())
        if not am:
            continue
        account = am.group(1).strip().rstrip(":").strip()
        if account.lower().endswith(" is"):
            account = account[:-3].strip()
        break
    return account, sid


def run_psgetsid(
    host: str,
    *,
    account: str = "",
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
    timeout: float = PSGETSID_TIMEOUT_SECONDS,
) -> Tuple[str, str, str]:
    """
    Executa PsGetSid no host remoto.

    Retorna (conta, sid, erro). Erro vazio = sucesso parcial/total.
    """
    exe = resolve_psgetsid_exe(pstools_dir)
    if not exe or not os.path.isfile(exe):
        return "", "", "PsGetSid não encontrado na pasta PSTools."

    argv = build_psgetsid_argv(
        exe,
        host,
        account=account,
        user=user,
        password=password,
    )
    if not argv:
        return "", "", "Host inválido."

    try:
        proc = run_captured(argv, timeout=max(5.0, float(timeout)))
    except subprocess.TimeoutExpired:
        return "", "", f"PsGetSid excedeu {int(timeout)}s."
    except FileNotFoundError:
        return "", "", f"PsGetSid não encontrado: {exe}"
    except OSError as exc:
        return "", "", f"Falha ao iniciar PsGetSid: {exc}"

    out = decode_best_effort(proc.stdout or b"").strip()
    err = decode_best_effort(proc.stderr or b"").strip()
    combined = out if out else err

    if is_psgetsid_usage_text(combined):
        return "", "", "PsGetSid rejeitou o comando (Usage)."

    if is_psgetsid_eula_text(combined):
        return (
            "",
            "",
            "PsGetSid exibiu o EULA da Sysinternals em vez do SID. "
            "Verifique se PsGetSid64.exe está na pasta PSTools e tente novamente.",
        )

    account_parsed, sid = parse_psgetsid_output(combined)
    if sid:
        return account_parsed, sid, ""

    if proc.returncode != 0:
        msg = err or out or f"PsGetSid falhou (exit {proc.returncode})."
        return "", "", msg[:280]
    return account_parsed, sid, "SID não encontrado na resposta."
