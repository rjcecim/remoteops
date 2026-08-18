"""
ConPTY (Windows Pseudo Console) — execução com saída capturável no log.

Usado pelo Executor para PsExec do botão Executar.
Não usar em PsInfo, Pesquisa nem no botão Desinstalar dessas abas.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Union

# VT/ANSI comum emitido pelo ConPTY — remove para o QTextEdit.
# Inclui CSI (?25h, 2J, H…), OSC (]0;title BEL) e códigos de 1 byte.
_ANSI_RE = re.compile(
    r"(?:"
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[P^_][^\x1b]*\x1b\\"  # DCS/PM/APC
    r"|\x1b[@-Z\\-_]"  # 2-byte (não inclui ']')
    r"|\x00"
    r")"
)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HPCON = wintypes.HANDLE
HRESULT = ctypes.c_long
PVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1)
INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102
WAIT_OBJECT_0 = 0x00000000
S_OK = 0


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", PVOID),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", PVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


kernel32.CreatePseudoConsole.argtypes = [
    COORD,
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(HPCON),
]
kernel32.CreatePseudoConsole.restype = HRESULT

kernel32.ResizePseudoConsole.argtypes = [HPCON, COORD]
kernel32.ResizePseudoConsole.restype = HRESULT

kernel32.ClosePseudoConsole.argtypes = [HPCON]
kernel32.ClosePseudoConsole.restype = None

kernel32.CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.POINTER(wintypes.HANDLE),
    ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.DWORD,
]
kernel32.CreatePipe.restype = wintypes.BOOL

kernel32.SetHandleInformation.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
]
kernel32.SetHandleInformation.restype = wintypes.BOOL

kernel32.InitializeProcThreadAttributeList.argtypes = [
    PVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(SIZE_T),
]
kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

kernel32.UpdateProcThreadAttribute.argtypes = [
    PVOID,
    wintypes.DWORD,
    SIZE_T,
    PVOID,
    SIZE_T,
    PVOID,
    ctypes.POINTER(SIZE_T),
]
kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

kernel32.DeleteProcThreadAttributeList.argtypes = [PVOID]
kernel32.DeleteProcThreadAttributeList.restype = None

kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    PVOID,
    PVOID,
    wintypes.BOOL,
    wintypes.DWORD,
    PVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
kernel32.CreateProcessW.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.GetExitCodeProcess.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL

kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL

kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.WriteFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.PeekNamedPipe.restype = wintypes.BOOL


def strip_ansi(text: str) -> str:
    if not text:
        return ""
    # Cursor absolute (ConPTY): vira quebra de linha para não colar textos.
    text = re.sub(r"\x1b\[\d+;\d+H", "\n", text)
    text = _ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _argv_to_command_line(argv: Sequence[str]) -> str:
    """Command line Windows (regras de list2cmdline / CommandLineToArgvW)."""
    return subprocess.list2cmdline([str(a) for a in argv])


def _close_handle(handle) -> None:
    if not handle:
        return
    try:
        val = int(handle) if not isinstance(handle, int) else handle
        if val and val != -1:
            kernel32.CloseHandle(wintypes.HANDLE(val))
    except Exception:
        pass


@dataclass
class ConPtyResult:
    return_code: int
    output: str
    cancelled: bool = False
    timed_out: bool = False
    exception: str = ""


class ConPtySession:
    """Sessão ConPTY: processo filho + leitura da saída."""

    def __init__(self, argv: Sequence[str], *, cols: int = 120, rows: int = 30):
        if not argv:
            raise ValueError("argv vazio")
        self.argv = [str(a) for a in argv]
        self.cols = max(20, int(cols))
        self.rows = max(5, int(rows))
        self._hpc: Optional[HPCON] = None
        self._pi: Optional[PROCESS_INFORMATION] = None
        self._attr_buf = None
        self._h_out_read: Optional[wintypes.HANDLE] = None
        self._h_in_write: Optional[wintypes.HANDLE] = None
        self._io_lock = threading.Lock()

    def write_input(self, data: Union[bytes, str]) -> bool:
        """Envia bytes/texto ao stdin do ConPTY (teclado da sessão interativa)."""
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        if not data:
            return True
        with self._io_lock:
            handle = self._h_in_write
            if not handle:
                return False
            written = wintypes.DWORD(0)
            arr = (ctypes.c_char * len(data)).from_buffer_copy(data)
            ok = kernel32.WriteFile(
                handle, arr, len(data), ctypes.byref(written), None
            )
            return bool(ok)

    def resize(self, cols: int, rows: int) -> bool:
        """ResizePseudoConsole — wrapping de dir/tabelas no console integrado."""
        cols = max(20, int(cols))
        rows = max(5, int(rows))
        self.cols = cols
        self.rows = rows
        hpc = self._hpc
        if not hpc:
            return False
        hr = kernel32.ResizePseudoConsole(hpc, COORD(cols, rows))
        return hr == S_OK

    def start(self) -> None:
        # Espelha o sample EchoCon da Microsoft:
        #   CreatePipe(&hPipePTYIn, &hPipeOut)  → input: PTY lê, host escreve
        #   CreatePipe(&hPipeIn, &hPipePTYOut) → output: host lê, PTY escreve
        h_pty_in = wintypes.HANDLE()
        h_in_write = wintypes.HANDLE()
        h_out_read = wintypes.HANDLE()
        h_pty_out = wintypes.HANDLE()

        if not kernel32.CreatePipe(
            ctypes.byref(h_pty_in), ctypes.byref(h_in_write), None, 0
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.CreatePipe(
            ctypes.byref(h_out_read), ctypes.byref(h_pty_out), None, 0
        ):
            _close_handle(h_pty_in)
            _close_handle(h_in_write)
            raise ctypes.WinError(ctypes.get_last_error())

        hpc = HPCON()
        hr = kernel32.CreatePseudoConsole(
            COORD(self.cols, self.rows),
            h_pty_in,
            h_pty_out,
            0,
            ctypes.byref(hpc),
        )
        _close_handle(h_pty_in)
        _close_handle(h_pty_out)
        if hr != S_OK:
            _close_handle(h_in_write)
            _close_handle(h_out_read)
            raise OSError(f"CreatePseudoConsole falhou (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

        self._hpc = hpc
        self._h_in_write = h_in_write
        self._h_out_read = h_out_read
        self._output_chunks: List[bytes] = []
        self._reader_stop = threading.Event()

        def _early_reader() -> None:
            handle = self._h_out_read
            buf = (ctypes.c_char * 4096)()
            while not self._reader_stop.is_set() and handle:
                read = wintypes.DWORD(0)
                ok = kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
                if not ok or read.value == 0:
                    break
                self._output_chunks.append(bytes(buf[: read.value]))

        # Listener antes do CreateProcess (como EchoCon).
        self._reader_thread = threading.Thread(
            target=_early_reader, name="conpty-reader", daemon=True
        )
        self._reader_thread.start()

        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if size.value == 0:
            self.close()
            raise OSError("InitializeProcThreadAttributeList: tamanho inválido")

        attr_buf = ctypes.create_string_buffer(size.value)
        self._attr_buf = attr_buf
        if not kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(size)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

        # EchoCon / docs: lpValue = HPCON por valor (HANDLE), tamanho sizeof(HPCON).
        if not kernel32.UpdateProcThreadAttribute(
            attr_buf,
            0,
            SIZE_T(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE),
            hpc,
            SIZE_T(ctypes.sizeof(HPCON)),
            None,
            None,
        ):
            kernel32.DeleteProcThreadAttributeList(attr_buf)
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

        si = STARTUPINFOEXW()
        ctypes.memset(ctypes.byref(si), 0, ctypes.sizeof(si))
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # Evita herdar stdout/stderr redirecionados do pai (ex.: console/IDE),
        # o que faria o filho ignorar o ConPTY.
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = INVALID_HANDLE_VALUE
        si.StartupInfo.hStdOutput = INVALID_HANDLE_VALUE
        si.StartupInfo.hStdError = INVALID_HANDLE_VALUE
        si.lpAttributeList = ctypes.cast(attr_buf, PVOID)

        cmdline = _argv_to_command_line(self.argv)
        cmdline_buf = ctypes.create_unicode_buffer(cmdline)
        pi = PROCESS_INFORMATION()
        ok = kernel32.CreateProcessW(
            None,
            cmdline_buf,
            None,
            None,
            False,
            EXTENDED_STARTUPINFO_PRESENT,
            None,
            None,
            ctypes.byref(si.StartupInfo),
            ctypes.byref(pi),
        )
        # Lista de atributos só pode ser destruída após CreateProcess.
        kernel32.DeleteProcThreadAttributeList(attr_buf)
        self._attr_buf = None

        if not ok:
            err = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(err)

        self._pi = pi

    @property
    def process_handle(self):
        return self._pi.hProcess if self._pi else None

    @property
    def pid(self) -> Optional[int]:
        return int(self._pi.dwProcessId) if self._pi else None

    def _read_available(self) -> bytes:
        handle = self._h_out_read
        if not handle:
            return b""
        avail = wintypes.DWORD(0)
        if not kernel32.PeekNamedPipe(
            handle, None, 0, None, ctypes.byref(avail), None
        ):
            return b""
        if avail.value == 0:
            return b""
        buf = (ctypes.c_char * min(avail.value, 8192))()
        read = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None):
            return b""
        return bytes(buf[: read.value])

    def _drain_output(self, on_chunk: Callable[[str], None], chunks: List[str]) -> None:
        while True:
            raw = self._read_available()
            if not raw:
                return
            text = strip_ansi(
                raw.decode("utf-8", errors="replace")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            if not text:
                continue
            chunks.append(text)
            try:
                on_chunk(text)
            except Exception:
                pass

    def read_output_loop(
        self,
        on_chunk: Callable[[str], None],
        *,
        should_cancel: Optional[Callable[[], bool]] = None,
        timeout: Optional[float] = None,
    ) -> ConPtyResult:
        """Aguarda o processo; o reader já foi iniciado em ``start()``."""
        if self._pi is None:
            raise RuntimeError("Sessão ConPTY não iniciada")

        chunks: List[str] = []
        cancelled = False
        timed_out = False
        deadline = (time.monotonic() + float(timeout)) if timeout else None
        emitted = 0

        def _flush_new() -> None:
            nonlocal emitted
            raw = b"".join(self._output_chunks[emitted:])
            emitted = len(self._output_chunks)
            if not raw:
                return
            text = strip_ansi(
                raw.decode("utf-8", errors="replace")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            if not text:
                return
            chunks.append(text)
            try:
                on_chunk(text)
            except Exception:
                pass

        try:
            while True:
                _flush_new()
                if should_cancel and should_cancel():
                    cancelled = True
                    self.terminate()
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    self.terminate()
                    break
                wait = kernel32.WaitForSingleObject(self._pi.hProcess, 200)
                if wait == WAIT_OBJECT_0:
                    break
                if wait != WAIT_TIMEOUT:
                    break
            time.sleep(0.5)
            _flush_new()
        finally:
            with self._io_lock:
                if self._h_in_write is not None:
                    _close_handle(self._h_in_write)
                    self._h_in_write = None
            if self._hpc is not None:
                try:
                    kernel32.ClosePseudoConsole(self._hpc)
                except Exception:
                    pass
                self._hpc = None
            if self._h_out_read is not None:
                _close_handle(self._h_out_read)
                self._h_out_read = None
            self._reader_stop.set()
            t = getattr(self, "_reader_thread", None)
            if t is not None:
                t.join(timeout=2.0)
            _flush_new()

        code = wintypes.DWORD(1)
        if self._pi is not None:
            kernel32.GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code))
        return ConPtyResult(
            return_code=int(code.value),
            output="".join(chunks),
            cancelled=cancelled,
            timed_out=timed_out,
        )

    def terminate(self) -> None:
        if self._pi is not None and self._pi.hProcess:
            try:
                kernel32.TerminateProcess(self._pi.hProcess, 1)
            except Exception:
                pass

    def close(self) -> None:
        with self._io_lock:
            if self._h_in_write is not None:
                _close_handle(self._h_in_write)
                self._h_in_write = None
        if self._hpc is not None:
            try:
                kernel32.ClosePseudoConsole(self._hpc)
            except Exception:
                pass
            self._hpc = None
        if self._h_out_read is not None:
            _close_handle(self._h_out_read)
            self._h_out_read = None
        if self._pi is not None:
            _close_handle(self._pi.hThread)
            _close_handle(self._pi.hProcess)
            self._pi = None


def run_argv_conpty(
    argv: Sequence[str],
    *,
    on_output: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: Optional[float] = None,
    cols: int = 120,
    rows: int = 30,
) -> ConPtyResult:
    """
    Executa argv sob ConPTY e entrega texto (já sem ANSI) via ``on_output``.
    """
    session = ConPtySession(argv, cols=cols, rows=rows)
    try:
        session.start()
    except FileNotFoundError as exc:
        return ConPtyResult(return_code=1, output="", exception=str(exc))
    except OSError as exc:
        return ConPtyResult(return_code=1, output="", exception=str(exc))

    def _emit(chunk: str) -> None:
        if on_output:
            on_output(chunk)

    try:
        return session.read_output_loop(
            _emit, should_cancel=should_cancel, timeout=timeout
        )
    finally:
        session.close()
