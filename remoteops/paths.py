"""Resolução de caminhos do aplicativo (dev e executável empacotado)."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """
    Raiz portátil do app:
    - exe PyInstaller: pasta do .exe
    - desenvolvimento: pasta RemoteOps/ (pai do pacote ``remoteops``)
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def meipass_dir() -> Path | None:
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def assets_dir() -> Path:
    bundle = meipass_dir()
    if bundle is not None:
        return bundle / "assets"
    return project_root() / "assets"


def config_dir() -> Path:
    bundle = meipass_dir()
    if bundle is not None:
        # Preferir config ao lado do exe (editável); fallback no bundle.
        beside = project_root() / "config"
        if beside.is_dir():
            return beside
        return bundle / "config"
    return project_root() / "config"
