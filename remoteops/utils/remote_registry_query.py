"""
Consultas Remote Registry isoladas em processo filho (Windows spawn).

Motivo: ``winreg.ConnectRegistry`` / ``OpenKey`` / ``EnumKey`` bloqueiam a
thread Python sem cancelamento seguro. Timeout e “Parar pesquisa” só são
reais se o trabalho Win32/RPC viver em outro processo que possa ser
``terminate()``/``kill()``.

API pública:
- ``query_remote_installed_apps`` — um host, com timeout e cancelamento;
- ``run_remote_inventory_batch`` — vários hosts com paralelismo limitado.

O filho só importa ``utils.psinfo`` e serializa dicts/listas/primitivos.
Não recebe widgets Qt, sinais, locks nem credenciais.
"""

from __future__ import annotations

import multiprocessing
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from remoteops.utils.psinfo import HostInventoryStatus, InstalledApp, list_remote_installed_apps_status

# Timeout individual cobrindo ConnectRegistry + enumeração 64/32 + dedup + IPC.
REMOTE_REGISTRY_TIMEOUT_SECONDS = 15.0
# Tempo extra após terminate/kill para join do filho.
PROCESS_JOIN_TIMEOUT_SECONDS = 2.0
# Intervalo de poll do Pipe / flag de cancelamento (não é o timeout do host).
_POLL_INTERVAL_SECONDS = 0.2


def _app_to_dict(app: InstalledApp) -> dict:
    return {
        "display_name": app.display_name,
        "version": app.version,
        "publisher": app.publisher,
        "display_line": app.display_line,
        "product_code": app.product_code,
        "uninstall_string": app.uninstall_string,
        "quiet_uninstall_string": app.quiet_uninstall_string,
        "is_msi": bool(app.is_msi),
        "arch": app.arch,
    }


def _app_from_dict(data: dict) -> InstalledApp:
    return InstalledApp(
        display_name=str(data.get("display_name") or ""),
        version=str(data.get("version") or ""),
        publisher=str(data.get("publisher") or ""),
        display_line=str(data.get("display_line") or ""),
        product_code=str(data.get("product_code") or ""),
        uninstall_string=str(data.get("uninstall_string") or ""),
        quiet_uninstall_string=str(data.get("quiet_uninstall_string") or ""),
        is_msi=bool(data.get("is_msi")),
        arch=str(data.get("arch") or ""),
    )


def status_to_payload(status: HostInventoryStatus) -> dict:
    """Serializa status para Pipe/Queue (spawn-safe)."""
    return {
        "host": status.host,
        "ok": bool(status.ok),
        "apps": [_app_to_dict(a) for a in (status.apps or [])],
        "error_kind": status.error_kind or "",
        "message": status.message or "",
        "winerror": status.winerror,
        "stage": status.stage or "",
    }


def status_from_payload(payload: dict) -> HostInventoryStatus:
    """Reconstrói HostInventoryStatus a partir do payload do filho."""
    apps_raw = payload.get("apps") or []
    apps = [_app_from_dict(a) for a in apps_raw if isinstance(a, dict)]
    winerror = payload.get("winerror")
    if winerror is not None and not isinstance(winerror, int):
        try:
            winerror = int(winerror)
        except (TypeError, ValueError):
            winerror = None
    return HostInventoryStatus(
        host=str(payload.get("host") or ""),
        ok=bool(payload.get("ok")),
        apps=apps,
        error_kind=str(payload.get("error_kind") or ""),
        message=str(payload.get("message") or ""),
        winerror=winerror,
        stage=str(payload.get("stage") or ""),
    )


def _make_status(
    host: str,
    *,
    ok: bool = False,
    apps: Optional[List[InstalledApp]] = None,
    error_kind: str = "",
    message: str = "",
    winerror: Optional[int] = None,
    stage: str = "",
) -> HostInventoryStatus:
    return HostInventoryStatus(
        host=host,
        ok=ok,
        apps=list(apps or []),
        error_kind=error_kind,
        message=message,
        winerror=winerror,
        stage=stage,
    )


def remote_registry_query_worker(host: str, conn) -> None:
    """
    Target do processo filho (nível de módulo — exigido pelo spawn).

    Executa a consulta Win32 completa e envia um único payload serializável.
    """
    try:
        status = list_remote_installed_apps_status(host)
        if status.ok and not status.stage:
            status.stage = "enumerate"
        conn.send(status_to_payload(status))
    except Exception as exc:  # noqa: BLE001 — borda do processo filho
        conn.send(
            {
                "host": (host or "").strip().strip("\\"),
                "ok": False,
                "apps": [],
                "error_kind": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "winerror": getattr(exc, "winerror", None)
                if isinstance(getattr(exc, "winerror", None), int)
                else None,
                "stage": "child",
            }
        )
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _stop_process(proc: Optional[multiprocessing.Process]) -> None:
    if proc is None:
        return
    _stop_processes([proc])


def _stop_processes(procs: Sequence[Optional[multiprocessing.Process]]) -> None:
    """terminate em paralelo, depois join; kill só nos que restarem vivos."""
    alive = [p for p in procs if p is not None]
    for proc in alive:
        try:
            if proc.is_alive():
                proc.terminate()
        except OSError:
            pass
    for proc in alive:
        try:
            proc.join(PROCESS_JOIN_TIMEOUT_SECONDS)
        except Exception:
            pass
    for proc in alive:
        try:
            if proc.is_alive():
                proc.kill()
                proc.join(PROCESS_JOIN_TIMEOUT_SECONDS)
        except OSError:
            pass
        except Exception:
            pass


def _close_conn(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except OSError:
        pass


@dataclass
class _ActiveQuery:
    host: str
    proc: multiprocessing.Process
    parent_conn: object
    deadline: float


def query_remote_installed_apps(
    host: str,
    *,
    timeout: float = REMOTE_REGISTRY_TIMEOUT_SECONDS,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> HostInventoryStatus:
    """
    Consulta inventário remoto em processo isolado.

    Encera o filho em timeout ou cancelamento. Nunca deixa a chamada Win32
    no processo da UI/QThread.
    """
    h = (host or "").strip().strip("\\")
    if not h:
        return _make_status(
            "",
            error_kind="invalid_host",
            message="Host inválido ou vazio.",
            stage="validate",
        )

    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError):
        timeout_s = REMOTE_REGISTRY_TIMEOUT_SECONDS
    if timeout_s <= 0:
        timeout_s = REMOTE_REGISTRY_TIMEOUT_SECONDS

    if should_cancel and should_cancel():
        return _make_status(
            h,
            error_kind="cancelled",
            message="Consulta cancelada antes de iniciar.",
            stage="cancel",
        )

    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=remote_registry_query_worker,
        args=(h, child_conn),
        name=f"rr-query-{h}",
        daemon=True,
    )
    try:
        proc.start()
    except Exception as exc:  # noqa: BLE001
        _close_conn(parent_conn)
        _close_conn(child_conn)
        return _make_status(
            h,
            error_kind="internal_error",
            message=f"Falha ao iniciar processo de consulta: {exc}",
            stage="spawn",
        )
    finally:
        # Pai não usa a ponta do filho.
        _close_conn(child_conn)

    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if should_cancel and should_cancel():
                _stop_process(proc)
                return _make_status(
                    h,
                    error_kind="cancelled",
                    message="Consulta cancelada.",
                    stage="cancel",
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(proc)
                return _make_status(
                    h,
                    error_kind="timed_out",
                    message=(
                        f"Consulta Remote Registry excedeu "
                        f"{timeout_s:.0f}s e foi encerrada."
                    ),
                    stage="timeout",
                )

            try:
                ready = parent_conn.poll(min(_POLL_INTERVAL_SECONDS, remaining))
            except (OSError, EOFError, BrokenPipeError):
                ready = False

            if not ready:
                if not proc.is_alive():
                    # Filho morreu sem enviar payload utilizável.
                    exitcode = proc.exitcode
                    return _make_status(
                        h,
                        error_kind="internal_error",
                        message=(
                            "Processo de consulta encerrou sem resultado "
                            f"(exit={exitcode})."
                        ),
                        stage="ipc",
                    )
                continue

            try:
                payload = parent_conn.recv()
            except (EOFError, OSError, BrokenPipeError) as exc:
                _stop_process(proc)
                return _make_status(
                    h,
                    error_kind="internal_error",
                    message=f"Falha ao receber resultado da consulta: {exc}",
                    stage="ipc",
                )

            if not isinstance(payload, dict):
                return _make_status(
                    h,
                    error_kind="internal_error",
                    message="Payload inválido do processo de consulta.",
                    stage="ipc",
                )
            return status_from_payload(payload)
    finally:
        _stop_process(proc)
        _close_conn(parent_conn)


def run_remote_inventory_batch(
    hosts: Sequence[str],
    *,
    max_workers: int,
    timeout: float = REMOTE_REGISTRY_TIMEOUT_SECONDS,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Iterator[HostInventoryStatus]:
    """
    Agenda consultas com no máximo ``max_workers`` processos ativos.

    Yields um ``HostInventoryStatus`` por host efetivamente iniciado.
    Hosts ainda na fila quando ocorre cancelamento global **não** são
    emitidos (não viram unreachable).
    """
    pending: List[str] = [((h or "").strip().strip("\\")) for h in hosts]
    pending = [h for h in pending if h]

    try:
        workers = max(1, int(max_workers))
    except (TypeError, ValueError):
        workers = 1

    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError):
        timeout_s = REMOTE_REGISTRY_TIMEOUT_SECONDS
    if timeout_s <= 0:
        timeout_s = REMOTE_REGISTRY_TIMEOUT_SECONDS

    ctx = multiprocessing.get_context("spawn")
    # job_id -> ActiveQuery (permite o mesmo hostname mais de uma vez na lista)
    active: Dict[int, _ActiveQuery] = {}
    next_id = 0
    cancelled = False

    def _spawn(host: str) -> Tuple[Optional[_ActiveQuery], Optional[HostInventoryStatus]]:
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=remote_registry_query_worker,
            args=(host, child_conn),
            name=f"rr-query-{host}",
            daemon=True,
        )
        try:
            proc.start()
        except Exception as exc:  # noqa: BLE001
            _close_conn(parent_conn)
            _close_conn(child_conn)
            return None, _make_status(
                host,
                error_kind="internal_error",
                message=f"Falha ao iniciar processo de consulta: {exc}",
                stage="spawn",
            )
        _close_conn(child_conn)
        return (
            _ActiveQuery(
                host=host,
                proc=proc,
                parent_conn=parent_conn,
                deadline=time.monotonic() + timeout_s,
            ),
            None,
        )

    def _reap(job_id: int, status: HostInventoryStatus) -> HostInventoryStatus:
        job = active.pop(job_id, None)
        if job is not None:
            _stop_process(job.proc)
            _close_conn(job.parent_conn)
        return status

    try:
        while pending or active:
            if should_cancel and should_cancel():
                cancelled = True

            if cancelled:
                procs = [job.proc for job in active.values()]
                _stop_processes(procs)
                for job_id, job in list(active.items()):
                    _close_conn(job.parent_conn)
                    yield _make_status(
                        job.host,
                        error_kind="cancelled",
                        message="Consulta cancelada.",
                        stage="cancel",
                    )
                active.clear()
                pending.clear()
                break

            while pending and len(active) < workers:
                if should_cancel and should_cancel():
                    cancelled = True
                    break
                host = pending.pop(0)
                job, early = _spawn(host)
                if early is not None:
                    yield early
                    continue
                assert job is not None
                nonlocal_id = next_id
                next_id += 1
                active[nonlocal_id] = job

            if cancelled:
                continue

            progressed = False
            now = time.monotonic()
            for job_id, job in list(active.items()):
                if now >= job.deadline:
                    yield _reap(
                        job_id,
                        _make_status(
                            job.host,
                            error_kind="timed_out",
                            message=(
                                f"Consulta Remote Registry excedeu "
                                f"{timeout_s:.0f}s e foi encerrada."
                            ),
                            stage="timeout",
                        ),
                    )
                    progressed = True
                    continue

                try:
                    ready = job.parent_conn.poll(0)
                except (OSError, EOFError, BrokenPipeError):
                    ready = False

                if ready:
                    try:
                        payload = job.parent_conn.recv()
                    except (EOFError, OSError, BrokenPipeError) as exc:
                        yield _reap(
                            job_id,
                            _make_status(
                                job.host,
                                error_kind="internal_error",
                                message=f"Falha ao receber resultado: {exc}",
                                stage="ipc",
                            ),
                        )
                        progressed = True
                        continue
                    if isinstance(payload, dict):
                        yield _reap(job_id, status_from_payload(payload))
                    else:
                        yield _reap(
                            job_id,
                            _make_status(
                                job.host,
                                error_kind="internal_error",
                                message="Payload inválido do processo de consulta.",
                                stage="ipc",
                            ),
                        )
                    progressed = True
                    continue

                if not job.proc.is_alive():
                    yield _reap(
                        job_id,
                        _make_status(
                            job.host,
                            error_kind="internal_error",
                            message=(
                                "Processo de consulta encerrou sem resultado "
                                f"(exit={job.proc.exitcode})."
                            ),
                            stage="ipc",
                        ),
                    )
                    progressed = True

            if not progressed and (pending or active):
                time.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        _stop_processes([job.proc for job in active.values()])
        for job in active.values():
            _close_conn(job.parent_conn)
        active.clear()
