"""Interpretação de CLIXML (saída serializada do PowerShell) em texto e metadados.

O PowerShell, quando o transporte JSON falha, pode emitir ``#< CLIXML`` em
stderr. Este módulo desserializa o XML de forma segura (sem instanciar tipos)
e devolve registros estruturados para diagnóstico — nunca para execução.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

CLIXML_NAMESPACE = "http://schemas.microsoft.com/powershell/2004/04"

MAX_DOCUMENT_CHARS = 512_000
MAX_FIELD_CHARS = 8_000
MAX_STACK_CHARS = 4_000
MAX_PLAIN_CHARS = 16_000
MAX_SUMMARY_CHARS = 400

_HEADER_RE = re.compile(r"#<\s*CLIXML\b", re.IGNORECASE)
_ESCAPE_RE = re.compile(r"_x([0-9A-Fa-f]{4})_")
_DTD_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_CLOSE_OBJS_RE = re.compile(r"</Objs\s*>", re.IGNORECASE)

# Fallback apenas para XML truncado/inválido — não é o caminho principal.
_S_TAG_RE = re.compile(
    r"<S\b(?P<attrs>[^>]*)>(?P<body>.*?)</S>",
    flags=re.IGNORECASE | re.DOTALL,
)
_ATTR_S_RE = re.compile(r"""\bS\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ATTR_N_RE = re.compile(r"""\bN\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

_CATEGORY_LINE_RE = re.compile(r"^\s*\+?\s*CategoryInfo\s*:\s*(.*)\s*$", re.IGNORECASE)
_FQID_LINE_RE = re.compile(r"^\s*\+?\s*FullyQualifiedErrorId\s*:\s*(.*)\s*$", re.IGNORECASE)
_STACK_LINE_RE = re.compile(r"^\s*\+?\s*ScriptStackTrace\s*:\s*(.*)\s*$", re.IGNORECASE)
_INVOCATION_LINE_RE = re.compile(
    r"^(At\s+(line:|C:|\\\\)|No line:|\+\s)",
    re.IGNORECASE,
)
_CATEGORY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]+$")

_STREAM_MAP = {
    "error": "Error",
    "warning": "Warning",
    "information": "Information",
    "info": "Information",
    "verbose": "Verbose",
    "debug": "Debug",
    "progress": "Progress",
    "output": "",
    "host": "",
}

_CLASSIFIED_STREAMS = frozenset({"Error", "Warning", "Information"})
_SKIP_TYPES = frozenset({"securestring", "ss"})
_CONTAINER_TAGS = frozenset({"props", "ms", "dct", "en"})

_SAFE_PARSE_FAIL = "Não foi possível interpretar a saída CLIXML do PowerShell."


@dataclass
class CliXmlRecord:
    stream: str
    message: str
    exception: str = ""
    category: str = ""
    error_id: str = ""
    target: str = ""
    stack_trace: str = ""


@dataclass
class CliXmlResult:
    records: list[CliXmlRecord] = field(default_factory=list)
    plain_text: str = ""
    parsed: bool = False
    parse_error: str = ""


def looks_like_clixml(text: str) -> bool:
    """True quando o texto contém o cabeçalho ``#< CLIXML`` (BOM/prefixo ignorados)."""
    return _HEADER_RE.search(_strip_bom(text or "")) is not None


def is_clixml_log_noise(line: str) -> bool:
    """True para linhas de transporte CLIXML que não devem ir ao painel de log."""
    s = _strip_bom(line or "").lstrip()
    if not s:
        return False
    head = s[:48]
    if head.lstrip().startswith("#<") and "CLIXML" in head.upper():
        return True
    if re.match(r"</?Objs\b", s, re.IGNORECASE):
        return True
    if re.match(r"</?Obj\b", s, re.IGNORECASE):
        return True
    if re.match(r"<S\s+S\s*=", s, re.IGNORECASE):
        return True
    if re.match(r"</S\s*>", s, re.IGNORECASE):
        return True
    return False


def decode_powershell_escapes(text: str) -> str:
    """Decodifica ``_xHHHH_`` em um único passe (``_x005F_`` vira ``_`` sem reprocessar)."""
    if not text or "_x" not in text:
        return text or ""
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def normalize_detail_text(text: str) -> str:
    """Normaliza texto detalhado: CRLF→LF, rstrip por linha, menos linhas vazias."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = _collapse_blank_lines(t)
    return _clip(t, MAX_FIELD_CHARS)


def summarize_one_line(text: str) -> str:
    """Resumo de uma linha para log/interface (não destrói o detalhe original)."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = " ".join(t.split())
    return _clip(t, MAX_SUMMARY_CHARS)


def normalize_stream(value: str) -> str:
    key = (value or "").strip()
    if not key:
        return ""
    mapped = _STREAM_MAP.get(key.lower())
    if mapped is not None:
        return mapped
    return key[:1].upper() + key[1:] if key else ""


def parse_clixml(text: str) -> CliXmlResult:
    """Analisa CLIXML de forma segura. Nunca levanta exceção para o chamador."""
    try:
        return _parse_clixml_inner(text)
    except Exception as exc:
        return CliXmlResult(
            records=[],
            plain_text=_safe_non_xml_fallback(text),
            parsed=False,
            parse_error=f"parser failure: {_clip(str(exc), 200)}",
        )


def clixml_to_text(text: str) -> str:
    """PowerShell pode emitir erros como ``#< CLIXML ...``.

    Devolve texto humano. Se não parecer CLIXML, devolve o texto normalizado.
    Nunca levanta exceção (a interface depende disso).
    """
    try:
        result = parse_clixml(text)
        if result.plain_text:
            return result.plain_text
        if looks_like_clixml(text or ""):
            return result.parse_error or ""
        return _normalize_source(text)
    except Exception:
        return _safe_non_xml_fallback(text)


def build_clixml_diagnostics(result: CliXmlResult) -> dict | None:
    """Monta o bloco ``Diagnostics`` para o payload de fallback (sem XML bruto)."""
    if result is None:
        return None
    rec = _primary_record(result.records)
    if rec is None and not result.parsed and not result.parse_error:
        return None
    diag: dict[str, str] = {"Format": "CLIXML"}
    if rec is not None:
        diag["Stream"] = rec.stream or ""
        if rec.category:
            diag["Category"] = rec.category
        if rec.error_id:
            diag["ErrorId"] = rec.error_id
        if rec.exception:
            diag["Exception"] = rec.exception
        if rec.target:
            diag["Target"] = rec.target
        if rec.stack_trace:
            diag["StackTrace"] = _clip(rec.stack_trace, MAX_STACK_CHARS)
    elif result.parse_error:
        diag["Stream"] = ""
    else:
        diag["Stream"] = ""
    return diag


def contains_raw_clixml(text: str) -> bool:
    """True se o texto ainda parece XML/CLIXML bruto (não deve ir à UI)."""
    s = _strip_bom(text or "")
    if looks_like_clixml(s):
        return True
    stripped = s.lstrip()
    return stripped.startswith("<Objs") or stripped.startswith("<Objs ")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _strip_bom(text: str) -> str:
    return (text or "").lstrip("\ufeff")


def _normalize_source(text: str) -> str:
    t = _strip_bom(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return t.strip()


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _collapse_blank_lines(text: str) -> str:
    out: list[str] = []
    blank = 0
    for line in (text or "").split("\n"):
        if line.strip():
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip("\n")


def _safe_non_xml_fallback(text: str) -> str:
    t = _normalize_source(text)
    if contains_raw_clixml(t):
        return _SAFE_PARSE_FAIL
    return t


def _local_name(tag: str) -> str:
    if not tag:
        return ""
    if tag[0] == "{":
        return tag.split("}", 1)[-1]
    if ":" in tag:
        return tag.split(":", 1)[-1]
    return tag


def _extract_xml_body(text: str) -> str | None:
    t = _strip_bom(text or "")
    match = _HEADER_RE.search(t)
    if match is None:
        return None
    body = t[match.end() :].lstrip("\ufeff \t\r\n")
    close = None
    for found in _CLOSE_OBJS_RE.finditer(body):
        close = found
    if close is not None:
        body = body[: close.end()]
    return body.strip()


def _parse_clixml_inner(text: str) -> CliXmlResult:
    source = _strip_bom(text or "")
    if not looks_like_clixml(source):
        return CliXmlResult(
            records=[],
            plain_text=_normalize_source(source),
            parsed=False,
        )

    body = _extract_xml_body(source)
    if not body:
        return CliXmlResult(
            records=[],
            plain_text=_SAFE_PARSE_FAIL,
            parsed=False,
            parse_error="missing XML body after CLIXML header",
        )

    if len(body) > MAX_DOCUMENT_CHARS:
        return CliXmlResult(
            records=[],
            plain_text="CLIXML omitido: documento excede o limite de tamanho.",
            parsed=False,
            parse_error=f"document exceeds {MAX_DOCUMENT_CHARS} characters",
        )

    if _DTD_RE.search(body):
        return CliXmlResult(
            records=[],
            plain_text=_SAFE_PARSE_FAIL,
            parsed=False,
            parse_error="DTD/ENTITY is not allowed",
        )

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        records = _regex_fallback_records(body)
        plain = _records_to_plain(records) if records else _SAFE_PARSE_FAIL
        return CliXmlResult(
            records=records,
            plain_text=plain,
            parsed=False,
            parse_error=f"invalid or truncated XML: {_clip(str(exc), 200)}",
        )

    records = _extract_records(root)
    return CliXmlResult(
        records=records,
        plain_text=_records_to_plain(records),
        parsed=True,
    )


def _extract_records(root: ET.Element) -> list[CliXmlRecord]:
    records: list[CliXmlRecord] = []
    pending: list[ET.Element] = []
    pending_stream = ""

    def flush_pending() -> None:
        nonlocal pending, pending_stream
        if pending:
            records.extend(_records_from_stream_group(pending, pending_stream))
        pending = []
        pending_stream = ""

    children = list(root) if _local_name(root.tag).lower() == "objs" else [root]
    if _local_name(root.tag).lower() != "objs":
        # Documento sem <Objs>: trata o próprio elemento.
        rec = _record_from_element(root)
        return [rec] if rec is not None else []

    for child in children:
        local = _local_name(child.tag).lower()
        stream = normalize_stream(child.attrib.get("S", ""))
        name = child.attrib.get("N", "")
        if local == "s" and not name:
            if pending and stream != pending_stream:
                flush_pending()
            pending.append(child)
            pending_stream = stream
            continue
        flush_pending()
        rec = _record_from_element(child)
        if rec is not None:
            records.append(rec)
    flush_pending()
    return records


def _record_from_element(elem: ET.Element) -> CliXmlRecord | None:
    local = _local_name(elem.tag).lower()
    stream = normalize_stream(elem.attrib.get("S", ""))
    if local == "s":
        text = _decoded_element_text(elem)
        if not text.strip():
            return None
        return _record_from_stream_lines([text], stream)
    if local == "obj":
        return _record_from_object(elem, stream)
    if stream in _CLASSIFIED_STREAMS:
        text = _decoded_element_text(elem)
        if text.strip():
            return CliXmlRecord(stream=stream, message=normalize_detail_text(text))
    return None


def _records_from_stream_group(elements: list[ET.Element], stream: str) -> list[CliXmlRecord]:
    lines = [_decoded_element_text(el) for el in elements]
    lines = [ln for ln in lines if ln.strip() or ln == ""]
    if not lines:
        return []
    blocks = _split_stream_blocks(lines)
    records: list[CliXmlRecord] = []
    for block in blocks:
        rec = _record_from_stream_lines(block, stream)
        if rec is not None:
            records.append(rec)
    return records


def _split_stream_blocks(lines: list[str]) -> list[list[str]]:
    """Separa erros consecutivos do mesmo stream após FullyQualifiedErrorId."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if _FQID_LINE_RE.match(line.strip()):
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return [b for b in blocks if any(x.strip() for x in b)]


def _record_from_stream_lines(lines: list[str], stream: str) -> CliXmlRecord | None:
    message_lines: list[str] = []
    category = ""
    error_id = ""
    stack_parts: list[str] = []
    for raw in lines:
        line = raw.replace("\r\n", "\n").replace("\r", "\n")
        stripped = line.strip()
        cat_m = _CATEGORY_LINE_RE.match(stripped)
        if cat_m:
            category = _category_name(cat_m.group(1) or "") or (cat_m.group(1) or "").strip()
            continue
        fq_m = _FQID_LINE_RE.match(stripped)
        if fq_m:
            error_id = (fq_m.group(1) or "").strip()
            continue
        st_m = _STACK_LINE_RE.match(stripped)
        if st_m:
            stack_parts.append((st_m.group(1) or "").rstrip())
            continue
        if _INVOCATION_LINE_RE.match(stripped):
            continue
        message_lines.append(line.rstrip())
    message = normalize_detail_text("\n".join(message_lines))
    if not message and not category and not error_id:
        return None
    return CliXmlRecord(
        stream=stream,
        message=message,
        category=category,
        error_id=error_id,
        stack_trace=_clip(normalize_detail_text("\n".join(stack_parts)), MAX_STACK_CHARS),
    )


def _category_name(info: str) -> str:
    head = (info or "").split(":", 1)[0].strip()
    if _CATEGORY_NAME_RE.fullmatch(head):
        return head
    return ""


def _record_from_object(elem: ET.Element, stream: str) -> CliXmlRecord | None:
    named = _collect_named(elem)
    tostring = _decoded_element_text(_find_child(elem, "ToString"))
    message_prop = _named_text(named, "Message")
    exception = _named_text(named, "Exception")
    error_id = _named_text(named, "FullyQualifiedErrorId")
    target = _named_text(named, "TargetObject")
    stack = _named_text(named, "ScriptStackTrace")
    category = _category_from_named(named)

    extras: list[str] = []
    for key, node in named.items():
        if key.lower() in {
            "message",
            "exception",
            "fullyqualifiederrorid",
            "targetobject",
            "scriptstacktrace",
            "categoryinfo",
            "invocationinfo",
            "errordetails",
            "pipelineiterationinfo",
        }:
            continue
        local = _local_name(node.tag).lower()
        node_stream = normalize_stream(node.attrib.get("S", ""))
        if local == "s" and node_stream in _CLASSIFIED_STREAMS:
            extras.append(_decoded_element_text(node))

    display = _pick_display_message(message_prop, exception, tostring, extras)
    if not display and not exception and not category and not error_id:
        return None
    return CliXmlRecord(
        stream=stream,
        message=normalize_detail_text(display),
        exception=normalize_detail_text(exception),
        category=category,
        error_id=error_id,
        target=normalize_detail_text(target),
        stack_trace=_clip(normalize_detail_text(stack), MAX_STACK_CHARS),
    )


def _pick_display_message(message: str, exception: str, tostring: str, extras: list[str]) -> str:
    if message:
        return message
    if tostring and exception and _essentially_same(tostring, exception):
        return tostring
    if exception:
        return exception
    if tostring:
        return tostring
    for extra in extras:
        if extra.strip():
            return extra
    return ""


def _essentially_same(a: str, b: str) -> bool:
    na = summarize_one_line(a).lower()
    nb = summarize_one_line(b).lower()
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _collect_named(elem: ET.Element) -> dict[str, ET.Element]:
    found: dict[str, ET.Element] = {}

    def consider(node: ET.Element) -> None:
        name = node.attrib.get("N") or ""
        if name and name not in found:
            found[name] = node

    for child in elem:
        consider(child)
        local = _local_name(child.tag).lower()
        if local in _CONTAINER_TAGS:
            for grandchild in child:
                consider(grandchild)
    return found


def _find_child(elem: ET.Element | None, local_name: str) -> ET.Element | None:
    if elem is None:
        return None
    want = local_name.lower()
    for child in elem:
        if _local_name(child.tag).lower() == want:
            return child
    return None


def _named_text(named: dict[str, ET.Element], key: str) -> str:
    for name, node in named.items():
        if name.lower() == key.lower():
            return _scalar_text(node)
    return ""


def _category_from_named(named: dict[str, ET.Element]) -> str:
    node = None
    for name, candidate in named.items():
        if name.lower() == "categoryinfo":
            node = candidate
            break
    if node is None:
        return ""
    inner = _collect_named(node)
    category = _named_text(inner, "Category")
    if category:
        return category
    tostring = _decoded_element_text(_find_child(node, "ToString"))
    return _category_name(tostring) or tostring.split(":", 1)[0].strip()


def _scalar_text(elem: ET.Element) -> str:
    local = _local_name(elem.tag).lower()
    if local in _SKIP_TYPES:
        return ""
    if local == "nil":
        return ""
    if local == "obj":
        inner = _collect_named(elem)
        category = _named_text(inner, "Category")
        if category:
            return category
        tostring = _decoded_element_text(_find_child(elem, "ToString"))
        if tostring:
            return tostring
        return ""
    return _decoded_element_text(elem)


def _decoded_element_text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    local = _local_name(elem.tag).lower()
    if local in _SKIP_TYPES:
        return ""
    raw = _element_text(elem)
    return decode_powershell_escapes(raw)


def _element_text(elem: ET.Element) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if _local_name(child.tag).lower() in _SKIP_TYPES:
            if child.tail:
                parts.append(child.tail)
            continue
        # Não descer em TN/Props/MS ao coletar texto de um Obj — só texto direto.
        child_local = _local_name(child.tag).lower()
        if child_local in {"tn", "props", "ms", "dct", "en", "obj"}:
            if child.tail:
                parts.append(child.tail)
            continue
        parts.append(_element_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _records_to_plain(records: list[CliXmlRecord]) -> str:
    parts = [rec.message for rec in records if (rec.message or "").strip()]
    text = "\n".join(parts).strip()
    return _clip(text, MAX_PLAIN_CHARS)


def _primary_record(records: list[CliXmlRecord]) -> CliXmlRecord | None:
    if not records:
        return None
    for wanted in ("Error", "Warning", "Information"):
        for rec in records:
            if rec.stream == wanted:
                return rec
    return records[0]


def _regex_fallback_records(body: str) -> list[CliXmlRecord]:
    """Último recurso para documentos truncados: extrai ``<S>`` completos."""
    records: list[CliXmlRecord] = []
    pending_lines: list[str] = []
    pending_stream = ""

    def flush() -> None:
        nonlocal pending_lines, pending_stream
        if pending_lines:
            rec = _record_from_stream_lines(pending_lines, pending_stream)
            if rec is not None:
                records.append(rec)
        pending_lines = []
        pending_stream = ""

    for match in _S_TAG_RE.finditer(body or ""):
        attrs = match.group("attrs") or ""
        if _ATTR_N_RE.search(attrs):
            continue
        stream = ""
        sm = _ATTR_S_RE.search(attrs)
        if sm:
            stream = normalize_stream(sm.group(1))
        raw_body = match.group("body") or ""
        # Entidades XML ainda estão no texto cru; o parser XML não rodou.
        decoded = decode_powershell_escapes(html.unescape(raw_body))
        if pending_lines and stream != pending_stream:
            flush()
        pending_lines.append(decoded)
        pending_stream = stream
    flush()
    return records
