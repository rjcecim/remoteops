"""Parsers do ``stdout`` em tabela do ``winget`` (pt-BR e en).

Os cabeçalhos podem vir em português ou inglês e, em alguns hosts, a tabela
sai com largura variável. Por isso tentamos usar as posições das colunas
detectadas no header; se não conseguirmos, caímos em ``split`` por 2+ espaços.

Quando o nome é truncado com reticências UTF-8 (``…``) e o stdout foi lido
como CP1252, o glifo vira ``â€¦`` e desloca as fatias fixas — o Id começa com
``€¦`` e as versões herdam dígitos do campo anterior. Por isso normalizamos
mojibake e validamos o Id com fallback por regex.
"""

from __future__ import annotations

import re

# UTF-8 lido como CP1252/Latin-1 (reticências e travessões do winget).
_MOJIBAKE_MAP = (
    ("\u00e2\u20ac\u00a6", "\u2026"),  # â€¦ → …
    ("\u00e2\u20ac\u201d", "\u2014"),  # â€” → —
    ("\u00e2\u20ac\u201c", "\u2013"),  # â€“ → –
    ("\u00e2\u20ac\u2122", "\u2122"),  # â„¢ → ™
    ("\u00c2\u00ae", "\u00ae"),  # Â® → ®
    ("\u00c2\u00a9", "\u00a9"),  # Â© → ©
)

# Id winget: Publisher.Package… (exige letra após um ponto; evita versões numéricas).
_PKG_ID_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9+._\-]*\.[A-Za-z][A-Za-z0-9+._\-]*)")
_SOURCE_RE = re.compile(r"(?i)(?<!\S)(winget|msstore)$")
_VERSION_TOKEN_RE = re.compile(r"^\d+(?:\.\d+){1,5}$")


def normalize_winget_table_text(text: str) -> str:
    """Corrige mojibake típico de UTF-8 interpretado como CP1252."""
    out = text or ""
    for bad, good in _MOJIBAKE_MAP:
        if bad in out:
            out = out.replace(bad, good)
    return out


def _looks_like_pkg_id(value: str) -> bool:
    s = (value or "").strip()
    if not s or len(s) > 128:
        return False
    # Fatias fixas às vezes incluem o início da versão ("Id 1"); usa o 1º token.
    s = s.split()[0]
    if any(ord(c) > 127 for c in s):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._\-]*\.[A-Za-z][A-Za-z0-9+._\-]*", s))


def _clean_pkg_id(value: str) -> str:
    s = (value or "").strip()
    return s.split()[0] if s else ""


def _looks_like_version(value: str) -> bool:
    s = (value or "").strip()
    if not s:
        return False
    return bool(_VERSION_TOKEN_RE.match(s.split()[0]))


def _looks_like_source(value: str) -> bool:
    return (value or "").strip().lower() in {"winget", "msstore"}


def _parse_row_by_pkg_id(
    raw: str,
    *,
    expect_available: bool,
) -> dict | None:
    """Fallback: localiza o Id por padrão Publisher.Package e versões à direita."""
    line = normalize_winget_table_text(raw).rstrip()
    if not line:
        return None
    src_m = _SOURCE_RE.search(line)
    if not src_m:
        return None
    source = src_m.group(1)
    body = line[: src_m.start()].rstrip()
    id_matches = list(_PKG_ID_RE.finditer(body))
    if not id_matches:
        return None
    # Preferir o último Id à esquerda das versões (nome pode ter pontos raramente).
    id_m = id_matches[-1]
    name = body[: id_m.start()].rstrip(" .…")
    name = name.rstrip()
    pkg_id = id_m.group(1)
    right = body[id_m.end() :].strip()
    tokens = right.split()
    versions = [t for t in tokens if _VERSION_TOKEN_RE.match(t)]
    if expect_available:
        if len(versions) < 2 or not name:
            return None
        version, available = versions[0], versions[1]
    else:
        if len(versions) < 1 or not name:
            return None
        version = versions[0]
        available = versions[1] if len(versions) >= 2 else ""
    return {
        "Name": name,
        "Id": pkg_id,
        "Version": version,
        "Available": available,
        "Source": source,
    }


def _header_index(lines: list[str], *, require_available: bool = False) -> int:
    """Índice da linha de cabeçalho (``Name Id Version ...``) ou ``-1``."""
    for i, line in enumerate(lines):
        s = (line or "").strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("name") and " id" in low and "version" in low:
            if require_available and "available" not in low:
                continue
            return i
        if low.startswith("nome") and " id" in low and ("versão" in low or "versao" in low):
            if require_available and not ("disponível" in low or "disponivel" in low):
                continue
            return i
    return -1


def _column_positions(header_line: str, tokens: list[str]) -> list[int]:
    low = (header_line or "").lower()

    def _pos(token: str) -> int:
        try:
            return low.index(token)
        except ValueError:
            return -1

    return [_pos(t) for t in tokens]


def _is_separator_or_empty(raw: str) -> bool:
    stripped = raw.strip()
    if not stripped:
        return True
    return set(stripped) == {"-"}


def _slice_row(raw: str, positions: list[int]) -> list[str]:
    """Aplica ``raw[a:b].strip()`` entre as posições informadas."""
    out: list[str] = []
    for start, end in zip(positions, positions[1:] + [None]):
        chunk = raw[start:end] if end is not None else raw[start:]
        out.append(chunk.strip())
    return out


def _split_spaced(raw: str) -> list[str]:
    return re.split(r"\s{2,}", raw.strip())


def parse_winget_list(lines: list[str]) -> list[dict]:
    """Parse do ``winget list`` → ``Name/Id/Version/Available/Source``."""
    lines = [normalize_winget_table_text(x) for x in lines]
    header_idx = _header_index(lines, require_available=False)
    if header_idx < 0:
        return []

    header = lines[header_idx]
    id_pos, ver_pos, avail_pos, src_pos = _column_positions(
        header, [" id", "version", "available", "source"]
    )
    if id_pos < 0:
        id_pos = _column_positions(header, ["id"])[0]
    if ver_pos < 0:
        ver_pos = _column_positions(header, ["versão"])[0]
    if avail_pos < 0:
        avail_pos = _column_positions(header, ["disponível"])[0]
        if avail_pos < 0:
            avail_pos = _column_positions(header, ["disponivel"])[0]
    if src_pos < 0:
        src_pos = _column_positions(header, ["origem"])[0]

    use_slices_5 = (
        all(p >= 0 for p in (id_pos, ver_pos, avail_pos, src_pos))
        and id_pos < ver_pos < avail_pos < src_pos
    )
    use_slices_4 = (
        all(p >= 0 for p in (id_pos, ver_pos, src_pos))
        and avail_pos < 0
        and id_pos < ver_pos < src_pos
    )

    out: list[dict] = []
    for raw in lines[header_idx + 2 :]:
        raw = (raw or "").rstrip()
        if _is_separator_or_empty(raw):
            continue
        low = raw.lower().strip()
        if "no installed package" in low or "nenhum pacote instalado" in low:
            break

        row: dict | None = None
        if use_slices_5:
            name, pkg_id, version, available, source = _slice_row(
                raw, [0, id_pos, ver_pos, avail_pos, src_pos]
            )
            pkg_id = _clean_pkg_id(pkg_id)
            if (
                name
                and pkg_id
                and _looks_like_pkg_id(pkg_id)
                and _looks_like_version(version)
                and _looks_like_source(source)
            ):
                row = {
                    "Name": name.rstrip(" .…"),
                    "Id": pkg_id,
                    "Version": version.strip().split()[0],
                    "Available": available.strip().split()[0] if available else "",
                    "Source": source.strip().split()[0],
                }
        elif use_slices_4:
            name, pkg_id, version, source = _slice_row(raw, [0, id_pos, ver_pos, src_pos])
            pkg_id = _clean_pkg_id(pkg_id)
            if (
                name
                and pkg_id
                and _looks_like_pkg_id(pkg_id)
                and _looks_like_version(version)
                and _looks_like_source(source)
            ):
                row = {
                    "Name": name.rstrip(" .…"),
                    "Id": pkg_id,
                    "Version": version.strip().split()[0],
                    "Available": "",
                    "Source": source.strip().split()[0],
                }
        else:
            parts = _split_spaced(raw)
            if len(parts) >= 4:
                name, pkg_id, version = parts[0], parts[1], parts[2]
                available = parts[3] if len(parts) >= 5 else ""
                source = parts[4] if len(parts) >= 5 else parts[3]
                pkg_id = _clean_pkg_id(pkg_id)
                if (
                    name
                    and pkg_id
                    and _looks_like_pkg_id(pkg_id)
                    and _looks_like_version(version)
                    and _looks_like_source(source)
                ):
                    row = {
                        "Name": name.rstrip(" .…"),
                        "Id": pkg_id,
                        "Version": version.strip().split()[0],
                        "Available": available.strip().split()[0] if available else "",
                        "Source": source.strip().split()[0],
                    }

        if row is None:
            row = _parse_row_by_pkg_id(raw, expect_available=False)
        if row is None:
            continue
        out.append(row)
    return out


def _search_column_positions(header_line: str) -> tuple[int, int, int, int]:
    """Retorna ``(id_pos, ver_pos, match_pos, src_pos)``; ``-1`` quando ausente."""
    low = (header_line or "").lower()
    id_pos = low.find(" id")
    if id_pos < 0:
        id_pos = low.find("id")
    ver_pos = low.find("version")
    if ver_pos < 0:
        ver_pos = low.find("versão")
    if ver_pos < 0:
        ver_pos = low.find("versao")
    match_pos = low.find("match")
    if match_pos < 0:
        match_pos = low.find("correspondência")
    if match_pos < 0:
        match_pos = low.find("correspondencia")
    src_pos = low.find("source")
    if src_pos < 0:
        src_pos = low.find("origem")
    return id_pos, ver_pos, match_pos, src_pos


def _parse_search_row_tokens(raw: str) -> tuple[str, str, str, str, str] | None:
    """Fallback para linhas compactas com apenas um espaco entre colunas."""
    parts = _split_spaced(raw)
    if len(parts) >= 4:
        name, pkg_id, version = parts[0], parts[1], parts[2]
        match = parts[3] if len(parts) >= 5 else ""
        source = parts[4] if len(parts) >= 5 else parts[3]
        if name and pkg_id and version and source:
            return name, pkg_id, version, match, source

    tokens = (raw or "").split()
    if len(tokens) == 4:
        name, pkg_id, version, source = tokens
        if name and pkg_id and version and source:
            return name, pkg_id, version, "", source
    if len(tokens) >= 5:
        name, pkg_id, version, match, source = tokens[0], tokens[1], tokens[2], tokens[3], tokens[4]
        if name and pkg_id and version and source:
            return name, pkg_id, version, match, source
    return None


def parse_winget_search(lines: list[str]) -> list[dict]:
    """Parse do ``winget search`` → ``Name/Id/Version/Match/Source``."""
    lines = [normalize_winget_table_text(x) for x in lines]
    header_idx = _header_index(lines, require_available=False)
    if header_idx < 0:
        return []

    header = lines[header_idx]
    id_pos, ver_pos, match_pos, src_pos = _search_column_positions(header)
    use_slices_5 = (
        all(p >= 0 for p in (id_pos, ver_pos, match_pos, src_pos))
        and id_pos < ver_pos < match_pos < src_pos
    )
    use_slices_4 = (
        all(p >= 0 for p in (id_pos, ver_pos, src_pos))
        and match_pos < 0
        and id_pos < ver_pos < src_pos
    )

    out: list[dict] = []
    for raw in lines[header_idx + 2 :]:
        raw = (raw or "").rstrip()
        if _is_separator_or_empty(raw):
            continue

        parsed: tuple[str, str, str, str, str] | None = None
        if use_slices_5:
            name, pkg_id, version, match, source = _slice_row(
                raw, [0, id_pos, ver_pos, match_pos, src_pos]
            )
            pkg_id = _clean_pkg_id(pkg_id)
            if name and pkg_id and version and source and _looks_like_pkg_id(pkg_id):
                parsed = (name, pkg_id, version, match, source)
        elif use_slices_4:
            name, pkg_id, version, source = _slice_row(raw, [0, id_pos, ver_pos, src_pos])
            pkg_id = _clean_pkg_id(pkg_id)
            if name and pkg_id and version and source and _looks_like_pkg_id(pkg_id):
                parsed = (name, pkg_id, version, "", source)
        if parsed is None:
            parsed = _parse_search_row_tokens(raw)
            if parsed is not None:
                pkg_id = _clean_pkg_id(parsed[1])
                parsed = (parsed[0], pkg_id, parsed[2], parsed[3], parsed[4])
                if not _looks_like_pkg_id(pkg_id):
                    parsed = None
        if parsed is None:
            resilient = _parse_row_by_pkg_id(raw, expect_available=False)
            if resilient is None:
                continue
            out.append(
                {
                    "Name": resilient["Name"],
                    "Id": resilient["Id"],
                    "Version": resilient["Version"],
                    "Match": "",
                    "Source": resilient["Source"],
                }
            )
            continue

        name, pkg_id, version, match, source = parsed
        out.append(
            {
                "Name": name,
                "Id": pkg_id,
                "Version": version,
                "Match": match,
                "Source": source,
            }
        )
    return out


_UPGRADE_FOOTER_RE = re.compile(
    r"(\bupgrades?\s+available\.|\batualiza(?:ç|c)(?:õ|o)es\s+dispon(?:í|i)veis\.)\s*$",
    re.IGNORECASE,
)


def parse_winget_upgrade(lines: list[str]) -> list[dict]:
    """Parse do ``winget upgrade`` → ``Name/Id/Version/Available/Source``."""
    lines = [normalize_winget_table_text(x) for x in lines]
    header_idx = _header_index(lines, require_available=True)
    if header_idx < 0:
        return []

    header = lines[header_idx]
    id_pos, ver_pos, avail_pos, src_pos = _column_positions(
        header, [" id", "version", "available", "source"]
    )
    if id_pos < 0:
        id_pos = _column_positions(header, ["id"])[0]
    if ver_pos < 0:
        ver_pos = _column_positions(header, ["versão"])[0]
    if avail_pos < 0:
        avail_pos = _column_positions(header, ["disponível"])[0]
        if avail_pos < 0:
            avail_pos = _column_positions(header, ["disponivel"])[0]
    if src_pos < 0:
        src_pos = _column_positions(header, ["origem"])[0]

    use_slices = all(p >= 0 for p in (id_pos, ver_pos, avail_pos, src_pos)) and (
        id_pos < ver_pos < avail_pos < src_pos
    )

    out: list[dict] = []
    for raw in lines[header_idx + 2 :]:
        raw = (raw or "").rstrip()
        if _is_separator_or_empty(raw):
            continue
        if _UPGRADE_FOOTER_RE.search(raw.strip()):
            break

        row: dict | None = None
        if use_slices:
            name, pkg_id, version, available, source = _slice_row(
                raw, [0, id_pos, ver_pos, avail_pos, src_pos]
            )
            pkg_id = _clean_pkg_id(pkg_id)
            if (
                name
                and pkg_id
                and version
                and _looks_like_pkg_id(pkg_id)
                and _looks_like_version(version)
                and _looks_like_version(available)
                and _looks_like_source(source)
            ):
                row = {
                    "Name": name.rstrip(" .…"),
                    "Id": pkg_id,
                    "Version": version.strip().split()[0],
                    "Available": available.strip().split()[0],
                    "Source": source.strip().split()[0],
                }
        else:
            parts = _split_spaced(raw)
            if len(parts) >= 5:
                name, pkg_id, version, available, source = parts[:5]
                pkg_id = _clean_pkg_id(pkg_id)
                if (
                    name
                    and pkg_id
                    and version
                    and _looks_like_pkg_id(pkg_id)
                    and _looks_like_version(version)
                    and _looks_like_version(available)
                    and _looks_like_source(source)
                ):
                    row = {
                        "Name": name.rstrip(" .…"),
                        "Id": pkg_id,
                        "Version": version.strip().split()[0],
                        "Available": available.strip().split()[0],
                        "Source": source.strip().split()[0],
                    }

        if row is None:
            row = _parse_row_by_pkg_id(raw, expect_available=True)
        if row is None:
            continue
        out.append(row)
    return out
