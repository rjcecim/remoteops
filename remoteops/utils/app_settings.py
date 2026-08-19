"""Configurações portáteis em settings.ini (ao lado do exe / raiz do repo)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import QSettings

from remoteops.paths import project_root

SETTINGS_SAVE_ERROR_MSG = (
    "Não foi possível salvar settings.ini. "
    "Verifique se a pasta do aplicativo permite gravação."
)

# Chaves conhecidas (somente preferências não sensíveis)
KEY_PSTOOLS_DIR = "tools/pstools_dir"
KEY_SEARCH_MAX_WORKERS = "search/max_workers"
KEY_SEARCH_HOSTS_PATH = "search/hosts_path"
KEY_LOGS_FILE_ENABLED = "logs/file_logging_enabled"
KEY_REMOTE_REGISTRY_TIMEOUT = "timeouts/remote_registry_seconds"


class SettingsWriteError(OSError):
    """Falha ao gravar settings.ini (pasta somente leitura / acesso negado)."""

    def __init__(self, message: str = SETTINGS_SAVE_ERROR_MSG):
        super().__init__(message)
        self.message = message


def get_app_dir() -> Path:
    return project_root()


def get_settings_path() -> Path:
    return get_app_dir() / "settings.ini"


def create_settings() -> QSettings:
    return QSettings(str(get_settings_path()), QSettings.Format.IniFormat)


def load_setting(key: str, default: Any = None) -> Any:
    """Lê uma chave do settings.ini. Se o arquivo não existir, retorna default."""
    if not get_settings_path().is_file():
        return default
    try:
        settings = create_settings()
        return settings.value(key, default)
    except Exception:
        return default


def _check_sync_status(settings: QSettings) -> None:
    status = settings.status()
    if status == QSettings.Status.AccessError:
        raise SettingsWriteError(SETTINGS_SAVE_ERROR_MSG)
    if status == QSettings.Status.FormatError:
        raise SettingsWriteError(SETTINGS_SAVE_ERROR_MSG)
    if status != QSettings.Status.NoError:
        raise SettingsWriteError(SETTINGS_SAVE_ERROR_MSG)


def save_setting(key: str, value: Any) -> None:
    """
    Grava uma chave e, em seguida, o snapshot completo das preferências.
    Assim o settings.ini sempre reflete todas as configurações atuais.
    """
    save_portable_settings({key: value})


def _collect_current_settings() -> Dict[str, Any]:
    """Monta o snapshot das preferências atuais (imports locais evitam ciclos)."""
    from remoteops.utils.app_logging import is_file_logging_enabled
    from remoteops.utils.network_range import (
        KEY_NET_ENABLED,
        KEY_NET_END_IP,
        KEY_NET_IGNORED_SUBNETS,
        KEY_NET_SCAN_THREADS,
        KEY_NET_START_IP,
        get_network_range_config,
    )
    from remoteops.utils.pstools import get_pstools_dir
    from remoteops.utils.remote_registry_query import get_remote_registry_timeout
    from remoteops.utils.search_settings import get_search_hosts_path, get_search_max_workers

    net = get_network_range_config()
    return {
        KEY_PSTOOLS_DIR: get_pstools_dir(),
        KEY_SEARCH_MAX_WORKERS: int(get_search_max_workers()),
        KEY_SEARCH_HOSTS_PATH: get_search_hosts_path(),
        KEY_LOGS_FILE_ENABLED: bool(is_file_logging_enabled()),
        KEY_REMOTE_REGISTRY_TIMEOUT: float(get_remote_registry_timeout()),
        KEY_NET_ENABLED: bool(net.enabled),
        KEY_NET_START_IP: net.start_ip,
        KEY_NET_END_IP: net.end_ip,
        KEY_NET_IGNORED_SUBNETS: net.ignored_subnets,
        KEY_NET_SCAN_THREADS: int(net.scan_threads),
    }


def save_portable_settings(updates: Optional[Dict[str, Any]] = None) -> None:
    """
    Grava o snapshot completo em settings.ini.

    ``updates`` sobrescreve valores do snapshot (útil antes de atualizar o runtime).
    Em falha, não usa Registro nem pastas de usuário do Windows.
    """
    values = _collect_current_settings()
    if updates:
        values.update(updates)

    try:
        settings = create_settings()
        for key, value in values.items():
            settings.setValue(key, value)
        settings.sync()
        _check_sync_status(settings)
    except SettingsWriteError:
        raise
    except Exception as exc:
        raise SettingsWriteError(SETTINGS_SAVE_ERROR_MSG) from exc
