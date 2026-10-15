"""Pipeline de saída de console: decode incremental, ANSI, CR e redação.

Bytes → texto → strip ANSI (estado) → redação → linhas/parciais → UI.
Mantém estado entre chunks para UTF-8, escapes e senhas fragmentadas.
"""

from __future__ import annotations

import codecs
import re
from typing import Callable, Iterable, List, Optional, Sequence

from remoteops.utils.redaction import REDACTED, redact_command_text

# CSI incompleto no fim do chunk — aguarda o terminador.
_INCOMPLETE_ANSI_RE = re.compile(
    r"(?:"
    r"\x1b$|"
    r"\x1b\][^\x07\x1b]*$|"
    r"\x1b\[[0-9;?]*[ -/]*$|"
    r"\x1b[P^_][^\x1b]*$"
    r")\Z"
)

_ANSI_RE = re.compile(
    r"(?:"
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b[P^_][^\x1b]*\x1b\\"
    r"|\x1b[@-Z\\-_]"
    r"|\x00"
    r")"
)

_CUP_RE = re.compile(r"\x1b\[\d+;\d+H")


def strip_ansi(text: str, *, normalize_cr: bool = False) -> str:
    """Remove sequências VT/ANSI. Por padrão preserva ``\\r`` (progresso)."""
    if not text:
        return ""
    text = _CUP_RE.sub("\n", text)
    text = _ANSI_RE.sub("", text)
    if normalize_cr:
        return text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\r\n", "\n")


class IncrementalUtf8Decoder:
    """Decodifica UTF-8 por chunks sem corromper caracteres divididos."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> str:
        if not data:
            return ""
        return self._decoder.decode(data, final=False)

    def finish(self) -> str:
        return self._decoder.decode(b"", final=True)


class StreamingAnsiFilter:
    """Remove ANSI com carry de sequências incompletas entre leituras."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        if not text and not self._pending:
            return ""
        data = self._pending + (text or "")
        self._pending = ""
        match = _INCOMPLETE_ANSI_RE.search(data)
        if match:
            self._pending = match.group(0)
            data = data[: match.start()]
        return strip_ansi(data, normalize_cr=False)

    def finish(self) -> str:
        leftover = self._pending
        self._pending = ""
        return strip_ansi(leftover, normalize_cr=False) if leftover else ""


def _dangling_password_prefix(text: str, passwords: Sequence[str]) -> str:
    """Maior sufixo de ``text`` que é prefixo próprio (incompleto) de alguma senha."""
    if not text or not passwords:
        return ""
    best = ""
    for pwd in passwords:
        if not pwd or len(pwd) < 2:
            continue
        # Prefixo próprio: 1..(len-1); senha completa já é coberta por redact.
        upper = min(len(pwd), len(text) + 1)
        for n in range(1, upper):
            prefix = pwd[:n]
            if text.endswith(prefix) and len(prefix) > len(best):
                best = prefix
    return best


class StreamRedactor:
    """Redação com cauda: senhas cortadas entre chunks ainda são mascaradas."""

    def __init__(
        self,
        passwords: Optional[Iterable[str]] = None,
        *,
        placeholder: str = REDACTED,
    ) -> None:
        self._passwords = [p for p in (passwords or []) if p]
        self._placeholder = placeholder
        self._tail = ""

    def update_passwords(self, passwords: Optional[Iterable[str]]) -> None:
        self._passwords = [p for p in (passwords or []) if p]

    def feed(self, text: str) -> str:
        if not text and not self._tail:
            return text or ""
        combined = self._tail + (text or "")
        dangling = _dangling_password_prefix(combined, self._passwords)
        if dangling:
            emit_src = combined[: len(combined) - len(dangling)]
            self._tail = dangling
        else:
            emit_src = combined
            self._tail = ""
        if not emit_src:
            return ""
        return redact_command_text(
            emit_src, passwords=self._passwords, placeholder=self._placeholder
        )

    def finish(self) -> str:
        leftover = self._tail
        self._tail = ""
        if not leftover:
            return ""
        return redact_command_text(
            leftover, passwords=self._passwords, placeholder=self._placeholder
        )


class ConPtyTextProcessor:
    """Estado do caminho ConPTY: UTF-8 → ANSI → (texto pronto para linhas)."""

    def __init__(self) -> None:
        self._utf8 = IncrementalUtf8Decoder()
        self._ansi = StreamingAnsiFilter()

    def feed_bytes(self, data: bytes) -> str:
        return self._ansi.feed(self._utf8.feed(data))

    def finish(self) -> str:
        return self._ansi.feed(self._utf8.finish()) + self._ansi.finish()


def emit_conpty_text(
    text: str,
    *,
    prefix: str = "",
    on_line: Callable[[str], None],
    carry: List[str],
    on_partial: Optional[Callable[[str], None]] = None,
    keep_blank_lines: bool = False,
) -> None:
    """Converte pedaços ConPTY em linhas; ``\\r`` atualiza a linha atual."""
    if not text:
        return
    data = (carry[0] if carry else "") + text
    carry.clear()
    parts = data.split("\n")
    if not data.endswith("\n"):
        carry.append(parts[-1])
        parts = parts[:-1]
    for part in parts:
        line = part.split("\r")[-1]
        if not keep_blank_lines and not line.strip():
            continue
        on_line(f"{prefix}{line}" if prefix else line)
    if on_partial is not None:
        partial = carry[0] if carry else ""
        if partial:
            # Progresso: só o segmento após o último CR.
            partial = partial.split("\r")[-1]
        on_partial(f"{prefix}{partial}" if (prefix and partial) else partial)


# Alias histórico (executor / batch install).
_emit_conpty_text = emit_conpty_text


def sanitize_output_text(
    text: str,
    passwords: Optional[Sequence[str]] = None,
) -> str:
    """Última barreira antes de UI/log para texto já decodificado."""
    return redact_command_text(text or "", passwords=passwords)
