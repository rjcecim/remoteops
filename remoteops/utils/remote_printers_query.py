"""Consulta de impressoras no Registro Remoto isolada em processo filho.

``winreg.ConnectRegistry`` / ``OpenKey`` / ``EnumKey`` podem bloquear a thread
sem cancelamento. Timeout e cancelamento só são reais se a chamada Win32
viver noutro processo (spawn), como em ``remote_registry_query``.
"""

from __future__ import annotations

import multiprocessing
import time
from typing import Callable, Optional

from remoteops.utils.installed_printers import MSG_CANCELLED, MSG_TIMEOUT
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.remote_printers_registry import collect_remote_printers_registry
from remoteops.utils.remote_registry_query import (
    PROCESS_JOIN_TIMEOUT_SECONDS,
    get_remote_registry_timeout,
)

_POLL_INTERVAL_SECONDS = 0.2
CancelFn = Callable[[], bool]


def _empty_payload(host: str, *, error: str = "", error_kind: str = "") -> dict:
    return {
        "ok": False,
        "host": host or "",
        "error": error,
        "error_kind": error_kind,
        "winerror": None,
        "user_sid": "",
        "default_name": "",
        "user_connections": [],
        "computer_connections": [],
        "local_printers": [],
        "hku_entries": [],
        "warnings": [],
    }


def remote_printers_registry_worker(request: dict, conn) -> None:
    """Target do processo filho (nível de módulo — exigido pelo spawn)."""
    payload = request if isinstance(request, dict) else {}
    try:
        result = collect_remote_printers_registry(
            str(payload.get("host") or ""),
            username=str(payload.get("username") or ""),
            domain=str(payload.get("domain") or ""),
            session_name=str(payload.get("session_name") or ""),
            include_local_fallback=bool(payload.get("include_local_fallback")),
        )
        conn.send(result if isinstance(result, dict) else _empty_payload(""))
    except Exception as exc:  # noqa: BLE001 — borda do processo filho
        conn.send(
            _empty_payload(
                str(payload.get("host") or ""),
                error=f"{type(exc).__name__}: {exc}",
                error_kind="internal_error",
            )
        )
    finally:
        try:
            conn.close()
        except OSError:
            pass


def hanging_registry_worker(request: dict, conn) -> None:
    """Worker só para testes de timeout/cancelamento (bloqueia até ser encerrado)."""
    try:
        time.sleep(float((request or {}).get("sleep_s") or 30))
        conn.send(_empty_payload(str((request or {}).get("host") or ""), error="hang"))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _stop_process(proc: Optional[multiprocessing.Process]) -> None:
    if proc is None:
        return
    try:
        if proc.is_alive():
            proc.terminate()
    except OSError:
        pass
    try:
        proc.join(PROCESS_JOIN_TIMEOUT_SECONDS)
    except Exception:
        pass
    try:
        if proc.is_alive():
            proc.kill()
            proc.join(PROCESS_JOIN_TIMEOUT_SECONDS)
    except Exception:
        pass


def _close_conn(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except OSError:
        pass


def query_remote_printers_registry(
    host: str,
    *,
    username: str = "",
    domain: str = "",
    session_name: str = "",
    include_local_fallback: bool = False,
    timeout: Optional[float] = None,
    should_cancel: Optional[CancelFn] = None,
    worker_target=None,
    extra_request: Optional[dict] = None,
) -> dict:
    """Consulta o Registro Remoto em processo isolado (spawn).

    ``worker_target`` e ``extra_request`` existem para testes de timeout —
    o fluxo de produção usa ``remote_printers_registry_worker``.
    """
    name = normalize_host(host)
    if not name or not is_valid_host(name):
        return _empty_payload(name, error="Host inválido.", error_kind="invalid_host")

    if should_cancel and should_cancel():
        return _empty_payload(name, error=MSG_CANCELLED, error_kind="cancelled")

    try:
        timeout_s = float(timeout) if timeout is not None and float(timeout) > 0 else get_remote_registry_timeout()
    except (TypeError, ValueError):
        timeout_s = get_remote_registry_timeout()

    request = {
        "host": name,
        "username": username or "",
        "domain": domain or "",
        "session_name": session_name or "",
        "include_local_fallback": bool(include_local_fallback),
    }
    if extra_request:
        request.update(extra_request)

    target = worker_target or remote_printers_registry_worker
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=target,
        args=(request, child_conn),
        name=f"rr-printers-{name}",
        daemon=True,
    )
    try:
        proc.start()
    except Exception as exc:  # noqa: BLE001
        _close_conn(parent_conn)
        _close_conn(child_conn)
        return _empty_payload(
            name,
            error=f"Falha ao iniciar processo de consulta: {exc}",
            error_kind="internal_error",
        )
    finally:
        _close_conn(child_conn)

    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if should_cancel and should_cancel():
                _stop_process(proc)
                return _empty_payload(name, error=MSG_CANCELLED, error_kind="cancelled")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(proc)
                return _empty_payload(name, error=MSG_TIMEOUT, error_kind="timed_out")

            try:
                ready = parent_conn.poll(min(_POLL_INTERVAL_SECONDS, remaining))
            except (OSError, EOFError, BrokenPipeError):
                ready = False

            if not ready:
                if not proc.is_alive():
                    return _empty_payload(
                        name,
                        error=(
                            "Processo de consulta encerrou sem resultado "
                            f"(exit={proc.exitcode})."
                        ),
                        error_kind="internal_error",
                    )
                continue

            try:
                payload = parent_conn.recv()
            except (EOFError, OSError, BrokenPipeError) as exc:
                _stop_process(proc)
                return _empty_payload(
                    name,
                    error=f"Falha ao receber resultado da consulta: {exc}",
                    error_kind="internal_error",
                )
            if not isinstance(payload, dict):
                return _empty_payload(
                    name,
                    error="Payload inválido do processo de consulta.",
                    error_kind="internal_error",
                )
            return payload
    finally:
        _stop_process(proc)
        _close_conn(parent_conn)
