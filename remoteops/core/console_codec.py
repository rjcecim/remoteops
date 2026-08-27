"""Decodificação canónica de bytes de console Windows (pipes / OEM / UTF-16)."""

from __future__ import annotations


def _oem_encoding() -> str:
    try:
        import ctypes

        cp = int(ctypes.windll.kernel32.GetOEMCP())
        if cp in (65001, 20127):
            return "utf-8"
        if cp > 0:
            return f"cp{cp}"
    except Exception:
        pass
    return ""


def decode_console_bytes(data: bytes) -> str:
    """Decodifica saída de console: BOM UTF-16/UTF-8, UTF-16LE sem BOM, UTF-8, OEM, ANSI.

    ConPTY entrega UTF-8. Pipes de ``cmd.exe`` usam a OEM code page (GetOEMCP).
    CLIXML / PowerShell redirecionado pode ser UTF-16LE sem BOM (bytes ``\\x00``).
    """
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except Exception:
            pass
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    # Heurística UTF-16LE sem BOM (ex.: CLIXML do PowerShell via PsExec).
    if data.count(b"\x00") >= max(2, len(data) // 8):
        try:
            return data.decode("utf-16le", errors="replace")
        except Exception:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    oem = _oem_encoding()
    if oem:
        try:
            return data.decode(oem, errors="replace")
        except Exception:
            pass
    for enc in ("oem", "mbcs", "cp850", "cp1252"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1", errors="replace")


# Alias histórico usado pelo Executor e batch install.
decode_best_effort = decode_console_bytes
