"""Constantes compartilhadas pelo backend de execução remota."""

from __future__ import annotations

import re
import subprocess

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PSEXEC_ACTION_TIMEOUT_S = 30 * 60  # 30 minutos
REMOTE_CANCEL_GRACE_S = 12.0
# CreateProcessW: lpCommandLine máximo é 32767 caracteres. Folga para quoting.
CREATEPROCESS_CMDLINE_MAX = 32000

EXEC_ACTIONS = {"install", "upgrade", "upgrade_all", "uninstall"}
MULTI_ITEM_EXEC_ACTIONS = {"install", "upgrade", "uninstall"}


def result_exit_code(value: object, *, if_missing: int = 1) -> int:
    """Converte ``ExitCode`` de um resultado (dict). ``0`` conta como sucesso — não usar ``value or 1``."""
    if value is None:
        return if_missing
    try:
        code = int(value)
    except (TypeError, ValueError):
        return if_missing
    # JSON/unsigned às vezes traz HRESULT winget como uint32 positivo.
    if code > 0x7FFFFFFF:
        code -= 0x100000000
    return code


# Soft-success do winget (AppInstallerErrors.h) — operação útil concluiu.
WINGET_SOFT_SUCCESS_EXIT_CODES = frozenset(
    {
        -1978334967,  # REBOOT_REQUIRED_TO_FINISH
        -1978334965,  # REBOOT_INITIATED
        -1978335189,  # UPDATE_NOT_APPLICABLE
    }
)


def is_winget_success_exit(value: object, *, if_missing: int = 1) -> bool:
    """True quando o exit code do winget indica sucesso (inclui soft-success)."""
    code = result_exit_code(value, if_missing=if_missing)
    return code == 0 or code in WINGET_SOFT_SUCCESS_EXIT_CODES

MARKER_B64_BEGIN = "__WINGETRM_B64_BEGIN__"
MARKER_B64_END = "__WINGETRM_B64_END__"
MARKER_JSON_BEGIN = "__WINGETRM_JSON_BEGIN__"
MARKER_JSON_END = "__WINGETRM_JSON_END__"
MARKER_LOG_PREFIX = "__WINGETRM_LOG__"
MARKER_PCT_PREFIX = "__WINGETRM_PCT__"
REALTIME_LOG_PREFIX = "__WINGETRM_RT__"

SPINNER_LINES = {"-", "\\", "|", "/"}

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
MARKER_LINE_RE = re.compile(r"^__WINGETRM_(B64_BEGIN|B64_END|JSON_BEGIN|JSON_END|DBG|FILE|PCT)__.*$")
MARKER_LOG_RE = re.compile(r"^__WINGETRM_LOG__(?P<payload>.*)$")
MARKER_PCT_RE = re.compile(r"^__WINGETRM_PCT__(?P<pct>\d{1,3})\s*$")
B64_CHUNK_RE = re.compile(r"^[A-Za-z0-9+/=]{16,}$")
JSON_LINE_RE = re.compile(r"^\s*\{.*\}\s*$")

PROGRESS_RE = re.compile(
    r"^(?:.*?[\s█▒░▓]*)?\s*(?P<cur>[0-9]+(?:[\\.,][0-9]+)?)\s*(?P<cur_u>KB|MB|GB)\s*/\s*(?P<tot>[0-9]+(?:[\\.,][0-9]+)?)\s*(?P<tot_u>KB|MB|GB)\s*$",
    re.IGNORECASE,
)
PROGRESS_PCT_RE = re.compile(r"^(?:.*?[\s█▒░▓]*)?\s*(?P<pct>\d{1,3})%\s*$", re.IGNORECASE)
