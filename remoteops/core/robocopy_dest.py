"""Destino Robocopy: normalização, validação e caminhos canônicos (C$ / C:\\).

Uma única política para UI, CommandBuilder e lote MSI.
O destino canônico é relativo ao C: remoto (ex.: ``temp\\scripts``).
"""

from __future__ import annotations

import os
import re
from typing import Optional, Sequence

from remoteops.core.win_cmdline import split_extra_args
from remoteops.utils.ping import format_unc_host

# Switches quiet padrão (maiúsculas).
DEFAULT_ROBOCOPY_SWITCHES = "/NFL /NDL /NJH /NJS /NC /NS /NP"
DEFAULT_RETRIES = 2
DEFAULT_WAIT_SECONDS = 3
ALLOWED_RETRIES: tuple[int, ...] = (0, 1, 2, 3, 5)
ALLOWED_WAIT_SECONDS: tuple[int, ...] = (0, 1, 3, 5, 10)

_INVALID_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def normalize_robocopy_dest(dest: str) -> str:
    """Converte entrada do usuário em caminho relativo sob C: (sem validar)."""
    text = (dest or "").strip().replace('"', "").replace("'", "").strip()
    text = text.replace("/", "\\")
    lower = text.lower()
    if lower.startswith("\\\\"):
        return text  # UNC bruto — validação rejeita
    if lower.startswith("c:"):
        text = text[2:].lstrip("\\")
    # Remove segmentos vazios; NÃO remove ".." aqui — a validação rejeita.
    parts = [p for p in text.split("\\") if p != ""]
    return "\\".join(parts)


def validate_robocopy_dest(dest: str) -> tuple[str, str]:
    """
    Valida e normaliza o destino.

    Retorna ``(canonical, error)``. Se ``error`` for não-vazio, ``canonical``
    pode ser parcial e não deve ser usado para UNC/execução.
    """
    raw = (dest or "").strip()
    if not raw:
        return "", "Informe a pasta de destino no C: remoto (ex.: C:\\temp)."

    lowered = raw.replace("/", "\\").strip().lower()
    if lowered.startswith("\\\\"):
        return "", "Não use caminho UNC como destino. Informe um caminho no C: (ex.: C:\\temp)."
    if re.match(r"^[a-z]:", lowered) and not lowered.startswith("c:"):
        return "", "Somente a unidade C: do host remoto é suportada."
    if "c$" in lowered.replace("/", "\\").split("\\"):
        return "", "Não use C$ no destino. Informe o caminho local (ex.: C:\\temp)."

    canonical = normalize_robocopy_dest(raw)
    if not canonical:
        return "", "Informe a pasta de destino no C: remoto (ex.: C:\\temp)."

    parts = canonical.split("\\")
    for part in parts:
        if part in (".", ".."):
            return "", "O destino não pode conter '.' ou '..'."
        if _INVALID_CHARS_RE.search(part):
            return "", f"Caractere inválido no segmento '{part}'."
        # Nome reservado: CON, CON.txt, etc.
        base = part.split(".")[0].upper()
        if base in _RESERVED_NAMES:
            return "", f"Nome reservado do Windows não permitido: {part}."
        if part.endswith(" ") or part.endswith("."):
            return "", f"Segmento inválido no destino: '{part}'."

    # Caminho absoluto residual (ex.: após normalize malformado)
    if re.match(r"^[a-zA-Z]:", canonical):
        return "", "Destino inválido após normalização."

    if len(canonical) > 200:
        return "", "Caminho de destino excessivamente longo."

    return canonical, ""


def remote_local_path(canonical_dest: str, *relative_parts: str) -> str:
    """``C:\\temp\\file.msi`` a partir do canônico relativo."""
    bits = [canonical_dest, *[p for p in relative_parts if p]]
    joined = "\\".join(
        "\\".join(str(b).replace("/", "\\").split("\\")).strip("\\")
        for b in bits
        if b
    )
    return f"C:\\{joined}"


def remote_unc_path(host: str, canonical_dest: str, *relative_parts: str) -> str:
    """``\\\\HOST\\C$\\temp`` — ``host`` já deve estar formatado para UNC."""
    unc_host = format_unc_host(host) if host and not host.startswith("[") else host
    bits = [canonical_dest, *[p for p in relative_parts if p]]
    rel = "\\".join(
        "\\".join(str(b).replace("/", "\\").split("\\")).strip("\\")
        for b in bits
        if b
    )
    return f"\\\\{unc_host}\\C$\\{rel}"


def coerce_retries(value: object, default: int = DEFAULT_RETRIES) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return n if n in ALLOWED_RETRIES else default


def coerce_wait_seconds(value: object, default: int = DEFAULT_WAIT_SECONDS) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return n if n in ALLOWED_WAIT_SECONDS else default


def build_robocopy_switch_argv(
    switches: Optional[str],
    *,
    retries: object = DEFAULT_RETRIES,
    wait_seconds: object = DEFAULT_WAIT_SECONDS,
) -> list[str]:
    """Lista de switches Robocopy com ``/R`` e ``/W`` explícitos e únicos.

    ``switches=None`` aplica o quiet padrão; string vazia = nenhum quiet
    (ainda assim emite ``/R`` e ``/W``).
    """
    if switches is None:
        raw = DEFAULT_ROBOCOPY_SWITCHES
    else:
        raw = str(switches).strip()
    tokens = split_extra_args(raw) if raw else []
    # Nunca emitir opções destrutivas ou de follow de links.
    _BLOCKED = {
        "/MIR",
        "/PURGE",
        "/MOVE",
        "/MOV",
        "/SL",
        "/SJ",
        "/XF",
        "/XD",
    }
    filtered: list[str] = []
    for tok in tokens:
        upper = tok.upper()
        if upper.startswith("/R:") or upper.startswith("/W:"):
            continue
        if upper in _BLOCKED or upper.startswith("/R:") or upper.startswith("/W:"):
            continue
        # Normaliza flags quiet conhecidas para maiúsculas.
        if upper in {"/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP", "/E"}:
            filtered.append(upper)
        elif upper.startswith("/"):
            # Ignora switches não autorizados (política conservadora).
            continue
        else:
            filtered.append(tok)
    r = coerce_retries(retries)
    w = coerce_wait_seconds(wait_seconds)
    filtered.append(f"/R:{r}")
    filtered.append(f"/W:{w}")
    return filtered


def file_belongs_to_folder(file_path: str, folder_path: str) -> bool:
    """True se ``file_path`` está sob ``folder_path`` (sem escapar com ..)."""
    if not file_path or not folder_path:
        return False
    try:
        file_abs = os.path.abspath(os.path.normpath(file_path))
        folder_abs = os.path.abspath(os.path.normpath(folder_path))
        common = os.path.commonpath([file_abs, folder_abs])
        return os.path.normcase(common) == os.path.normcase(folder_abs)
    except (ValueError, OSError):
        return False
