"""Texto de pré-visualização do comando PsExec + winget."""

from __future__ import annotations

from remoteops.winget.winget_flags import (
    COMMON_EXEC_FLAGS,
    COMMON_QUERY_FLAGS,
    COMMON_UNINSTALL_FLAGS,
    COMMON_UPGRADE_ALL_FLAGS,
    flags_to_cli,
)


def _ps_quote_single(value: str) -> str:
    return "'" + (value or "").replace("'", "''") + "'"


def _ps_one_liner_for_ids(*, verb: str, ids: list[str], flags: str) -> str:
    ps_ids = ",".join(_ps_quote_single(x) for x in ids)
    return (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "
        + _ps_quote_single(f"$ids=@({ps_ids}); foreach($id in $ids){{ winget {verb} --id $id {flags} }}")
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
    exe = (psexec_path or "").strip() or "PsExec.exe"
    host = (host or "").strip()
    user = (username or "").strip()
    pw = (password or "").strip()

    q_flags = flags_to_cli(COMMON_QUERY_FLAGS)
    e_flags = flags_to_cli(COMMON_EXEC_FLAGS)
    a_flags = flags_to_cli(COMMON_UPGRADE_ALL_FLAGS)
    u_flags = flags_to_cli(COMMON_UNINSTALL_FLAGS)

    a = (action or "").lower()
    if a == "list":
        winget_cmd = f"winget upgrade {q_flags}"
    elif a == "search":
        q = (query or "").strip() or "<termo>"
        winget_cmd = f'winget search "{q}" {q_flags}'
    elif a == "installed":
        winget_cmd = f"winget list {q_flags}"
    elif a == "upgrade_all":
        winget_cmd = f"winget upgrade --all {a_flags}"
    elif a == "upgrade":
        if not ids:
            winget_cmd = f"winget upgrade --id <ID> ... {e_flags}"
        elif len(ids) == 1:
            winget_cmd = f"winget upgrade --id {ids[0]} {e_flags}"
        else:
            winget_cmd = _ps_one_liner_for_ids(verb="upgrade", ids=ids, flags=e_flags)
    elif a == "install":
        if not ids:
            winget_cmd = f"winget install --id <ID> ... {e_flags}"
        elif len(ids) == 1:
            winget_cmd = f"winget install --id {ids[0]} {e_flags}"
        else:
            winget_cmd = _ps_one_liner_for_ids(verb="install", ids=ids, flags=e_flags)
    elif a == "uninstall":
        if not ids:
            winget_cmd = f"winget uninstall --id <ID> ... {u_flags}"
        elif len(ids) == 1:
            winget_cmd = f"winget uninstall --id {ids[0]} {u_flags}"
        else:
            winget_cmd = _ps_one_liner_for_ids(verb="uninstall", ids=ids, flags=u_flags)
    else:
        winget_cmd = f"(ação) {action}"

    psexec_parts = [f'"{exe}"', f"\\\\{host}", "-accepteula", "-nobanner", "-r", "WINGETRM<auto>", "-h"]
    if user:
        psexec_parts += ["-u", f'"{user}"']
        if pw:
            psexec_parts += ["-p", '"********"']
    else:
        psexec_parts += ["-s"]
    psexec_line = " ".join(psexec_parts) + " powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand <...>"

    return "PsExec:\n" + psexec_line + "\n\nWinget remoto:\n" + winget_cmd
