"""Texto de pré-visualização do comando PsExec + winget."""

from __future__ import annotations

import subprocess

from remoteops.utils.redaction import redact_argv
from remoteops.winget.psexec_args import build_psexec_args
from remoteops.winget.winget_flags import (
    COMMON_EXEC_FLAGS,
    COMMON_QUERY_FLAGS,
    COMMON_UNINSTALL_FLAGS,
    COMMON_UPGRADE_ALL_FLAGS,
    SEARCH_QUERY_FLAGS,
    UPGRADE_QUERY_FLAGS,
    flags_to_cli,
    unique_valid_ids,
)

# O -Command real é um bootstrap gzip gerado na execução (caminhos únicos).
_PS_COMMAND_PLACEHOLDER = "<...>"


def _ps_quote_single(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def _upgrade_foreach_script(ids: list[str], flags: str) -> str:
    """Script no PowerShell já iniciado pelo PsExec — sem powershell.exe aninhado."""
    ps_ids = ",".join(_ps_quote_single(x) for x in unique_valid_ids(ids))
    return (
        f"$ids=@({ps_ids}); foreach($id in $ids){{ "
        f"winget upgrade --id $id {flags} "
        f"}}"
    )


def _install_foreach_script(ids: list[str], flags: str) -> str:
    """Script no PowerShell já iniciado pelo PsExec — sem powershell.exe aninhado."""
    ps_ids = ",".join(_ps_quote_single(x) for x in unique_valid_ids(ids))
    return (
        f"$ids=@({ps_ids}); foreach($id in $ids){{ "
        f"winget install --id $id {flags} "
        f"}}"
    )


def _uninstall_foreach_script(ids: list[str], flags: str) -> str:
    """Script no PowerShell já iniciado pelo PsExec — sem powershell.exe aninhado."""
    ps_ids = ",".join(_ps_quote_single(x) for x in unique_valid_ids(ids))
    return (
        f"$ids=@({ps_ids}); foreach($id in $ids){{ "
        f"winget uninstall --id $id {flags} "
        f"}}"
    )


def build_preview_text(
    *,
    psexec_path: str,
    host: str,
    username: str,
    password: str,
    action: str,
    ids: list[str],
    query: str,
) -> str:
    q_flags = flags_to_cli(COMMON_QUERY_FLAGS)
    upgrade_q_flags = flags_to_cli(UPGRADE_QUERY_FLAGS)
    search_q_flags = flags_to_cli(SEARCH_QUERY_FLAGS)
    e_flags = flags_to_cli(COMMON_EXEC_FLAGS)
    a_flags = flags_to_cli(COMMON_UPGRADE_ALL_FLAGS)
    u_flags = flags_to_cli(COMMON_UNINSTALL_FLAGS)

    a = (action or "").lower()
    if a == "list":
        winget_cmd = f"winget upgrade {upgrade_q_flags}"
    elif a == "search":
        q = (query or "").strip() or "<termo>"
        winget_cmd = f'winget search "{q}" {search_q_flags}'
    elif a == "installed":
        winget_cmd = f"winget list {q_flags}"
    elif a == "upgrade_all":
        winget_cmd = f"winget upgrade --all {a_flags}"
    elif a == "upgrade":
        upgrade_ids = unique_valid_ids(ids)
        if not upgrade_ids:
            winget_cmd = f"winget upgrade --id <ID> {e_flags}"
        elif len(upgrade_ids) == 1:
            winget_cmd = f"winget upgrade --id {upgrade_ids[0]} {e_flags}"
        else:
            winget_cmd = _upgrade_foreach_script(upgrade_ids, e_flags)
    elif a == "install":
        install_ids = unique_valid_ids(ids)
        if not install_ids:
            winget_cmd = f"winget install --id <ID> {e_flags}"
        elif len(install_ids) == 1:
            winget_cmd = f"winget install --id {install_ids[0]} {e_flags}"
        else:
            winget_cmd = _install_foreach_script(install_ids, e_flags)
    elif a == "uninstall":
        uninstall_ids = unique_valid_ids(ids)
        if not uninstall_ids:
            winget_cmd = f"winget uninstall --id <ID> {u_flags}"
        elif len(uninstall_ids) == 1:
            winget_cmd = f"winget uninstall --id {uninstall_ids[0]} {u_flags}"
        else:
            winget_cmd = _uninstall_foreach_script(uninstall_ids, u_flags)
    else:
        winget_cmd = f"(ação) {action}"

    argv = build_psexec_args(
        psexec_path=psexec_path,
        host=host,
        username=username,
        password=password,
        ps_command=_PS_COMMAND_PLACEHOLDER,
    )
    psexec_line = subprocess.list2cmdline(redact_argv(argv))

    return "PsExec:\n" + psexec_line + "\n\nWinget remoto:\n" + winget_cmd
