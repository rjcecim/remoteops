"""Executor assíncrono com resultado estruturado — pipes ou ConPTY."""

from __future__ import annotations

import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional, Sequence, Union

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from remoteops.core.models import CommandSpec, ExecutionResult, OperationStatus, is_robocopy_success
from remoteops.core.win_cmd import CREATE_NO_WINDOW, popen_argv
from remoteops.utils.redaction import redact_command_text


def decode_best_effort(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8-sig")
    except Exception:
        pass
    try:
        return b.decode("mbcs", errors="replace")
    except Exception:
        pass
    return b.decode("cp1252", errors="replace")


def _read_pipe(pipe, callback, prefix: str = "") -> str:
    """Lê um pipe linha a linha; evita deadlock ao rodar em thread dedicada."""
    chunks: List[str] = []
    try:
        for line_b in iter(pipe.readline, b""):
            line = decode_best_effort(line_b).rstrip("\r\n")
            if not line:
                continue
            chunks.append(line)
            text = f"{prefix}{line}" if prefix else line
            if callback:
                callback(text)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass
    return "\n".join(chunks)


def _emit_conpty_text(
    text: str,
    *,
    prefix: str,
    on_line,
    carry: List[str],
    on_partial=None,
) -> None:
    """Converte pedaços ConPTY em linhas para o log (mantém leftover parcial)."""
    if not text:
        return
    data = (carry[0] if carry else "") + text
    carry.clear()
    parts = data.split("\n")
    if not data.endswith("\n"):
        carry.append(parts[-1])
        parts = parts[:-1]
    for part in parts:
        line = part.rstrip("\r")
        if not line.strip():
            continue
        on_line(f"{prefix}{line}" if prefix else line)
    if on_partial is not None:
        partial = carry[0] if carry else ""
        on_partial(f"{prefix}{partial}" if (prefix and partial) else partial)


class Executor(QObject):
    """
    Executa um comando em ThreadPoolExecutor (1 worker) e emite sinais Qt.

    ``use_conpty=True``: saída via Windows ConPTY (PsExec do botão Executar).
    Robocopy e demais continuam em pipes CREATE_NO_WINDOW.

    PsInfo, Pesquisa e Desinstalar dessas abas NÃO usam este caminho ConPTY.

    Cancelamento: encerra apenas o processo LOCAL. Processos remotos iniciados
    via PsExec podem continuar — ``ExecutionResult.remote_may_continue=True``.
    """

    outputReceived = pyqtSignal(str)
    errorReceived = pyqtSignal(str)
    partialOutput = pyqtSignal(str)  # prompt / linha incompleta do ConPTY
    interactiveChanged = pyqtSignal(bool)  # sessão ConPTY aceita teclado
    finished = pyqtSignal(int)
    resultReady = pyqtSignal(object)  # ExecutionResult

    def __init__(self, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future: Optional[Future] = None
        self.process: Optional[subprocess.Popen] = None
        self._conpty_terminate = None
        self._conpty_write = None
        self._cancel_requested = False
        self._last_result: Optional[ExecutionResult] = None
        self._passwords: List[str] = []
        self._run_generation = 0
        self._lock = threading.Lock()
        self._interactive = False

    def run(
        self,
        command: Union[str, CommandSpec, Sequence[str]],
        *,
        passwords: Optional[Sequence[str]] = None,
        timeout: Optional[float] = None,
        use_conpty: bool = False,
    ) -> None:
        self.stop()
        with self._lock:
            self._cancel_requested = False
            self._conpty_terminate = None
            self._conpty_write = None
            self._run_generation += 1
            generation = self._run_generation
            self._passwords = [p for p in (passwords or []) if p]
            self.future = self.executor.submit(
                self._run_command, command, timeout, generation, bool(use_conpty)
            )
        QTimer.singleShot(100, lambda: self._check_future(generation))

    def send_input(self, text: str) -> bool:
        """Envia uma linha (Enter) ao ConPTY ativo. Retorna False se não houver sessão."""
        payload = text if text.endswith("\r") else f"{text}\r"
        return self._write_conpty(payload)

    def send_control(self, code: str = "\x03") -> bool:
        """Envia controle ao ConPTY (padrão: Ctrl+C)."""
        return self._write_conpty(code)

    def _write_conpty(self, data: str) -> bool:
        with self._lock:
            write = self._conpty_write
        if write is None:
            return False
        try:
            return bool(write(data))
        except Exception:
            return False

    def _set_interactive(self, active: bool) -> None:
        with self._lock:
            if self._interactive == active:
                return
            self._interactive = active
        self.interactiveChanged.emit(active)

    def _normalize_argv(
        self, command: Union[str, CommandSpec, Sequence[str]]
    ) -> tuple[List[str], bool, str]:
        if isinstance(command, CommandSpec):
            argv = command.argv
            display = command.sanitized_display(self._passwords)
            is_rc = (command.metadata or {}).get("kind") == "robocopy" or (
                argv and "robocopy" in argv[0].lower()
            )
            return argv, bool(is_rc), display

        if isinstance(command, (list, tuple)):
            argv = [str(a) for a in command]
            is_rc = bool(argv) and "robocopy" in argv[0].lower()
            display = redact_command_text(" ".join(argv), passwords=self._passwords)
            return argv, is_rc, display

        text = str(command or "").strip()
        display = redact_command_text(text, passwords=self._passwords)
        is_rc = text.lower().lstrip().startswith("robocopy")
        argv = ["cmd.exe", "/c", text]
        return argv, is_rc, display

    def _run_command(
        self,
        command: Union[str, CommandSpec, Sequence[str]],
        timeout: Optional[float],
        generation: int,
        use_conpty: bool,
    ) -> ExecutionResult:
        result = ExecutionResult(
            started_at=datetime.now(),
            status=OperationStatus.STARTED,
        )
        result.metadata["run_generation"] = generation
        result.metadata["conpty"] = bool(use_conpty)
        stdout_acc: List[str] = []
        stderr_acc: List[str] = []
        proc: Optional[subprocess.Popen] = None

        try:
            argv, is_robocopy, _display = self._normalize_argv(command)
            if not argv:
                result.exception = "Comando vazio"
                result.return_code = 1
                return result.finalize()

            prefix = "[ROBOCOPY] " if is_robocopy else ""

            if use_conpty and not is_robocopy:
                return self._run_conpty(
                    argv,
                    result=result,
                    generation=generation,
                    timeout=timeout,
                    prefix=prefix,
                    stdout_acc=stdout_acc,
                )

            creationflags = CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

            def on_out(line: str) -> None:
                stdout_acc.append(line)
                self.outputReceived.emit(line)

            def on_err(line: str) -> None:
                stderr_acc.append(line)
                self.errorReceived.emit(line)

            try:
                proc = popen_argv(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
            except FileNotFoundError:
                msg = f"Executável não encontrado: {argv[0]}"
                self.errorReceived.emit(msg)
                result.exception = msg
                result.return_code = 1
                return result.finalize()
            except OSError as exc:
                safe = redact_command_text(str(exc), passwords=self._passwords)
                msg = f"Falha ao iniciar processo: {safe}"
                self.errorReceived.emit(msg)
                result.exception = msg
                result.return_code = 1
                return result.finalize()

            with self._lock:
                if generation != self._run_generation:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    result.cancelled = True
                    result.remote_may_continue = True
                    return result.finalize()
                self.process = proc

            stdout_pipe = proc.stdout
            stderr_pipe = proc.stderr

            t_out = threading.Thread(
                target=lambda: _read_pipe(
                    stdout_pipe,
                    lambda ln: on_out(f"{prefix}{ln}" if prefix else ln),
                ),
                daemon=True,
            )
            t_err = threading.Thread(
                target=lambda: _read_pipe(
                    stderr_pipe,
                    lambda ln: on_err(f"{prefix}{ln}" if prefix else ln),
                ),
                daemon=True,
            )
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                result.timed_out = True
                result.remote_may_continue = True
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

            t_out.join(timeout=5)
            t_err.join(timeout=5)

            if self._cancel_requested and generation == self._run_generation:
                result.cancelled = True
                result.remote_may_continue = True

            result.return_code = proc.returncode
            result.stdout = "\n".join(stdout_acc)
            result.stderr = "\n".join(stderr_acc)

            if is_robocopy and result.return_code is not None:
                result.metadata["robocopy_success"] = is_robocopy_success(
                    result.return_code
                )
                if is_robocopy_success(result.return_code):
                    result.success = True
                    result.status = OperationStatus.COMPLETED
                    result.finished_at = datetime.now()
                    if result.started_at:
                        result.duration_seconds = (
                            result.finished_at - result.started_at
                        ).total_seconds()
                    return result

            return result.finalize()

        except Exception as exc:
            safe = redact_command_text(str(exc), passwords=self._passwords)
            self.errorReceived.emit(f"Error executing command: {safe}")
            result.exception = safe
            result.return_code = 1
            return result.finalize()
        finally:
            result.finished_at = result.finished_at or datetime.now()
            if result.started_at and result.duration_seconds is None:
                result.duration_seconds = (
                    result.finished_at - result.started_at
                ).total_seconds()
            with self._lock:
                if self.process is proc:
                    self.process = None
                if generation == self._run_generation:
                    self._last_result = result
                    self._conpty_terminate = None
                    self._conpty_write = None
            self._set_interactive(False)

    def _run_conpty(
        self,
        argv: List[str],
        *,
        result: ExecutionResult,
        generation: int,
        timeout: Optional[float],
        prefix: str,
        stdout_acc: List[str],
    ) -> ExecutionResult:
        from remoteops.core.conpty import ConPtySession

        session = ConPtySession(argv)
        try:
            session.start()
        except FileNotFoundError:
            msg = f"Executável não encontrado: {argv[0]}"
            self.errorReceived.emit(msg)
            result.exception = msg
            result.return_code = 1
            return result.finalize()
        except OSError as exc:
            safe = redact_command_text(str(exc), passwords=self._passwords)
            msg = f"Falha ao iniciar ConPTY: {safe}"
            self.errorReceived.emit(msg)
            result.exception = msg
            result.return_code = 1
            return result.finalize()

        with self._lock:
            if generation != self._run_generation:
                session.terminate()
                session.close()
                result.cancelled = True
                result.remote_may_continue = True
                return result.finalize()
            self._conpty_terminate = session.terminate
            self._conpty_write = session.write_input

        self._set_interactive(True)

        carry: List[str] = []

        def on_line(line: str) -> None:
            safe_line = redact_command_text(line, passwords=self._passwords)
            stdout_acc.append(safe_line)
            self.outputReceived.emit(safe_line)

        def on_partial(text: str) -> None:
            safe = redact_command_text(text, passwords=self._passwords)
            self.partialOutput.emit(safe)

        def on_chunk(chunk: str) -> None:
            _emit_conpty_text(
                chunk,
                prefix=prefix,
                on_line=on_line,
                carry=carry,
                on_partial=on_partial,
            )

        try:
            conpty_result = session.read_output_loop(
                on_chunk,
                should_cancel=lambda: self._cancel_requested
                or generation != self._run_generation,
                timeout=timeout,
            )
        finally:
            if carry and carry[0].strip():
                on_line(f"{prefix}{carry[0]}" if prefix else carry[0])
            self.partialOutput.emit("")
            with self._lock:
                if generation == self._run_generation:
                    self._conpty_terminate = None
                    self._conpty_write = None
            self._set_interactive(False)
            session.close()

        result.stdout = "\n".join(stdout_acc)
        result.return_code = conpty_result.return_code
        result.cancelled = bool(conpty_result.cancelled or self._cancel_requested)
        result.timed_out = bool(conpty_result.timed_out)
        result.remote_may_continue = result.cancelled or result.timed_out
        if conpty_result.exception:
            result.exception = redact_command_text(
                conpty_result.exception, passwords=self._passwords
            )
        return result.finalize()

    def _check_future(self, generation: int) -> None:
        with self._lock:
            fut = self.future
            current_gen = self._run_generation
        if fut is None or generation != current_gen:
            return
        if fut.done():
            try:
                result = fut.result()
                if not isinstance(result, ExecutionResult):
                    code = int(result) if result is not None else 1
                    result = ExecutionResult(return_code=code).finalize()
                if result.metadata.get("run_generation") != current_gen:
                    return
                self._last_result = result
                self.resultReady.emit(result)
                self.finished.emit(
                    result.return_code if result.return_code is not None else 1
                )
            except Exception as e:
                safe = redact_command_text(str(e), passwords=self._passwords)
                self.errorReceived.emit(safe)
                result = ExecutionResult(
                    return_code=1,
                    exception=safe,
                    status=OperationStatus.FAILED,
                )
                self.resultReady.emit(result)
                self.finished.emit(1)
            with self._lock:
                if self.future is fut:
                    self.future = None
        else:
            QTimer.singleShot(100, lambda: self._check_future(generation))

    def stop(self) -> None:
        """Cancela execução LOCAL (pipes ou ConPTY)."""
        with self._lock:
            self._cancel_requested = True
            proc = self.process
            fut = self.future
            terminate_conpty = self._conpty_terminate
            self._conpty_write = None
        if terminate_conpty is not None:
            try:
                terminate_conpty()
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
        if fut is not None and not fut.running() and not fut.done():
            fut.cancel()
        self._set_interactive(False)

    @property
    def last_result(self) -> Optional[ExecutionResult]:
        return self._last_result

    def shutdown(self, wait: bool = False) -> None:
        self.stop()
        self.executor.shutdown(wait=wait, cancel_futures=True)
