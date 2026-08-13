"""Varredura de hosts Windows na faixa de IP (mesma lógica do WinSeeker).

Por IP: ping ICMP → portas 445/135/139 → nome NetBIOS (nbtstat) ou DNS.
Não depende de Qt.
"""

from __future__ import annotations

import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Sequence

from remoteops.core.win_cmd import CREATE_NO_WINDOW
from remoteops.utils.network_range import (
    DEFAULT_SCAN_THREADS,
    MAX_SCAN_THREADS,
    MIN_SCAN_THREADS,
    snap_scan_threads,
)
from remoteops.utils.ping import is_valid_host, ping_host

WINDOWS_PORTS = (445, 135, 139)
PING_TIMEOUT_MS = 1000
PORT_TIMEOUT_SEC = 0.5
NBTSTAT_TIMEOUT_SEC = 3.0

ProgressCallback = Callable[[int, int, str, int], None]
HostCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


def parse_nbtstat_name(output: str) -> Optional[str]:
    """Extrai o nome NetBIOS da linha ``<00> UNIQUE`` (não GROUP / ``___``)."""
    if not output:
        return None
    unique_markers = ("UNIQUE", "ÚNICO", "UNICO")
    for raw in output.splitlines():
        line = raw.strip()
        if "<00>" not in line:
            continue
        upper = line.upper()
        if not any(m in upper for m in unique_markers):
            continue
        parts = line.split()
        if not parts:
            continue
        name = parts[0].strip()
        if name.startswith("___"):
            continue
        if name.startswith("<"):
            continue
        if is_valid_host(name):
            return name
    return None


def _decode_console(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("oem", "mbcs", "cp850", "utf-8"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _tcp_port_open(ip: str, port: int, timeout: float = PORT_TIMEOUT_SEC) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def has_windows_port(ip: str, should_cancel: Optional[CancelCallback] = None) -> bool:
    for port in WINDOWS_PORTS:
        if should_cancel and should_cancel():
            return False
        if _tcp_port_open(ip, port):
            return True
    return False


def netbios_name(ip: str) -> Optional[str]:
    if not is_valid_host(ip):
        return None
    try:
        result = subprocess.run(
            ["nbtstat", "-A", ip],
            capture_output=True,
            timeout=NBTSTAT_TIMEOUT_SEC,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    return parse_nbtstat_name(_decode_console(result.stdout or b""))


def dns_short_name(ip: str) -> Optional[str]:
    try:
        host, _aliases, _addrs = socket.gethostbyaddr(ip)
    except (OSError, socket.herror, socket.gaierror):
        return None
    host = (host or "").strip()
    if not host:
        return None
    short = host.split(".", 1)[0].strip()
    return short if is_valid_host(short) else None


def resolve_windows_hostname(ip: str) -> str:
    """Nome NetBIOS, senão DNS curto, senão o próprio IP."""
    name = netbios_name(ip) or dns_short_name(ip)
    if name and is_valid_host(name):
        return name
    return ip


def probe_windows_host(
    ip: str,
    should_cancel: Optional[CancelCallback] = None,
) -> Optional[str]:
    """Retorna hostname (ou IP) se o alvo responder ping e tiver porta Windows."""
    if should_cancel and should_cancel():
        return None
    if not is_valid_host(ip):
        return None
    online, _err = ping_host(ip, timeout_ms=PING_TIMEOUT_MS)
    if not online:
        return None
    if should_cancel and should_cancel():
        return None
    if not has_windows_port(ip, should_cancel=should_cancel):
        return None
    if should_cancel and should_cancel():
        return None
    return resolve_windows_hostname(ip)


def normalize_scan_workers(value: int, total: int) -> int:
    n = snap_scan_threads(value) if value else DEFAULT_SCAN_THREADS
    n = max(MIN_SCAN_THREADS, min(MAX_SCAN_THREADS, n))
    if total <= 0:
        return n
    return max(1, min(n, total))


def scan_windows_hosts(
    ips: Sequence[str],
    *,
    max_workers: int = DEFAULT_SCAN_THREADS,
    should_cancel: Optional[CancelCallback] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_host: Optional[HostCallback] = None,
) -> list[str]:
    """Varre IPs em paralelo e devolve hostnames únicos (ordem de descoberta).

    ``on_host`` é chamado na thread da varredura a cada hostname novo.
    """
    targets = [str(ip).strip() for ip in ips if str(ip).strip()]
    total = len(targets)
    if total == 0:
        return []

    workers = normalize_scan_workers(max_workers, total)
    found: list[str] = []
    seen: set[str] = set()
    done = 0

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(probe_windows_host, ip, _cancelled): ip for ip in targets}
        for fut in as_completed(futures):
            ip = futures[fut]
            name: Optional[str] = None
            try:
                name = None if _cancelled() else fut.result()
            except Exception:
                name = None
            done += 1
            if name:
                key = name.casefold()
                if key not in seen:
                    seen.add(key)
                    found.append(name)
                    if on_host:
                        on_host(name)
            if on_progress:
                on_progress(done, total, ip, len(found))
            if _cancelled():
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return found
