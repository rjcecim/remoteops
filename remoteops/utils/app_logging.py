"""Logging seguro da aplicação (nunca registra senhas)."""

from __future__ import annotations

import datetime
import logging
import os
from typing import Iterable, Optional

from remoteops.utils.redaction import redact_command_text

_LOGGER_NAME = "remoteops"
_configured = False
# Desligado por padrão; preferência pode ser persistida em settings.ini
_file_logging_enabled = False
_file_logging_loaded = False

# Único arquivo de log em disco (pasta logs/ ao lado do app)
LOG_FILENAME = "app.log"


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "on")


def get_log_dir(*, create: bool = True) -> str:
    """
    Diretório de logs portátil: ``<pasta do app>/logs``.
    Em desenvolvimento: raiz do repositório; no exe: pasta do RemoteOps.exe.
    """
    from remoteops.utils.app_settings import get_app_dir

    path = str(get_app_dir() / "logs")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def get_log_file_path() -> str:
    return os.path.join(get_log_dir(), LOG_FILENAME)


# Compat com código/UI antigos
def get_app_log_path() -> str:
    return get_log_file_path()


def get_history_log_path() -> str:
    return get_log_file_path()


def is_file_logging_enabled() -> bool:
    """Se True, grava app.log (preferência em settings.ini)."""
    global _file_logging_enabled, _file_logging_loaded
    if not _file_logging_loaded:
        try:
            from remoteops.utils.app_settings import KEY_LOGS_FILE_ENABLED, load_setting

            _file_logging_enabled = _parse_bool(
                load_setting(KEY_LOGS_FILE_ENABLED, False)
            )
        except Exception:
            _file_logging_enabled = False
        _file_logging_loaded = True
    return _file_logging_enabled


def set_file_logging_enabled(enabled: bool, *, persist: bool = True) -> bool:
    """
    Liga/desliga gravação em arquivo e persiste em settings.ini.
    Em falha de gravação, mantém o valor anterior e propaga SettingsWriteError.
    """
    global _file_logging_enabled, _file_logging_loaded
    current = is_file_logging_enabled()
    enabled = bool(enabled)
    if persist:
        from remoteops.utils.app_settings import KEY_LOGS_FILE_ENABLED, save_portable_settings

        save_portable_settings({KEY_LOGS_FILE_ENABLED: enabled})
    _file_logging_enabled = enabled
    _file_logging_loaded = True
    if enabled:
        _ensure_file_handler()
    elif current or _configured:
        _remove_file_handlers()
    return enabled


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """
    Configura o logger da aplicação.

    FileHandler só é criado se a preferência de log em arquivo estiver ativa.
    Tudo vai para um único ``logs/app.log`` na pasta do aplicativo.
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    _configured = True
    if is_file_logging_enabled():
        _ensure_file_handler()
    return logger


def get_logger() -> logging.Logger:
    if not _configured:
        return configure_logging()
    return logging.getLogger(_LOGGER_NAME)


def _ensure_file_handler() -> None:
    logger = get_logger()
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            return
    try:
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = logging.FileHandler(get_log_file_path(), encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(logger.level)
        logger.addHandler(fh)
    except OSError:
        pass


def _remove_file_handlers() -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def _append_log_line(text: str) -> None:
    """Única escrita direta em logs/app.log (além do FileHandler do logger)."""
    if not _file_logging_enabled:
        return
    try:
        path = get_log_file_path()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {text}\n")
    except OSError:
        pass


def log_operation(
    operation: str,
    *,
    detail: str = "",
    exit_code: Optional[int] = None,
    passwords: Optional[Iterable[str]] = None,
    level: int = logging.INFO,
) -> str:
    """
    Registra uma operação já sanitizada.

    Escreve uma única vez em ``logs/app.log`` quando o log em arquivo está ativo.
    """
    safe_detail = redact_command_text(detail or "", passwords=passwords)
    parts = [operation]
    if safe_detail:
        parts.append(safe_detail)
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    message = " | ".join(parts)
    if _file_logging_enabled:
        # Uma única gravação (evita duplicar via FileHandler + append)
        _append_log_line(message)
    return message


def append_history(
    text: str,
    passwords: Optional[Iterable[str]] = None,
) -> None:
    """API pública para histórico livre (UI). Preferir ``log_operation`` quando tipado."""
    if not _file_logging_enabled:
        return
    safe = redact_command_text(text or "", passwords=passwords)
    _append_log_line(safe)


def reset_logging_for_tests() -> None:
    """Reinicia estado do logger (apenas testes)."""
    global _configured, _file_logging_enabled, _file_logging_loaded
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    _configured = False
    _file_logging_enabled = False
    _file_logging_loaded = False
