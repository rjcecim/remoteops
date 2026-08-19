"""Classificação de linhas de saída do winget (log vs. progresso de download)."""

from __future__ import annotations

import re

from .clixml import clixml_to_text, contains_raw_clixml, looks_like_clixml, summarize_one_line
from .constants import (
    ANSI_RE,
    PROGRESS_PCT_RE,
    PROGRESS_RE,
    SPINNER_LINES,
    WINGET_SOFT_SUCCESS_EXIT_CODES,
    is_winget_success_exit,
    result_exit_code,
)

_BLOCK_CHARS = "█▒░▓▌▐■□▪▫"
_BLOCK_CLASS = rf"[\s{_BLOCK_CHARS}\u2580-\u259f\u25a0-\u25ff]*"
_TABLE_SEPARATOR_RE = re.compile(r"^[-─\s]+$")
# Mojibake típico de barras UTF-8 lidas como Latin-1/CP1252 (â–ˆ, Â█, etc.).
_MOJIBAKE_PROGRESS_RE = re.compile(
    r"("
    r"â[\u0080-\u00ff]{1,3}"  # UTF-8 block chars mal decodificados
    r"|Ã[\u0080-\u00bf]"
    r"|\ufffd"  # replacement char
    r")",
    re.IGNORECASE,
)


def normalize_winget_line(line: str) -> str:
    """Normaliza uma linha para comparação/deduplicação."""
    text = ANSI_RE.sub("", line or "")
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u00a0", " ")
    return text.strip()


# WinGet redesenha espera/status com \\r: "   - Waiting for another install..."
_SPINNER_STATUS_RE = re.compile(r"^[-\\|\/]\s+\S")


def is_winget_spinner_status(line: str) -> bool:
    """True para linha de spinner+texto (espera de outro install, etc.)."""
    stripped = normalize_winget_line(line)
    if not stripped or stripped in SPINNER_LINES:
        return False
    return bool(_SPINNER_STATUS_RE.match(stripped))


def is_winget_download_progress(line: str) -> bool:
    """True para barras de download do winget (caracteres de bloco / % / tamanho)."""
    stripped = normalize_winget_line(line)
    if not stripped or stripped in SPINNER_LINES:
        return True
    if PROGRESS_RE.match(stripped):
        return True
    if PROGRESS_PCT_RE.match(stripped):
        return True
    if re.match(
        rf"^{_BLOCK_CLASS}[0-9.,]+\s*(KB|MB|GB)\s*/\s*[0-9.,]+\s*(KB|MB|GB)\s*$", stripped, re.I
    ):
        return True
    if re.match(rf"^{_BLOCK_CLASS}\d{{1,3}}%\s*$", stripped):
        return True
    # Barra de progresso com encoding errado: quase só glifos quebrados + %/MB.
    if _MOJIBAKE_PROGRESS_RE.search(stripped):
        if re.search(r"\d{1,3}%", stripped) or re.search(r"(KB|MB|GB)\s*/\s*", stripped, re.I):
            return True
        # Linha só com lixo de barra (sem texto útil).
        useful = re.sub(r"[\s\d.,%/\ufffdâÃ█▒░▓\-\\|]+", "", stripped, flags=re.I)
        if len(useful) <= 2 and len(stripped) >= 4:
            return True
    return False


def is_winget_table_chrome(line: str) -> bool:
    """True para cabeçalho/separador de tabela do winget (não útil no log)."""
    stripped = normalize_winget_line(line)
    if not stripped:
        return False
    if _TABLE_SEPARATOR_RE.match(stripped.replace("─", "-")):
        return True
    low = stripped.lower()
    if not (low.startswith("name") or low.startswith("nome")):
        return False
    if " id" not in low and not low.startswith("id"):
        return False
    return "version" in low or "versão" in low or "versao" in low


_INSTALL_START_RE = re.compile(
    r"("
    r"starting package install"
    r"|installer hash"
    r"|verified installer hash"
    r"|starting (?:install|installation)"
    r"|installing\b"
    r"|iniciando a instala"  # "Iniciando a instalação do pacote..."
    r"|instalando\b"
    r"|hash do instalador"
    r")",
    re.IGNORECASE,
)


_PACKAGE_HEADER_RE = re.compile(r"^---\s+(.+?)\s+---$")

# `winget upgrade --all`: "(1/3) Found Google Chrome [Google.Chrome] Version ..."
# pt-BR: "(1/3) Encontrado Google Chrome [Google.Chrome] Versão ..."
_FOUND_PACKAGE_RE = re.compile(
    r"^(?:\((?P<idx>\d+)\s*/\s*(?P<total>\d+)\)\s+)?"
    r"(?:Found|Encontrad[oa])\s+"
    r"(?P<name>.+?)\s+"
    r"\[(?P<id>[^\]]+)\]",
    re.IGNORECASE,
)

_DOWNLOAD_START_RE = re.compile(r"^Downloading\s+https?://", re.IGNORECASE)
_BAIXANDO_START_RE = re.compile(r"^Baixando\s+https?://", re.IGNORECASE)

_LISTING_PKG_ID_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9+._\-]*\.[A-Za-z0-9][A-Za-z0-9+._\-]*)"
)
_UPGRADES_AVAILABLE_RE = re.compile(
    r"(\bupgrades?\s+available\b|\batualiza(?:ç|c)(?:õ|o)es\s+dispon(?:í|i)veis\b)",
    re.IGNORECASE,
)

_ITEM_COMPLETE_RE = re.compile(
    r"("
    r"successfully installed"
    r"|successfully uninstalled"
    r"|upgraded successfully"
    r"|installation failed"
    r"|uninstall failed"
    r"|instalado com sucesso"
    r"|desinstalado com sucesso"
    r"|falha na instala"
    r"|atualizado com sucesso"
    r")",
    re.IGNORECASE,
)


def parse_package_header(line: str) -> str | None:
    """Extrai o ID do pacote de uma linha ``--- Package.Id ---`` emitida pelo script remoto."""
    stripped = normalize_winget_line(line)
    m = _PACKAGE_HEADER_RE.match(stripped)
    if not m:
        return None
    pkg_id = (m.group(1) or "").strip()
    return pkg_id or None


def parse_found_package(line: str) -> tuple[str, str, int | None, int | None] | None:
    """Extrai nome/ID de ``Found Name [Id]`` (e ``(n/m)``) do ``upgrade --all``.

    Devolve ``(display_name, pkg_id, idx, total)`` ou ``None``.
    """
    stripped = normalize_winget_line(line)
    m = _FOUND_PACKAGE_RE.match(stripped)
    if not m:
        return None
    pkg_id = (m.group("id") or "").strip()
    name = (m.group("name") or "").strip()
    if not pkg_id or " " in pkg_id:
        return None
    # Evita "Found an existing package already installed" e similares.
    if "." not in pkg_id and not re.fullmatch(r"(?:9[A-Z0-9]{10,}|XP[A-Z0-9]{10,})", pkg_id, re.I):
        return None
    idx = int(m.group("idx")) if m.group("idx") else None
    total = int(m.group("total")) if m.group("total") else None
    return (name or pkg_id, pkg_id, idx, total)


def parse_upgrade_listing_row(line: str) -> tuple[str, str] | None:
    """Nome completo + Id de uma linha da tabela do ``winget upgrade --all``.

    O ``Found Name [Id]`` costuma truncar o nome (``Java 8`` em vez de
    ``Java 8 Update 501 (64-bit)``). A tabela listada antes traz o Nome.
    """
    stripped = normalize_winget_line(line)
    if not stripped or is_winget_table_chrome(line):
        return None
    if parse_found_package(stripped) or parse_package_header(stripped):
        return None
    if _UPGRADES_AVAILABLE_RE.search(stripped):
        return None
    if "://" in stripped:
        return None
    low = stripped.lower()
    if low.startswith("downloading") or low.startswith("baixando"):
        return None

    best: tuple[int, str] | None = None
    for m in _LISTING_PKG_ID_RE.finditer(stripped):
        pkg_id = m.group(1)
        if " " in pkg_id or not re.search(r"[A-Za-z]", pkg_id):
            continue
        if re.fullmatch(r"\d+(?:\.\d+){1,5}", pkg_id):
            continue
        best = (m.start(), pkg_id)
    if not best:
        return None
    start, pkg_id = best
    name = stripped[:start].rstrip(" .…").strip()
    right = stripped[start + len(pkg_id) :].strip()
    if not name or len(name) < 2:
        return None
    if not re.search(r"\d+(?:\.\d+)+", right):
        return None
    return name, pkg_id


def is_winget_download_start(line: str) -> bool:
    """True quando o winget começa a baixar o instalador."""
    stripped = normalize_winget_line(line)
    if not stripped:
        return False
    return bool(_DOWNLOAD_START_RE.match(stripped) or _BAIXANDO_START_RE.match(stripped))


def is_winget_item_complete(line: str) -> bool:
    """True quando um pacote terminou (sucesso ou falha)."""
    stripped = normalize_winget_line(line)
    if not stripped:
        return False
    return bool(_ITEM_COMPLETE_RE.search(stripped))


def is_winget_install_start(line: str) -> bool:
    """True quando a saída indica que o download terminou e a instalação começou."""
    stripped = normalize_winget_line(line)
    if not stripped:
        return False
    return bool(_INSTALL_START_RE.search(stripped))


def winget_download_percent(line: str) -> int | None:
    """Extrai percentual de uma linha de progresso de download, se houver."""
    stripped = normalize_winget_line(line)
    m = PROGRESS_RE.match(stripped)
    if m:
        try:
            cur = float(m.group("cur").replace(",", "."))
            tot = float(m.group("tot").replace(",", "."))
            cur_u = m.group("cur_u").upper()
            tot_u = m.group("tot_u").upper()
            cur_mb = cur / 1024.0 if cur_u == "KB" else cur * 1024.0 if cur_u == "GB" else cur
            tot_mb = tot / 1024.0 if tot_u == "KB" else tot * 1024.0 if tot_u == "GB" else tot
            if tot_mb > 0:
                return int(max(0.0, min(100.0, (cur_mb / tot_mb) * 100.0)))
        except Exception:
            pass
    mp = PROGRESS_PCT_RE.match(stripped)
    if mp:
        try:
            return max(0, min(100, int(mp.group("pct"))))
        except Exception:
            pass
    return None


def summarize_winget_output(output: str) -> str:
    """Extrai uma linha útil da saída do winget (ignora spinners e ANSI)."""
    if not output:
        return ""
    text = output
    if looks_like_clixml(text) or contains_raw_clixml(text):
        text = clixml_to_text(text)
    if contains_raw_clixml(text):
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        cleaned = normalize_winget_line(line)
        if not cleaned or cleaned in SPINNER_LINES:
            continue
        if looks_like_clixml(cleaned) or contains_raw_clixml(cleaned):
            continue
        lines.append(cleaned)
    chosen = ""
    for ln in lines:
        if _ITEM_COMPLETE_RE.search(ln):
            chosen = ln
            break
    if not chosen:
        for ln in lines:
            low = ln.lower()
            if "failed" in low or "not found" in low or "falha" in low:
                chosen = ln
                break
    if not chosen:
        chosen = lines[0] if lines else ""
    return summarize_one_line(chosen)


def _status_prefix_for_result(result: dict) -> str:
    diag = result.get("Diagnostics") if isinstance(result.get("Diagnostics"), dict) else {}
    stream = str((diag or {}).get("Stream") or "").strip()
    stream_l = stream.lower()
    if stream_l == "error":
        return "[ERRO]"
    if stream_l == "warning":
        return "[AVISO]"
    if stream_l in {"information", "info"}:
        return "[INFO]"
    if stream_l == "verbose":
        return "[VERBOSE]"
    if stream_l == "debug":
        return "[DEBUG]"
    exit_code = result_exit_code(result.get("ExitCode"), if_missing=1)
    if is_winget_success_exit(exit_code, if_missing=1):
        if exit_code in WINGET_SOFT_SUCCESS_EXIT_CODES:
            return "[AVISO]"
        return "[OK]"
    return "[ERRO]"


def _hint_for_result(result: dict) -> str:
    output = str(result.get("Output") or "")
    hint = summarize_winget_output(output)
    if hint:
        return hint
    diag = result.get("Diagnostics") if isinstance(result.get("Diagnostics"), dict) else {}
    if not diag:
        return ""
    for key in ("Exception", "Category", "ErrorId"):
        value = summarize_one_line(str(diag.get(key) or ""))
        if value:
            return value
    return ""


def format_exec_result_line(result: dict) -> str:
    """Uma linha humana: ``[ERRO] Id: resumo`` — sem XML, stack ou metadados crus."""
    if not isinstance(result, dict):
        return ""
    pkg_id = str(result.get("Id") or "").strip() or "pacote"
    prefix = _status_prefix_for_result(result)
    hint = _hint_for_result(result)
    exit_code = result_exit_code(result.get("ExitCode"), if_missing=0)
    if hint:
        return f"{prefix} {pkg_id}: {hint}"
    if prefix == "[OK]":
        return f"{prefix} {pkg_id}: concluído com sucesso."
    return f"{prefix} {pkg_id}: falhou (exit={exit_code})."


def filter_winget_log_lines(lines: list[str] | str) -> list[str]:
    """Mantém apenas linhas que devem aparecer no log (exclui progresso de download)."""
    if isinstance(lines, str):
        raw = lines.splitlines()
    else:
        raw = list(lines)
    out: list[str] = []
    for line in raw:
        if not normalize_winget_line(line):
            continue
        if is_winget_download_progress(line):
            continue
        if is_winget_table_chrome(line):
            continue
        out.append(line)
    return out
