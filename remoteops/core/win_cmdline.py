"""
Parsing de command line no estilo Windows.

Usa CommandLineToArgvW (Shell32) — as mesmas regras que o CRT/CreateProcess
aplicam a lpCommandLine. Não usa str.split() nem shlex POSIX.
"""

from __future__ import annotations

import sys
from typing import List


def split_windows_command_line(command_line: str) -> List[str]:
    """
    Divide uma string de command line Windows em lista de argumentos.

    Exemplos (conceituais)::

        ipconfig /all
            → ["ipconfig", "/all"]

        cmd /c "dir C:\\Program Files"
            → ["cmd", "/c", "dir C:\\Program Files"]

        "C:\\Program Files\\app.exe" /S
            → ["C:\\Program Files\\app.exe", "/S"]

    Em Windows, delega a ``CommandLineToArgvW`` e libera o buffer com
    ``LocalFree``. Fora do Windows, levanta ``OSError`` (este app é Windows-only).

    Sintaxe que depende do interpretador CMD (``&&``, redirecionamento, etc.)
    deve ser explícita via ``cmd /c ...`` — esta função não envolve o shell.
    """
    if command_line is None:
        return []
    text = str(command_line).strip()
    if not text:
        return []

    if sys.platform != "win32":
        raise OSError(
            "split_windows_command_line requer Windows (CommandLineToArgvW)"
        )

    import ctypes
    from ctypes import wintypes

    command_line_to_argv_w = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv_w.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    command_line_to_argv_w.restype = ctypes.POINTER(wintypes.LPWSTR)

    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL

    argc = ctypes.c_int(0)
    argv_ptr = command_line_to_argv_w(text, ctypes.byref(argc))
    if not argv_ptr:
        raise OSError("CommandLineToArgvW falhou ao analisar a command line")

    try:
        return [argv_ptr[i] for i in range(argc.value)]
    finally:
        local_free(argv_ptr)
