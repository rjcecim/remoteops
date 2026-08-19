from __future__ import annotations

"""
Integração básica com efeito Mica / backdrop do Windows 11 para janelas Qt.

Não depende de bibliotecas externas (usa apenas ctypes + WinAPI).
Se o recurso não estiver disponível (ex.: Windows 10), a função falha em silêncio.
"""

import ctypes
import sys
from ctypes import wintypes

from PyQt6.QtWidgets import QWidget


def _is_windows_11_or_newer() -> bool:
    try:
        # Em Python 3.13+ a API de versão mudou, mas sys.getwindowsversion continua existindo
        v = sys.getwindowsversion()  # type: ignore[attr-defined]
        # Build 22000+ é Windows 11
        return getattr(v, "build", 0) >= 22000
    except Exception:
        return False


def enable_mica_for_widget(widget: QWidget) -> None:
    """
    Tenta habilitar Mica (ou efeito de backdrop similar) na janela que contém o widget.
    Seguro para ser chamado várias vezes; ignora falhas silenciosamente.
    """
    if sys.platform != "win32":
        return

    if not _is_windows_11_or_newer():
        # Em Windows 10 ou inferior, não tenta aplicar Mica
        return

    try:
        hwnd = int(widget.window().winId())
    except Exception:
        return

    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        return

    # Constantes da WinAPI (nem todas existem em headers antigos, por isso definidas aqui)
    DWMWA_SYSTEMBACKDROP_TYPE = 38  # Windows 11 22H2+

    # Tipos de backdrop: 2 = Mica regular, 3 = Mica Alt
    DWMSBT_MAINWINDOW = 2

    DwmSetWindowAttribute = dwmapi.DwmSetWindowAttribute
    DwmSetWindowAttribute.restype = wintypes.HRESULT
    DwmSetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        wintypes.LPCVOID,
        wintypes.DWORD,
    ]

    def _set_attr(attr: int, value: int) -> None:
        val = ctypes.c_int(value)
        DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(attr),
            ctypes.byref(val),
            ctypes.sizeof(val),
        )

    # Apenas aplica o backdrop Mica; não força dark mode.
    # Title bar clara para combinar com o tema claro da UI.
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    try:
        _set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 0)
    except Exception:
        pass
    try:
        _set_attr(DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW)
    except Exception:
        # Não falha o app se o recurso não estiver disponível
        return

