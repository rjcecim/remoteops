"""Diagnóstico: quando uma execução remota falha, grava os buffers em disco
para facilitar a análise (``%TEMP%\\WingetRM_last_psexec.log``)."""

from __future__ import annotations

import os
from pathlib import Path


def save_last_psexec_log(
    *,
    action: str,
    host: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    exit_resolution_message: str | None = None,
    exit_resolution_source: str | None = None,
) -> Path | None:
    """Grava stdout/stderr do PsExec no ``%TEMP%``. Retorna o caminho ou ``None``."""
    try:
        temp = Path(os.environ.get("TEMP", str(Path.home())))
        target = temp / "WingetRM_last_psexec.log"
        res_block = ""
        if exit_resolution_message is not None and exit_resolution_source is not None:
            res_block = (
                f"--- exit resolution (source={exit_resolution_source}) ---\n"
                f"{exit_resolution_message}\n"
            )
        target.write_text(
            f"--- action={action} host={host} exit={exit_code} ---\n"
            f"{res_block}"
            "--- STDOUT ---\n"
            f"{stdout or ''}\n"
            "--- STDERR ---\n"
            f"{stderr or ''}\n",
            encoding="utf-8",
            errors="replace",
        )
        return target
    except Exception:
        return None
