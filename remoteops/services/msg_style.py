"""Estilos visuais possíveis no diálogo do ``msg.exe``.

O comando não aceita RTF, HTML nem fonte. Negrito e itálico usam
alfabeto matemático Unicode; sublinhado usa o combinante U+0332.
O limite de 255 do ``msg.exe`` conta unidades UTF-16.
"""

from __future__ import annotations

import unicodedata
from typing import Iterator, Optional

STYLE_BOLD = "bold"
STYLE_ITALIC = "italic"
STYLE_UNDERLINE = "underline"
STYLE_NONE = "none"

COMBINING_UNDERLINE = "\u0332"

# Itálico 'h' não existe em U+1D455 (reservado); o Unicode usa ℎ.
_ITALIC_H = "\u210E"


def _latin_map(start: int, source: str) -> dict[str, str]:
    return {src: chr(start + i) for i, src in enumerate(source)}


_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"

_BOLD_UPPER = _latin_map(0x1D400, _UPPER)
_BOLD_LOWER = _latin_map(0x1D41A, _LOWER)
_ITALIC_UPPER = _latin_map(0x1D434, _UPPER)
_ITALIC_LOWER = _latin_map(0x1D44E, _LOWER)
_ITALIC_LOWER["h"] = _ITALIC_H
_BOLD_ITALIC_UPPER = _latin_map(0x1D468, _UPPER)
_BOLD_ITALIC_LOWER = _latin_map(0x1D482, _LOWER)
_BOLD_DIGIT = _latin_map(0x1D7CE, _DIGITS)

_FORWARD: dict[tuple[str, bool, bool], str] = {}
_REVERSE: dict[str, tuple[str, bool, bool]] = {}


def _register(src: str, styled: str, *, bold: bool, italic: bool) -> None:
    _FORWARD[(src, bold, italic)] = styled
    _REVERSE[styled] = (src, bold, italic)


for _ch, _st in _BOLD_UPPER.items():
    _register(_ch, _st, bold=True, italic=False)
for _ch, _st in _BOLD_LOWER.items():
    _register(_ch, _st, bold=True, italic=False)
for _ch, _st in _ITALIC_UPPER.items():
    _register(_ch, _st, bold=False, italic=True)
for _ch, _st in _ITALIC_LOWER.items():
    _register(_ch, _st, bold=False, italic=True)
for _ch, _st in _BOLD_ITALIC_UPPER.items():
    _register(_ch, _st, bold=True, italic=True)
for _ch, _st in _BOLD_ITALIC_LOWER.items():
    _register(_ch, _st, bold=True, italic=True)
for _ch, _st in _BOLD_DIGIT.items():
    _register(_ch, _st, bold=True, italic=False)


def msg_unit_count(text: str) -> int:
    """Unidades UTF-16 — o limite documentado do ``msg.exe``."""
    return len((text or "").encode("utf-16-le")) // 2


def clip_message_text(text: str, limit: int) -> str:
    """Corta sem partir pares substitutos UTF-16."""
    if not text or limit <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        units = 2 if ord(ch) > 0xFFFF else 1
        if used + units > limit:
            break
        out.append(ch)
        used += units
    return "".join(out)


def _clusters(text: str) -> Iterator[str]:
    buf: list[str] = []
    for ch in text or "":
        if buf and unicodedata.combining(ch):
            buf.append(ch)
            continue
        if buf:
            yield "".join(buf)
        buf = [ch]
    if buf:
        yield "".join(buf)


def _decode_cluster(cluster: str) -> tuple[str, str, bool, bool, bool]:
    """Retorna (ascii_ou_base, marcas, bold, italic, underline)."""
    base, marks = cluster[0], cluster[1:]
    underline = COMBINING_UNDERLINE in marks
    if underline:
        marks = marks.replace(COMBINING_UNDERLINE, "")
    info = _REVERSE.get(base)
    if info is not None:
        ascii_ch, bold, italic = info
        return ascii_ch, marks, bold, italic, underline
    decomp = unicodedata.normalize("NFKD", base)
    if decomp and decomp[0].isascii() and decomp[0].isalnum() and decomp != base:
        return decomp[0], decomp[1:] + marks, False, False, underline
    return base, marks, False, False, underline


def _encode_cluster(
    ascii_ch: str,
    marks: str,
    *,
    bold: bool,
    italic: bool,
    underline: bool,
) -> str:
    styled = ascii_ch
    if ascii_ch.isascii() and ascii_ch.isalnum():
        if ascii_ch.isdigit():
            styled = _FORWARD.get((ascii_ch, True, False), ascii_ch) if bold else ascii_ch
        else:
            styled = _FORWARD.get((ascii_ch, bold, italic), ascii_ch)
    result = styled + marks
    if underline:
        result += COMBINING_UNDERLINE
    if not bold and not italic:
        result = unicodedata.normalize("NFC", result)
    return result


def inspect_message_style(text: str) -> dict[str, Optional[bool]]:
    """Estado homogêneo da seleção: True/False, ou None se misturado."""
    found = False
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    for cluster in _clusters(text):
        ascii_ch, _marks, is_bold, is_italic, is_under = _decode_cluster(cluster)
        if not (ascii_ch.isascii() and ascii_ch.isalnum()):
            continue
        if not found:
            bold, italic, underline = is_bold, is_italic, is_under
            found = True
            continue
        if bold is not None and bold != is_bold:
            bold = None
        if italic is not None and italic != is_italic:
            italic = None
        if underline is not None and underline != is_under:
            underline = None
        if bold is None and italic is None and underline is None:
            break
    if not found:
        return {"bold": False, "italic": False, "underline": False}
    return {"bold": bold, "italic": italic, "underline": underline}


def apply_message_style(text: str, style: str, *, toggle: bool = True) -> str:
    """Aplica ou remove um estilo no trecho. ``none`` restaura o texto simples."""
    kind = (style or "").strip().lower()
    if not text:
        return ""
    if kind == STYLE_NONE:
        toggle = False
        target_bold = False
        target_italic = False
        target_underline = False
    else:
        current = inspect_message_style(text)
        if kind == STYLE_BOLD:
            target_bold = False if (toggle and current.get("bold") is True) else True
            target_italic = None
            target_underline = None
        elif kind == STYLE_ITALIC:
            target_bold = None
            target_italic = False if (toggle and current.get("italic") is True) else True
            target_underline = None
        elif kind == STYLE_UNDERLINE:
            target_bold = None
            target_italic = None
            target_underline = (
                False if (toggle and current.get("underline") is True) else True
            )
        else:
            return text

    out: list[str] = []
    for cluster in _clusters(text):
        ascii_ch, marks, is_bold, is_italic, is_under = _decode_cluster(cluster)
        out.append(
            _encode_cluster(
                ascii_ch,
                marks,
                bold=is_bold if target_bold is None else target_bold,
                italic=is_italic if target_italic is None else target_italic,
                underline=is_under if target_underline is None else target_underline,
            )
        )
    return "".join(out)
