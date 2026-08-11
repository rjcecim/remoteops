"""Leitura binária de ``stdout``/``stderr`` do PsExec.

Cuida de três coisas chatas:

1. Quebras de linha podem vir só como ``\\r`` (sem ``\\n``) em barras de progresso.
2. A saída CLIXML do PowerShell vem em UTF-16LE (separada por ``\\n\\x00``/``\\r\\x00``).
3. O conteúdo é bytes crus; precisamos decodificar sem explodir em caracteres inválidos.
"""

from __future__ import annotations

from typing import Callable

from .constants import (
    B64_CHUNK_RE,
    JSON_LINE_RE,
    MARKER_LINE_RE,
    MARKER_LOG_RE,
    MARKER_PCT_RE,
    REALTIME_LOG_PREFIX,
    SPINNER_LINES,
)
from .winget_output import is_winget_download_progress, is_winget_table_chrome, normalize_winget_line, winget_download_percent


def _looks_like_utf16le(buffer: bytearray) -> bool:
    """Heurística: se o buffer tem muitos bytes zero intercalados, é UTF-16LE."""
    if not buffer:
        return False
    return buffer.count(b"\x00") >= max(8, len(buffer) // 8)


def decode_bytes(buf: bytes) -> str:
    """Decodifica bytes em texto, escolhendo o codec correto sem gerar ``�``.

    Tenta ``utf-8`` de forma *estrita* primeiro: se falhar, é sinal de que a linha
    veio do próprio PsExec na code page OEM do console (ex.: ``cp850`` em PT-BR),
    e não em ``cp1252``/``utf-8``. Só cai para ``latin-1`` (com ``replace``) como
    último recurso, para nunca explodir.
    """
    if buf and (buf.count(b"\x00") >= max(2, len(buf) // 8)):
        try:
            return buf.decode("utf-16le", errors="replace")
        except Exception:
            pass
    for enc in ("utf-8", "oem", "cp850", "cp1252"):
        try:
            return buf.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return buf.decode("latin-1", errors="replace")


def _split_utf16le_line(buffer: bytearray) -> tuple[bytes, int] | None:
    nl = buffer.find(b"\n\x00")
    cr = buffer.find(b"\r\x00")
    if nl < 0 and cr < 0:
        return None
    if nl < 0:
        end, skip = cr, 2
    elif cr < 0:
        end, skip = nl, 2
    else:
        end = min(nl, cr)
        is_crlf = (
            end + 3 < len(buffer)
            and buffer[end] == 0x0D
            and buffer[end + 1] == 0x00
            and buffer[end + 2] == 0x0A
            and buffer[end + 3] == 0x00
        )
        skip = 4 if is_crlf else 2

    line_bytes = bytes(buffer[:end])
    if len(line_bytes) % 2 == 1:
        line_bytes = line_bytes[:-1]
    return line_bytes, end + skip


def _split_ansi_line(buffer: bytearray) -> tuple[bytes, int] | None:
    nl = buffer.find(b"\n")
    cr = buffer.find(b"\r")
    if nl < 0 and cr < 0:
        return None
    if nl < 0:
        end, skip = cr, 1
    elif cr < 0:
        end, skip = nl, 1
    else:
        end = min(nl, cr)
        is_crlf = end + 1 < len(buffer) and buffer[end] == 0x0D and buffer[end + 1] == 0x0A
        skip = 2 if is_crlf else 1
    return bytes(buffer[:end]), end + skip


def _normalize_stripped(line: str) -> str:
    return normalize_winget_line(line)


def _try_parse_log_marker(stripped: str) -> str | None:
    m = MARKER_LOG_RE.match(stripped)
    if m is None:
        return None
    return m.group("payload") or ""


def _try_parse_pct_marker(stripped: str, progress_cb: Callable[[int], None] | None) -> bool:
    """Marcador dedicado ``__WINGETRM_PCT__<n>`` do ConPTY: sempre consumido
    (nunca vai para o log) e roteado direto para a barra de progresso."""
    m = MARKER_PCT_RE.match(stripped)
    if m is None:
        return False
    if progress_cb is not None:
        try:
            progress_cb(max(0, min(100, int(m.group("pct")))))
        except Exception:
            pass
    return True


def _try_parse_progress(stripped: str, progress_cb: Callable[[int], None] | None) -> bool:
    if progress_cb is None or not is_winget_download_progress(stripped):
        return False
    pct = winget_download_percent(stripped)
    if pct is not None:
        try:
            progress_cb(pct)
        except Exception:
            pass
    return True


def _is_noise_line(stripped: str) -> bool:
    if not stripped or stripped in SPINNER_LINES:
        return True
    if JSON_LINE_RE.match(stripped):
        return True
    if MARKER_LINE_RE.match(stripped):
        return True
    if B64_CHUNK_RE.match(stripped):
        return True
    return False


def make_line_processor(
    *,
    log_cb: Callable[[str], None] | None,
    progress_cb: Callable[[int], None] | None,
) -> Callable[[str, list[str], bool], None]:
    """Cria a função que consome cada linha decodificada e, se fizer sentido,
    repassa para a UI via ``log_cb`` / ``progress_cb``.
    """

    def _emit(msg: str) -> None:
        if log_cb is None:
            return
        try:
            log_cb(msg)
        except Exception:
            pass

    def _emit_winget_line(line: str, *, realtime: bool = False) -> None:
        stripped = _normalize_stripped(line)
        if not stripped:
            return
        if _try_parse_pct_marker(stripped, progress_cb):
            return
        if _is_noise_line(stripped):
            return
        if is_winget_table_chrome(stripped):
            return
        if _try_parse_progress(stripped, progress_cb):
            return
        _emit((REALTIME_LOG_PREFIX + line) if realtime else line)

    def process(line: str, collector: list[str], is_stderr: bool) -> None:
        if line is None:
            return
        collector.append(line)
        try:
            stripped = _normalize_stripped(line)
            if not stripped:
                return
            if _try_parse_pct_marker(stripped, progress_cb):
                return
            marker_payload = _try_parse_log_marker(stripped)
            if marker_payload is not None:
                if marker_payload:
                    _emit_winget_line(marker_payload, realtime=True)
                return
            _emit_winget_line(line)
        except Exception:
            return

    return process


def read_stream(
    stream,
    collector: list[str],
    is_stderr: bool,
    process_line: Callable[[str, list[str], bool], None],
) -> None:
    """Lê o stream até EOF, separando linhas (ANSI **ou** UTF-16LE) em tempo real."""
    buffer = bytearray()
    try:
        while True:
            try:
                chunk = stream.read1(4096)
            except Exception:
                chunk = stream.read(4096)
            if not chunk:
                break
            buffer.extend(chunk)

            while True:
                if _looks_like_utf16le(buffer):
                    split = _split_utf16le_line(buffer)
                else:
                    split = _split_ansi_line(buffer)
                if split is None:
                    break
                line_bytes, consumed = split
                del buffer[:consumed]
                process_line(decode_bytes(line_bytes), collector, is_stderr)

        if buffer:
            tail = decode_bytes(bytes(buffer))
            buffer.clear()
            for piece in tail.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                process_line(piece, collector, is_stderr)
    except Exception:
        return
