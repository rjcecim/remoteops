"""Resolução de códigos de saída Win32/HRESULT via ``FormatMessage`` (sem tabela fixa)."""

from __future__ import annotations

import sys
from ctypes import WinDLL, create_unicode_buffer, wintypes
from dataclasses import dataclass
from typing import Literal

SourceKind = Literal["winapi", "unknown"]

# FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS
_FMT_SYS = 0x00001000 | 0x00000200

_FACILITY_WIN32 = 7
_HRESULT_WIN32_MASK = 0xFFFF0000
_HRESULT_WIN32_PREFIX = (_FACILITY_WIN32 << 16) | 0x80000000

_kernel32_module: WinDLL | None = None


def _kernel32() -> WinDLL:
    global _kernel32_module
    if _kernel32_module is None:
        k = WinDLL("kernel32", use_last_error=True)
        k.FormatMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        k.FormatMessageW.restype = wintypes.DWORD
        _kernel32_module = k
    return _kernel32_module


@dataclass(frozen=True)
class ResolvedExitCode:
    """Resultado da resolução de um código retornado pelo processo (ex.: PsExec)."""

    exit_code: int
    message: str
    source: SourceKind


def _try_format_message(message_id: int) -> str | None:
    kernel32 = _kernel32()
    buf = create_unicode_buffer(4096)
    nchars = int(
        kernel32.FormatMessageW(
            _FMT_SYS,
            None,
            wintypes.DWORD(message_id & 0xFFFFFFFF),
            0,
            buf,
            wintypes.DWORD(len(buf)),
            None,
        )
    )
    if nchars == 0:
        return None
    text = (buf.value or "").strip().rstrip("\r\n")
    return text or None


def _is_win32_wrapped_hresult(code_u: int) -> bool:
    return (code_u & _HRESULT_WIN32_MASK) == _HRESULT_WIN32_PREFIX


def resolve_windows_exit_code(exit_code: int) -> ResolvedExitCode:
    """Consulta o Windows por uma mensagem para ``exit_code`` (Win32 e HRESULT).

    Não levanta exceções: em falha ou plataforma não suportada, devolve ``source="unknown"``.
    """
    code_u = exit_code & 0xFFFFFFFF

    if sys.platform != "win32":
        return ResolvedExitCode(
            exit_code=exit_code,
            message=f"Sem FormatMessage (não-Windows): código {exit_code} (0x{code_u:08X}).",
            source="unknown",
        )

    try:
        msg = _try_format_message(code_u)
        if msg:
            return ResolvedExitCode(exit_code=exit_code, message=msg, source="winapi")

        if _is_win32_wrapped_hresult(code_u):
            msg = _try_format_message(code_u & 0xFFFF)
            if msg:
                return ResolvedExitCode(exit_code=exit_code, message=msg, source="winapi")

        return ResolvedExitCode(
            exit_code=exit_code,
            message=f"Nenhuma mensagem do sistema para o código {exit_code} (0x{code_u:08X}).",
            source="unknown",
        )
    except Exception:
        return ResolvedExitCode(
            exit_code=exit_code,
            message=f"Não foi possível resolver 0x{code_u:08X} ({exit_code}).",
            source="unknown",
        )
