"""Conversão de CLIXML (saída serializada do PowerShell) em texto legível."""

from __future__ import annotations

import re

_S_TAG_RE = re.compile(r"<S(?:\s+[^>]*)?>(.*?)</S>", flags=re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def clixml_to_text(text: str) -> str:
    """PowerShell pode emitir erros como ``#< CLIXML ...``.

    Essa função tenta extrair um texto humano aproximado a partir dos nós
    ``<S>...</S>`` do CLIXML. Se não parecer CLIXML, devolve o texto inalterado.
    """
    t = (text or "").strip()
    if "#< CLIXML" not in t:
        return t

    body = t.split("#< CLIXML", 1)[-1].replace("\r\n", "\n").replace("\r", "\n")
    parts = _S_TAG_RE.findall(body)

    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        norm = _WS_RE.sub(" ", part or "").strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(norm)

    return "\n".join(cleaned).strip() if cleaned else t
