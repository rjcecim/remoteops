"""Runner stdlib partilhado: Popen + pipes + timeout + cancelamento local.

Sem Qt. Callers com cancelamento remoto (WinGet) mantêm a orquestração própria
e usam apenas ``popen_argv`` / ``CREATE_NO_WINDOW`` de ``win_cmd``.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from remoteops.core.win_cmd import CREATE_NO_WINDOW, popen_argv

CancelFn = Callable[[], bool]
ProcHook = Callable[[subprocess.Popen], None]


@dataclass
class CapturedProcess:
    """Resultado bruto (bytes) de um processo capturado."""

    returncode: int = 1
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    cancelled: bool = False
    spawn_error: str = ""


def _drain_pipe(stream, chunks: List[bytes]) -> None:
    if stream is None:
        return
    try:
        while True:
            data = stream.read(65536)
            if not data:
                break
            chunks.append(data)
    except Exception:
        pass


def run_argv_captured(
    argv: Sequence[str],
    *,
    timeout_s: Optional[float] = None,
    should_cancel: Optional[CancelFn] = None,
    poll_s: float = 0.15,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    bufsize: int = -1,
    on_started: Optional[ProcHook] = None,
    on_finished: Optional[Callable[[], None]] = None,
) -> CapturedProcess:
    """
    Executa ``argv`` sem shell, com ``CREATE_NO_WINDOW``, captura stdout/stderr.

    ``timeout_s`` None = sem limite. ``should_cancel`` True → kill local.
    """
    if not argv:
        return CapturedProcess(spawn_error="argv vazio")

    try:
        proc = popen_argv(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW if CREATE_NO_WINDOW else 0,
            env=env,
            bufsize=bufsize,
        )
    except FileNotFoundError:
        return CapturedProcess(spawn_error=f"Executável não encontrado: {argv[0]}")
    except OSError as exc:
        return CapturedProcess(spawn_error=str(exc) or "Falha ao iniciar o processo.")

    if on_started is not None:
        try:
            on_started(proc)
        except Exception:
            pass

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    readers = [
        threading.Thread(target=_drain_pipe, args=(proc.stdout, out_chunks), daemon=True),
        threading.Thread(target=_drain_pipe, args=(proc.stderr, err_chunks), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    cancelled = False
    try:
        deadline = (
            time.monotonic() + max(0.1, float(timeout_s))
            if timeout_s is not None
            else None
        )
        while proc.poll() is None:
            if should_cancel is not None and should_cancel():
                cancelled = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            time.sleep(max(0.05, float(poll_s)))
        for reader in readers:
            reader.join(timeout=8)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        if cancelled or timed_out:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    finally:
        if on_finished is not None:
            try:
                on_finished()
            except Exception:
                pass

    code = int(proc.returncode if proc.returncode is not None else 1)
    return CapturedProcess(
        returncode=code,
        stdout=b"".join(out_chunks),
        stderr=b"".join(err_chunks),
        timed_out=timed_out,
        cancelled=cancelled,
    )
