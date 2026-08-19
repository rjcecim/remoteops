"""Faixa de IPv4, exclusão de 3º octeto e persistência (varredura de rede).

Módulo compartilhado: Configurações hoje, outras abas no futuro.
Não depende de Qt Widgets — só settings.ini.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from remoteops.utils.app_settings import load_setting, save_portable_settings

KEY_NET_ENABLED = "network/enabled"
KEY_NET_START_IP = "network/start_ip"
KEY_NET_END_IP = "network/end_ip"
KEY_NET_IGNORED_SUBNETS = "network/ignored_subnets"
KEY_NET_SCAN_THREADS = "network/scan_threads"

DEFAULT_SCAN_THREADS = 50
MIN_SCAN_THREADS = 10
MAX_SCAN_THREADS = 200
MAX_RANGE_ADDRESSES = 65536

_runtime: Optional["NetworkRangeConfig"] = None


@dataclass(frozen=True)
class NetworkRangeConfig:
    enabled: bool = True
    start_ip: str = ""
    end_ip: str = ""
    ignored_subnets: str = ""
    scan_threads: int = DEFAULT_SCAN_THREADS

    @property
    def configured(self) -> bool:
        ips, err = expand_ipv4_range(self.start_ip, self.end_ip)
        return err is None and bool(ips)


def normalize_scan_threads(value: Any) -> int:
    try:
        if value is None or value is False or value is True:
            return DEFAULT_SCAN_THREADS
        if isinstance(value, str) and not value.strip():
            return DEFAULT_SCAN_THREADS
        n = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_SCAN_THREADS
    return max(MIN_SCAN_THREADS, min(MAX_SCAN_THREADS, n))


def parse_enabled(value: Any, default: bool = True) -> bool:
    """Interpreta o flag do settings.ini (QSettings pode devolver str/int/bool)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def snap_scan_threads(value: Any) -> int:
    """Limita 10–200 e arredonda para múltiplo de 10 (slider)."""
    n = normalize_scan_threads(value)
    snapped = int(round(n / 10.0) * 10)
    return normalize_scan_threads(snapped)


def parse_ipv4(text: str) -> Optional[ipaddress.IPv4Address]:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    return addr


def expand_ipv4_range(start: str, end: str) -> tuple[list[str], Optional[str]]:
    """Lista IPv4 inclusiva start→end. Erro em português se inválido."""
    start_addr = parse_ipv4(start)
    end_addr = parse_ipv4(end)
    if start_addr is None or end_addr is None:
        if not (start or "").strip() and not (end or "").strip():
            return [], None
        return [], "IP inválido. Use endereços IPv4."
    start_n = int(start_addr)
    end_n = int(end_addr)
    if start_n > end_n:
        return [], "IP de início deve ser menor ou igual ao IP de fim."
    count = end_n - start_n + 1
    if count > MAX_RANGE_ADDRESSES:
        return [], f"Faixa grande demais ({count} endereços; máximo {MAX_RANGE_ADDRESSES})."
    return [str(ipaddress.IPv4Address(n)) for n in range(start_n, end_n + 1)], None


def parse_ignored_subnets(text: str) -> tuple[set[int], Optional[str]]:
    """3º octeto: '9; 10; 40' → {9, 10, 40}."""
    octets: set[int] = set()
    raw = (text or "").strip()
    if not raw:
        return octets, None
    for part in raw.split(";"):
        token = part.strip()
        if not token:
            continue
        try:
            value = int(token, 10)
        except ValueError:
            return set(), f'Sub-rede inválida: "{part.strip()}". Use números de 0 a 255 separados por ;.'
        if value < 0 or value > 255:
            return set(), f'Sub-rede inválida: "{part.strip()}". Use números de 0 a 255 separados por ;.'
        octets.add(value)
    return octets, None


def format_ignored_subnets(octets: Iterable[int]) -> str:
    vals = sorted({int(x) for x in octets if 0 <= int(x) <= 255})
    return "; ".join(str(v) for v in vals)


def exclude_subnets(ips: Sequence[str], ignored_third_octets: set[int]) -> list[str]:
    """Remove IPs cujo 3º octeto está em ``ignored_third_octets`` (ex.: 9 → 192.168.9.*)."""
    if not ignored_third_octets:
        return list(ips)
    out: list[str] = []
    for ip in ips:
        addr = parse_ipv4(str(ip))
        if addr is None:
            continue
        if int(addr.packed[2]) in ignored_third_octets:
            continue
        out.append(str(addr))
    return out


def ips_for_config(cfg: NetworkRangeConfig) -> tuple[list[str], Optional[str]]:
    ips, err = expand_ipv4_range(cfg.start_ip, cfg.end_ip)
    if err:
        return [], err
    octets, oct_err = parse_ignored_subnets(cfg.ignored_subnets)
    if oct_err:
        return [], oct_err
    return exclude_subnets(ips, octets), None


def _load_config_from_settings() -> NetworkRangeConfig:
    return NetworkRangeConfig(
        enabled=parse_enabled(load_setting(KEY_NET_ENABLED, True), True),
        start_ip=str(load_setting(KEY_NET_START_IP, "") or "").strip(),
        end_ip=str(load_setting(KEY_NET_END_IP, "") or "").strip(),
        ignored_subnets=str(load_setting(KEY_NET_IGNORED_SUBNETS, "") or "").strip(),
        scan_threads=snap_scan_threads(load_setting(KEY_NET_SCAN_THREADS, DEFAULT_SCAN_THREADS)),
    )


def get_network_range_config() -> NetworkRangeConfig:
    global _runtime
    if _runtime is None:
        _runtime = _load_config_from_settings()
    return _runtime


def is_network_range_configured() -> bool:
    cfg = get_network_range_config()
    return bool(cfg.enabled) and cfg.configured


def network_range_search_mode() -> tuple[str, Optional[str], int]:
    """Como a Pesquisa de Apps deve obter os hosts.

    Retorna ``(mode, erro, quantidade_de_ips)``:
    - ``json``: faixa desativada ou vazia → usa hosts.json
    - ``network``: faixa ativada e válida → varre a rede
    - ``invalid``: faixa ativada, mas não pode ser usada
    """
    cfg = get_network_range_config()
    if not cfg.enabled:
        return "json", None, 0
    start = (cfg.start_ip or "").strip()
    end = (cfg.end_ip or "").strip()
    if not start and not end:
        return "json", None, 0
    ips, err = ips_for_config(cfg)
    if err:
        return "invalid", err, 0
    if not ips:
        return "invalid", "Nenhum IP restante após ignorar as sub-redes.", 0
    return "network", None, len(ips)


def set_network_range_config(
    *,
    enabled: Optional[bool] = None,
    start_ip: Optional[str] = None,
    end_ip: Optional[str] = None,
    ignored_subnets: Optional[str] = None,
    scan_threads: Optional[int] = None,
) -> NetworkRangeConfig:
    """Atualiza e persiste a faixa. Em falha de gravação propaga SettingsWriteError."""
    global _runtime
    current = get_network_range_config()
    use = current.enabled if enabled is None else parse_enabled(enabled, current.enabled)
    start = current.start_ip if start_ip is None else str(start_ip).strip()
    end = current.end_ip if end_ip is None else str(end_ip).strip()
    ignored = current.ignored_subnets if ignored_subnets is None else str(ignored_subnets).strip()
    threads = current.scan_threads if scan_threads is None else snap_scan_threads(scan_threads)
    cfg = NetworkRangeConfig(
        enabled=bool(use),
        start_ip=start,
        end_ip=end,
        ignored_subnets=ignored,
        scan_threads=threads,
    )
    save_portable_settings(
        {
            KEY_NET_ENABLED: bool(cfg.enabled),
            KEY_NET_START_IP: cfg.start_ip,
            KEY_NET_END_IP: cfg.end_ip,
            KEY_NET_IGNORED_SUBNETS: cfg.ignored_subnets,
            KEY_NET_SCAN_THREADS: int(cfg.scan_threads),
        }
    )
    _runtime = cfg
    return cfg
