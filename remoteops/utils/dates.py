"""Datas exibidas no RemoteOps: sempre dia/mês/ano (pt-BR)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

DISPLAY_DATE = "%d/%m/%Y"
DISPLAY_DATETIME = "%d/%m/%Y %H:%M:%S"

_DOTNET_DATE_RE = re.compile(r"^/Date\((-?\d+)\)/?$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$")
_SLASH_DATE_RE = re.compile(
    r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
    r"(?:\s*([AaPp][Mm]))?"
)
_WMI_PREFIX_RE = re.compile(r"^(\d{8})")
_ISO_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T\s](\d{1,2}):(\d{2})(?::(\d{2}))?"
)


def format_now_date() -> str:
    return datetime.now().strftime(DISPLAY_DATE)


def format_now_datetime() -> str:
    return datetime.now().strftime(DISPLAY_DATETIME)


def format_datetime(value: datetime) -> str:
    if value.hour or value.minute or value.second:
        return value.strftime(DISPLAY_DATETIME)
    return value.strftime(DISPLAY_DATE)


def parse_any_date(value: Any) -> Optional[datetime]:
    """Interpreta datas comuns (ISO, WMI, .NET, dd/mm ou mm/dd) em datetime local."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value

    text = str(value).strip()
    if not text or text in ("—", "-", "n/a", "N/A"):
        return None

    dotnet = _DOTNET_DATE_RE.match(text)
    if dotnet:
        try:
            ms = int(dotnet.group(1))
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None

    iso_dt = _ISO_DT_RE.match(text)
    if iso_dt:
        try:
            return datetime(
                int(iso_dt.group(1)),
                int(iso_dt.group(2)),
                int(iso_dt.group(3)),
                int(iso_dt.group(4)),
                int(iso_dt.group(5)),
                int(iso_dt.group(6) or 0),
            )
        except ValueError:
            return None

    iso = _ISO_DATE_RE.match(text)
    if iso:
        y, mo, d = (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None

    wmi = _WMI_PREFIX_RE.match(text)
    compact = text.isdigit() and len(text) in (8, 14)
    dotted = "." in text[:20] and (len(text) >= 8)
    long_wmi = len(text) >= 14 and text[:14].isdigit()
    if wmi and (compact or dotted or long_wmi):
        raw = wmi.group(1)
        try:
            return datetime(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            return None

    slash = _SLASH_DATE_RE.match(text)
    if slash:
        a, b, y = int(slash.group(1)), int(slash.group(2)), int(slash.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        else:
            day, month = a, b
        hour = int(slash.group(4) or 0)
        minute = int(slash.group(5) or 0)
        second = int(slash.group(6) or 0)
        ampm = slash.group(7)
        if ampm:
            is_pm = ampm.upper() == "PM"
            if is_pm and hour < 12:
                hour += 12
            elif not is_pm and hour == 12:
                hour = 0
        try:
            return datetime(y, month, day, hour, minute, second)
        except ValueError:
            if a <= 12 and b <= 12:
                try:
                    return datetime(y, a, b, hour, minute, second)
                except ValueError:
                    return None
            return None

    return None


def to_display_date(value: Any, *, default: str = "") -> str:
    """Converte qualquer data conhecida para ``dd/mm/aaaa`` (só a data)."""
    dt = parse_any_date(value)
    if dt is None:
        return default
    return dt.strftime(DISPLAY_DATE)


def to_display_datetime(value: Any, *, default: str = "") -> str:
    """Converte data/hora para ``dd/mm/aaaa HH:MM:SS`` quando houver hora."""
    dt = parse_any_date(value)
    if dt is None:
        return default
    text = "" if isinstance(value, datetime) else str(value)
    has_clock = ":" in text or dt.hour or dt.minute or dt.second
    if has_clock:
        return dt.strftime(DISPLAY_DATETIME)
    return dt.strftime(DISPLAY_DATE)
