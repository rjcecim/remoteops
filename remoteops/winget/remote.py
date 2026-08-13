"""Orquestra a execução remota de ``winget`` via PsExec + PowerShell.

Este módulo é intencionalmente *fino*: cada pedaço com lógica real mora em um
arquivo próprio (``constants``, ``json_utils``, ``clixml``, ``psexec_args``,
``powershell_script``, ``stream_reader``, ``output_parser``, ``result_file``,
``diagnostics``).
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Callable

from .clixml import (
    build_clixml_diagnostics,
    contains_raw_clixml,
    looks_like_clixml,
    parse_clixml,
)
from .constants import (
    CREATE_NO_WINDOW,
    CREATEPROCESS_CMDLINE_MAX,
    EXEC_ACTIONS,
    PSEXEC_ACTION_TIMEOUT_S,
    REALTIME_LOG_PREFIX,
    REMOTE_CANCEL_GRACE_S,
)
from .diagnostics import save_last_psexec_log
from .json_utils import loads_json_best_effort
from .output_parser import pick_json_blob
from .powershell_script import build_bootstrap_script, build_remote_script
from .psexec_args import build_psexec_args, psexec_hint
from .result_file import (
    build_remote_paths,
    delete_remote_artifact,
    read_remote_result_file,
    signal_remote_cancel,
    tail_remote_log_file,
)
from .stream_reader import make_line_processor, read_stream
from .win_error import ResolvedExitCode, resolve_windows_exit_code


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _emit(log_cb: Callable[[str], None] | None, msg: str) -> None:
    if log_cb is None:
        return
    try:
        log_cb(msg)
    except Exception:
        pass


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=CREATE_NO_WINDOW,
    )


def _request_remote_stop(
    proc: subprocess.Popen,
    *,
    log_cb: Callable[[str], None] | None,
    message: str,
    cancel_unc: tuple[str, ...],
    grace_s: float = REMOTE_CANCEL_GRACE_S,
) -> None:
    """Sinaliza cancelamento no host e só então mata o PsExec local se preciso."""
    _emit(log_cb, message)
    written = signal_remote_cancel(*cancel_unc)
    if not written:
        _emit(
            log_cb,
            f"[{_now_hms()}] Não foi possível gravar o sinal de cancelamento no host. "
            "Encerrando o PsExec local…",
        )
        try:
            proc.kill()
        except Exception:
            pass
        return
    deadline = time.monotonic() + float(grace_s)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _emit(log_cb, f"[{_now_hms()}] O host encerrou após o sinal de cancelamento.")
            return
        time.sleep(0.25)
    _emit(
        log_cb,
        f"[{_now_hms()}] O host não encerrou em {grace_s:.0f}s. Encerrando o PsExec local…",
    )
    try:
        proc.kill()
    except Exception:
        pass


def _run_and_capture(
    proc: subprocess.Popen,
    *,
    log_cb: Callable[[str], None] | None,
    progress_cb: Callable[[int], None] | None,
    cancel_event: threading.Event | None = None,
    cancel_unc: tuple[str, ...] = (),
) -> tuple[list[str], list[str], bool, bool]:
    """Lê stdout/stderr em threads e espera o processo. Devolve
    ``(stdout_lines, stderr_lines, timed_out, cancelled)``.
    """
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    processor = make_line_processor(log_cb=log_cb, progress_cb=progress_cb)

    t_out = threading.Thread(
        target=read_stream, args=(proc.stdout, stdout_lines, False, processor), daemon=True
    )
    t_err = threading.Thread(
        target=read_stream, args=(proc.stderr, stderr_lines, True, processor), daemon=True
    )
    t_out.start()
    t_err.start()

    timed_out = False
    cancelled = False
    deadline = time.monotonic() + float(PSEXEC_ACTION_TIMEOUT_S)
    poll_s = 0.25
    while True:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _request_remote_stop(
                proc,
                log_cb=log_cb,
                message=f"[{_now_hms()}] Cancelamento solicitado. Sinalizando o host remoto…",
                cancel_unc=cancel_unc,
            )
            break
        try:
            proc.wait(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                timed_out = True
                _request_remote_stop(
                    proc,
                    log_cb=log_cb,
                    message=(
                        f"[{_now_hms()}] Timeout após {PSEXEC_ACTION_TIMEOUT_S}s. "
                        "Sinalizando o host remoto…"
                    ),
                    cancel_unc=cancel_unc,
                )
                break

    for t in (t_out, t_err):
        try:
            t.join(timeout=10.0)
        except Exception:
            pass
    for s in (proc.stdout, proc.stderr):
        try:
            if s is not None:
                s.close()
        except Exception:
            pass

    return stdout_lines, stderr_lines, timed_out, cancelled


def _fallback_exec_payload(
    *,
    action: str,
    ids: list[str],
    exit_code: int,
    combined_text: str,
    diagnostics: dict | None = None,
) -> dict:
    results: list[dict] = []
    ids_list = list(ids) if ids else [""]
    for pkg_id in ids_list:
        item = {"Id": str(pkg_id), "ExitCode": int(exit_code), "Output": combined_text}
        if diagnostics:
            item["Diagnostics"] = diagnostics
        results.append(item)
    return {
        "Ok": False,
        "Action": action,
        "Meta": {"Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        "Error": "Execução remota não retornou JSON (provável falha do PsExec/host durante a execução).",
        "Results": results,
    }


def _annotate_stop(payload: dict, *, cancelled: bool, timed_out: bool) -> dict:
    meta = payload.get("Meta")
    if not isinstance(meta, dict):
        meta = {}
        payload["Meta"] = meta
    if cancelled:
        meta["Cancelled"] = True
        payload["Cancelled"] = True
        payload["Ok"] = False
        if not payload.get("Error"):
            payload["Error"] = "Operação cancelada pelo usuário."
    if timed_out:
        meta["TimedOut"] = True
        payload["TimedOut"] = True
        payload["Ok"] = False
        if not payload.get("Error"):
            payload["Error"] = "Execução remota excedeu o tempo limite."
    return payload


def _payload_or_raise(
    *,
    action: str,
    ids: list[str],
    stdout: str,
    stderr: str,
    file_json: str | None,
    exit_code: int,
    timed_out: bool,
    resolved_exit: ResolvedExitCode,
    cancelled: bool = False,
) -> dict:
    json_blob = pick_json_blob(stdout, stderr, file_json)

    payload: dict | None = None
    if json_blob:
        try:
            payload = loads_json_best_effort(json_blob)
        except Exception:
            payload = None

    if cancelled or timed_out:
        if payload is not None:
            _annotate_stop(payload, cancelled=cancelled, timed_out=timed_out)
            if str(payload.get("Action", "")).lower() in EXEC_ACTIONS and payload.get("Results"):
                return payload
            raise RuntimeError(
                payload.get("Error")
                or (
                    "Operação cancelada pelo usuário."
                    if cancelled
                    else "Execução remota excedeu o tempo limite."
                )
            )
        if cancelled:
            raise RuntimeError("Operação cancelada pelo usuário.")
        raise RuntimeError(
            "Execução remota excedeu o tempo limite e foi encerrada. "
            "O host remoto pode ter ficado com o serviço/processo do PsExec pendurado (PSEXESVC)."
        )

    if payload is None:
        combined_raw = "\n".join([x for x in (stdout, stderr) if x]).strip()
        parsed = parse_clixml(combined_raw)
        combined = parsed.plain_text
        if contains_raw_clixml(combined):
            combined = "Não foi possível interpretar a saída CLIXML do PowerShell."
        act = (action or "").lower()
        if act in EXEC_ACTIONS:
            diagnostics = (
                build_clixml_diagnostics(parsed) if looks_like_clixml(combined_raw) else None
            )
            return _fallback_exec_payload(
                action=act,
                ids=ids,
                exit_code=exit_code,
                combined_text=combined,
                diagnostics=diagnostics,
            )
        if exit_code != 0:
            hint = psexec_hint(
                exit_code,
                combined,
                system_message=resolved_exit.message,
                system_source=resolved_exit.source,
            )
            raise RuntimeError(hint)
        raise RuntimeError(
            "Não foi possível interpretar o retorno do host remoto como JSON.\n\n"
            + (combined or stdout or stderr)
        )

    if not payload.get("Ok", False):
        if str(payload.get("Action", "")).lower() in EXEC_ACTIONS and payload.get("Results"):
            return payload
        raise RuntimeError(payload.get("Error") or "Falha no host remoto.")

    return payload


def run_remote_winget(
    *,
    psexec_path: str,
    host: str,
    username: str,
    password: str,
    action: str,
    ids: list[str],
    query: str,
    log_cb: Callable[[str], None] | None,
    progress_cb: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Executa ``winget`` no host remoto via PsExec+PowerShell e devolve o dict JSON."""
    svc_name = "WINGETRM" + uuid.uuid4().hex[:6].upper()
    artifacts = build_remote_paths(host, svc_name)

    script = build_remote_script(
        action=action,
        ids=ids,
        query=query,
        result_path=artifacts.json_path,
        log_path=artifacts.log_path,
        cancel_path=artifacts.cancel_path,
        timeout_s=PSEXEC_ACTION_TIMEOUT_S,
    )
    ps_command = build_bootstrap_script(script)
    args = build_psexec_args(
        psexec_path=psexec_path,
        host=host,
        username=username,
        password=password,
        svc_name=svc_name,
        ps_command=ps_command,
    )
    cmdline_len = len(subprocess.list2cmdline(args))
    if cmdline_len > CREATEPROCESS_CMDLINE_MAX:
        raise RuntimeError(
            f"A linha de comando do PsExec tem {cmdline_len} caracteres "
            f"(limite do Windows: 32767 → WinError 206). "
            "O script remoto ficou grande demais para um único CreateProcess."
        )

    exe = args[0]
    target = args[1]
    _emit(log_cb, f"[{_now_hms()}] Executando: {exe} {target} (ação={action})")
    _emit(log_cb, f"[{_now_hms()}] Serviço remoto: {svc_name} (-r WINGETRM<auto>)")

    proc = _spawn(args)
    tail_stop = threading.Event()

    def _realtime_log_cb(msg: str) -> None:
        _emit(log_cb, REALTIME_LOG_PREFIX + msg)

    tail_processor = make_line_processor(log_cb=_realtime_log_cb, progress_cb=progress_cb)

    def _process_tailed_line(line: str) -> None:
        tail_processor(line, [], False)

    tail_thread = threading.Thread(
        target=tail_remote_log_file,
        kwargs={
            "unc_admin": artifacts.log_admin,
            "unc_c": artifacts.log_c,
            "process_line": _process_tailed_line,
            "stop_event": tail_stop,
        },
        daemon=True,
    )
    tail_thread.start()
    try:
        stdout_lines, stderr_lines, timed_out, cancelled = _run_and_capture(
            proc,
            log_cb=log_cb,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            cancel_unc=(artifacts.cancel_admin, artifacts.cancel_c),
        )
    finally:
        tail_stop.set()
        try:
            tail_thread.join(timeout=5.0)
        except Exception:
            pass
        delete_remote_artifact(artifacts.cancel_admin, artifacts.cancel_c)

    stdout = "\n".join(stdout_lines).strip()
    stderr = "\n".join(stderr_lines).strip()

    exit_code_int = int(proc.returncode if proc.returncode is not None else 0)
    resolved_exit = resolve_windows_exit_code(exit_code_int)
    _emit(
        log_cb,
        f"[{_now_hms()}] PsExec exit={exit_code_int}: {resolved_exit.message} (origem={resolved_exit.source})",
    )

    if proc.returncode != 0:
        save_last_psexec_log(
            action=action,
            host=host,
            exit_code=exit_code_int,
            stdout=stdout,
            stderr=stderr,
            exit_resolution_message=resolved_exit.message,
            exit_resolution_source=resolved_exit.source,
        )

    file_json = read_remote_result_file(artifacts.json_admin, artifacts.json_c, log_cb=log_cb)

    return _payload_or_raise(
        action=action,
        ids=list(ids or []),
        stdout=stdout,
        stderr=stderr,
        file_json=file_json,
        exit_code=exit_code_int,
        timed_out=timed_out,
        resolved_exit=resolved_exit,
        cancelled=cancelled,
    )
