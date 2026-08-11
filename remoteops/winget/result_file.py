"""Recupera artefatos gravados pelo host remoto via SMB (ADMIN$/C$)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable


def build_remote_paths(host: str, svc_name: str) -> tuple[str, str, str, str, str, str]:
    """Retorna os caminhos do JSON e do log incremental remoto."""
    base_remote = f"C:\\Windows\\Temp\\{svc_name}"
    remote_json = base_remote + ".json"
    remote_log = base_remote + ".log"
    unc_admin_json = f"\\\\{host.strip()}\\ADMIN$\\Temp\\{svc_name}.json"
    unc_c_json = f"\\\\{host.strip()}\\C$\\Windows\\Temp\\{svc_name}.json"
    unc_admin_log = f"\\\\{host.strip()}\\ADMIN$\\Temp\\{svc_name}.log"
    unc_c_log = f"\\\\{host.strip()}\\C$\\Windows\\Temp\\{svc_name}.log"
    return remote_json, unc_admin_json, unc_c_json, remote_log, unc_admin_log, unc_c_log


def delete_remote_artifact(*paths: str) -> None:
    for p in paths:
        if not p:
            continue
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def read_remote_result_file(
    unc_admin: str,
    unc_c: str,
    *,
    attempts: int = 12,
    sleep_s: float = 0.25,
    log_cb: Callable[[str], None] | None = None,
) -> str | None:
    """Tenta ler ``svc_name.json`` via ``ADMIN$`` e ``C$`` com retry curto."""
    paths = [unc_admin, unc_c]
    last_err: Exception | None = None
    for _ in range(max(1, attempts)):
        for p in paths:
            try:
                if os.path.exists(p):
                    data = Path(p).read_bytes()
                    delete_remote_artifact(p)
                    return data.decode("utf-8", errors="replace")
            except Exception as e:
                last_err = e
        time.sleep(sleep_s)
    if last_err is not None and log_cb is not None:
        try:
            log_cb(f"[diag] Falha ao ler resultado via share admin: {last_err!r}")
        except Exception:
            pass
    return None


def tail_remote_log_file(
    unc_admin: str,
    unc_c: str,
    *,
    process_line: Callable[[str], None],
    stop_event: threading.Event,
    poll_s: float = 0.2,
) -> None:
    """Lê novas linhas de um arquivo remoto enquanto ele cresce."""
    active_path: str | None = None
    offset = 0
    pending = b""
    stable_loops = 0

    def _consume(chunk: bytes) -> bytes:
        nonlocal pending
        pending += chunk
        parts = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        pending = parts.pop() if parts else b""
        for raw in parts:
            try:
                process_line(raw.decode("utf-8", errors="replace"))
            except Exception:
                continue
        return pending

    while True:
        progressed = False
        candidates = [active_path] if active_path else []
        candidates += [p for p in (unc_admin, unc_c) if p and p not in candidates]

        for p in candidates:
            if not p:
                continue
            try:
                if not os.path.exists(p):
                    continue
                active_path = p
                data = Path(p).read_bytes()
                if len(data) < offset:
                    offset = 0
                    pending = b""
                if len(data) > offset:
                    _consume(data[offset:])
                    offset = len(data)
                    progressed = True
                break
            except Exception:
                continue

        if stop_event.is_set():
            if progressed:
                stable_loops = 0
            else:
                stable_loops += 1
            if stable_loops >= 3:
                break
        time.sleep(poll_s)

    if pending:
        try:
            process_line(pending.decode("utf-8", errors="replace"))
        except Exception:
            pass

    delete_remote_artifact(unc_admin, unc_c)
