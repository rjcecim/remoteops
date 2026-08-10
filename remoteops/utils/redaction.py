"""
Política central de proteção de credenciais.

Toda representação destinada a UI, logs, histórico ou arquivos temporários
deve passar por estas funções. Não espalhe mascaramento ad-hoc pelo código.

A flag de senha do PsExec é ``-p`` (ou ``/p``) **isolada**, seguida do valor.
Argumentos como ``-Path``, ``-Profile``, ``-Priority``, ``-NoProfile`` NÃO
são senha e não devem ser mascarados.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

# Marcador padrão usado em preview/logs
REDACTED = "********"

# Apenas a flag isolada -p / -P (ou /p), com espaço ou '=', nunca "-Path" etc.
# Lookahead negativo: após p/P não pode vir letra (evita -Path/-Profile/-Priority).
_PASSWORD_FLAG_RE = re.compile(
    r'(?P<prefix>(?:^|[\s"])(?:-|/)p(?![A-Za-z])(?:\s+|=))'
    r'(?P<value>"[^"]*"|\'[^\']*\'|[^\s"\']+)',
    re.IGNORECASE,
)


def mask_password(password: Optional[str], placeholder: str = REDACTED) -> str:
    """Retorna o placeholder se houver senha; string vazia se não houver."""
    if password is None or str(password).strip() == "":
        return ""
    return placeholder


def _is_password_flag(arg: str) -> bool:
    """True somente para ``-p`` / ``/p`` exatos (case-insensitive)."""
    return arg.lower() in ("-p", "/p")


def _is_password_flag_equals(arg: str) -> bool:
    """True para ``-p=valor`` / ``/p=valor`` (não ``-Path=...``)."""
    lower = arg.lower()
    return lower.startswith("-p=") or lower.startswith("/p=")


def redact_command_text(
    text: str,
    passwords: Optional[Iterable[str]] = None,
    placeholder: str = REDACTED,
) -> str:
    """
    Sanitiza uma string de comando/log para exibição.

    1. Substitui valores explícitos de ``passwords`` (se fornecidos).
    2. Mascara o valor da flag ``-p`` / ``/p`` isolada (defesa em profundidade).
    """
    if not text:
        return text or ""

    result = text

    if passwords:
        # Senhas mais longas primeiro evita mascaramento parcial incorreto
        for pwd in sorted({p for p in passwords if p}, key=len, reverse=True):
            if not pwd:
                continue
            result = result.replace(pwd, placeholder)

    def _replace_flag(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{placeholder}"

    result = _PASSWORD_FLAG_RE.sub(_replace_flag, result)
    return result


def redact_argv(
    argv: Sequence[str],
    placeholder: str = REDACTED,
) -> list[str]:
    """
    Sanitiza uma lista de argumentos (estilo subprocess).

    Trata somente ``-p`` / ``/p`` isolados (próximo elemento = senha) ou
    ``-p=`` / ``/p=``. Não altera ``-Path``, ``-Profile``, ``-Priority``, etc.
    """
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            out.append(placeholder)
            skip_next = False
            continue
        if _is_password_flag(arg):
            out.append(arg)
            skip_next = True
            continue
        if _is_password_flag_equals(arg):
            # Preserva o prefixo "-p=" ou "/p="
            out.append(f"{arg[:3]}{placeholder}")
            continue
        out.append(arg)
    # Flag -p no final sem valor: não inventa placeholder extra
    return out


def format_argv_for_display(argv: Sequence[str], placeholder: str = REDACTED) -> str:
    """Junta argv sanitizado em string legível para preview/log."""
    return " ".join(quote_arg_for_display(a) for a in redact_argv(argv, placeholder))


def quote_arg_for_display(arg: str) -> str:
    """Aspas simples para display quando há espaços (não para execução)."""
    if not arg:
        return '""'
    if any(ch in arg for ch in (" ", "\t", '"')):
        escaped = arg.replace('"', '\\"')
        return f'"{escaped}"'
    return arg


def assert_no_secrets(text: str, passwords: Optional[Iterable[str]] = None) -> str:
    """
    Garante que o texto não contém senhas conhecidas; sempre aplica redaction.
    Útil como última barreira antes de gravar em disco ou UI.
    """
    return redact_command_text(text, passwords=passwords)
