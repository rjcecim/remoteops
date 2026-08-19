"""Montagem da linha de comando do PsExec e mensagens de erro amigáveis."""

from __future__ import annotations

import shutil


def _resolve_psexec_exe(psexec_path: str) -> str:
    p = (psexec_path or "").strip()
    if p:
        return p
    for name in ("PsExec.exe", "PsExec", "psexec"):
        found = shutil.which(name)
        if found:
            return found
    return "PsExec.exe"


def build_psexec_args(
    *,
    psexec_path: str,
    host: str,
    username: str,
    password: str,
    ps_command: str,
) -> list[str]:
    """Retorna a lista de argumentos para invocar o PsExec com PowerShell remoto."""
    exe = _resolve_psexec_exe(psexec_path)
    target = f"\\\\{host.strip()}"

    args: list[str] = [
        exe,
        target,
        "-accepteula",
        "-nobanner",
        "-s",
    ]

    if username.strip():
        args += ["-u", username.strip()]
        if password.strip():
            args += ["-p", password]

    args += [
        "powershell",
        "-OutputFormat",
        "Text",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps_command,
    ]
    return args


def psexec_hint(
    exit_code: int,
    stderr: str,
    *,
    system_message: str | None = None,
    system_source: str | None = None,
) -> str:
    """Converte um código de saída do PsExec em uma única frase para o log."""
    del system_source  # reservado para chamadas existentes; a frase já é autocontida
    s = (stderr or "").strip().lower()

    if (
        "not a valid win32 application" in s
        or ("aplicativo" in s and "win32" in s and ("válido" in s or "valido" in s or "vï¿½lido" in s))
    ):
        return "PsExec: serviço remoto incompatível (PSEXESVC / arquitetura ou antivírus)."
    if exit_code == 6:
        return "PsExec não acessou o host (exit=6: identificador inválido — máquina offline, DNS, SMB/RPC ou ADMIN$)."
    if exit_code == 1326:
        return "PsExec: falha de autenticação (exit=1326: usuário/senha incorretos)."
    if exit_code == 1385:
        return "PsExec: logon remoto não permitido (exit=1385)."
    if system_message:
        return f"PsExec falhou (exit={exit_code}: {system_message})."
    return f"PsExec falhou (exit={exit_code})."
