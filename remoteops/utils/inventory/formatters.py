from __future__ import annotations

import re
from typing import Any, Optional

from remoteops.utils.dates import to_display_date
from remoteops.utils.psinfo import (
    format_bytes_compact,
    format_uptime_duration,
    is_invalid_psinfo_uptime,
    parse_size_to_bytes,
)

_EMPTY = "—"

_TPM_VERSION_RE = re.compile(r"^[\d.]+")


def sanitize_display_text(value: Any, *, default: str = "") -> str:
    """Remove nulos e caracteres de controle comuns em saída WMI/TPM."""
    if value is None:
        return default
    text = str(value)
    if "\x00" in text:
        text = text.split("\x00", 1)[0]
    cleaned = "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")
    return cleaned.strip() or default


def safe_str(value: Any, *, default: str = _EMPTY) -> str:
    text = sanitize_display_text(value, default="")
    return text if text else default


def format_tpm_version(spec: Any = None, manufacturer: Any = None) -> str:
    """Versão TPM legível — SpecVersion primeiro; ManufacturerVersion pode ter lixo binário."""
    for candidate in (spec, manufacturer):
        text = sanitize_display_text(candidate, default="")
        if not text:
            continue
        match = _TPM_VERSION_RE.match(text)
        if match:
            return match.group(0).rstrip(".")
        return text
    return _EMPTY

# SMBIOS memory type codes (subset comum)
_SMBIOS_MEMORY_TYPES: dict[int, str] = {
    0: "Desconhecido",
    1: "Other",
    2: "DRAM",
    3: "Synchronous DRAM",
    4: "Cache DRAM",
    5: "EDO",
    6: "EDRAM",
    7: "VRAM",
    8: "SRAM",
    9: "RAM",
    10: "ROM",
    11: "Flash",
    12: "EEPROM",
    13: "FEPROM",
    14: "EPROM",
    15: "CDRAM",
    16: "3DRAM",
    17: "SDRAM",
    18: "SGRAM",
    19: "RDRAM",
    20: "DDR",
    21: "DDR2",
    22: "DDR2 FB-DIMM",
    24: "DDR3",
    25: "FBD2",
    26: "DDR4",
    27: "LPDDR",
    28: "LPDDR2",
    29: "LPDDR3",
    30: "LPDDR4",
    34: "DDR5",
    35: "LPDDR5",
}


def format_bytes_human(num_bytes: int, *, precision: int = 1) -> str:
    if num_bytes <= 0:
        return _EMPTY
    compact = format_bytes_compact(num_bytes)
    return compact if compact else _EMPTY


def bytes_to_gb(num_bytes: int, *, precision: int = 1) -> str:
    if num_bytes <= 0:
        return _EMPTY
    gb = num_bytes / (1024**3)
    if gb >= 100:
        return f"{gb:.0f} GB"
    if gb >= 10:
        return f"{gb:.1f} GB"
    return f"{gb:.2f} GB"


def smbios_memory_type(code: Any) -> str:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return safe_str(code)
    return _SMBIOS_MEMORY_TYPES.get(n, f"Tipo {n}")


def format_mhz(value: Any) -> str:
    text = safe_str(value, default="")
    if not text:
        return _EMPTY
    try:
        n = int(float(text))
        return f"{n} MHz" if n > 0 else _EMPTY
    except (TypeError, ValueError):
        return text


def format_uptime(text: str) -> str:
    s = safe_str(text, default="")
    if is_invalid_psinfo_uptime(s):
        return _EMPTY
    return s


def format_link_speed_bps(value: Any) -> str:
    try:
        bps = int(value)
    except (TypeError, ValueError):
        return safe_str(value)
    if bps <= 0:
        return _EMPTY
    if bps >= 1_000_000_000:
        gbps = bps / 1_000_000_000
        return f"{gbps:.0f} Gbps" if gbps == int(gbps) else f"{gbps:.1f} Gbps"
    if bps >= 1_000_000:
        mbps = bps / 1_000_000
        return f"{mbps:.0f} Mbps" if mbps == int(mbps) else f"{mbps:.1f} Mbps"
    return f"{bps} bps"


def parse_psinfo_size(text: str) -> int:
    return parse_size_to_bytes(text)


def volume_used_pct(size_bytes: int, free_bytes: int) -> Optional[float]:
    if size_bytes <= 0:
        return None
    used = max(0, size_bytes - free_bytes)
    return round(100.0 * used / size_bytes, 1)


def normalize_wmi_date(value: Any) -> str:
    text = safe_str(value, default="")
    if not text or text == _EMPTY:
        return _EMPTY
    return to_display_date(text) or text


def architecture_label(code: Any) -> str:
    mapping = {
        0: "x86",
        1: "MIPS",
        2: "Alpha",
        3: "PowerPC",
        5: "ARM",
        6: "Itanium",
        9: "x64",
        12: "ARM64",
    }
    try:
        return mapping.get(int(code), f"Arquitetura {int(code)}")
    except (TypeError, ValueError):
        return safe_str(code)
