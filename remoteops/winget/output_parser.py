"""Extração do JSON de retorno do host remoto a partir de ``stdout``/``stderr``."""

from __future__ import annotations

import base64

from .constants import MARKER_B64_BEGIN, MARKER_B64_END, MARKER_JSON_BEGIN, MARKER_JSON_END


def _find_all(text: str, needle: str) -> list[int]:
    out: list[int] = []
    pos = 0
    while True:
        k = text.find(needle, pos)
        if k < 0:
            return out
        out.append(k)
        pos = k + len(needle)


def extract_between(text: str, begin: str, end: str) -> str | None:
    """Devolve o bloco entre ``begin`` e ``end``. Se houver múltiplos ``begin``,
    tenta o mais próximo do final (última ocorrência que rende algo não-vazio).
    """
    t = text or ""
    if begin not in t:
        return None
    for start in reversed(_find_all(t, begin)):
        a = start + len(begin)
        b = t.find(end, a)
        block = t[a:] if b < 0 else t[a:b]
        lines = [x for x in block.splitlines() if (x or "").strip()]
        payload = "\n".join(lines).strip()
        if payload:
            return payload
    return ""


def extract_b64_json(text: str) -> str | None:
    """Converte o payload Base64 marcado em JSON (texto)."""
    raw = extract_between(text, MARKER_B64_BEGIN, MARKER_B64_END)
    if not raw:
        return None
    b64 = "".join(ch for ch in raw if ch.isalnum() or ch in "+/=")
    if not b64:
        return None
    try:
        data = base64.b64decode(b64, validate=False)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def extract_marked_json(text: str) -> str | None:
    return extract_between(text, MARKER_JSON_BEGIN, MARKER_JSON_END)


def find_last_json_like_line(stdout: str, stderr: str) -> str | None:
    for line in (stdout.splitlines() + stderr.splitlines())[::-1]:
        s = (line or "").strip()
        if s.startswith("{") and s.endswith("}"):
            return s
    return None


def pick_json_blob(stdout: str, stderr: str, file_json: str | None) -> str | None:
    """Tenta, em ordem, extrair: B64 marcado, JSON marcado, última linha tipo-JSON,
    conteúdo do arquivo remoto (resgatado via SMB).
    """
    marked = extract_b64_json(stdout) or extract_b64_json(stderr)
    if marked:
        return marked
    marked = extract_marked_json(stdout) or extract_marked_json(stderr)
    if marked:
        return marked
    last = find_last_json_like_line(stdout, stderr)
    if last:
        return last
    return file_json
