"""Recupera artefatos gravados pelo host remoto via SMB (ADMIN$/C$)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple


class RemoteArtifacts(NamedTuple):
    """Caminhos locais-no-host e UNC para JSON, log e cancelamento."""

    json_path: str
    json_admin: str
    json_c: str
    log_path: str
    log_admin: str
    log_c: str
    cancel_path: str
    cancel_admin: str
    cancel_c: str


def build_remote_paths(host: str, run_id: str) -> RemoteArtifacts:
    """Retorna os caminhos do JSON, do log incremental e do sinal de cancel."""
    host = host.strip()
    base_remote = f"C:\\Windows\\Temp\\{run_id}"
    return RemoteArtifacts(
        json_path=base_remote + ".json",
        json_admin=f"\\\\{host}\\ADMIN$\\Temp\\{run_id}.json",
        json_c=f"\\\\{host}\\C$\\Windows\\Temp\\{run_id}.json",
        log_path=base_remote + ".log",
        log_admin=f"\\\\{host}\\ADMIN$\\Temp\\{run_id}.log",
        log_c=f"\\\\{host}\\C$\\Windows\\Temp\\{run_id}.log",
        cancel_path=base_remote + ".cancel",
        cancel_admin=f"\\\\{host}\\ADMIN$\\Temp\\{run_id}.cancel",
        cancel_c=f"\\\\{host}\\C$\\Windows\\Temp\\{run_id}.cancel",
    )


def signal_remote_cancel(*paths: str) -> list[str]:
    """Cria o arquivo de cancelamento no host via UNC. Devolve os caminhos gravados."""
    written: list[str] = []
    for p in paths:
        if not p:
            continue
        try:
            Path(p).write_text("1", encoding="utf-8")
            written.append(p)
        except Exception:
            continue
    return written


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
    """Tenta ler o JSON de resultado via ``ADMIN$`` e ``C$`` com retry curto."""
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
