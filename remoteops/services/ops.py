"""Casos de uso: execução, desinstalação remota e RustDesk."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from remoteops.core.models import CommandSpec, OperationStatus
from remoteops.core.win_cmd import open_external_console_argv_keep_open, quote_for_cmd
from remoteops.core.win_cmdline import split_windows_command_line
from remoteops.utils.app_logging import log_operation
from remoteops.utils.pstools import resolve_pstools_tool
from remoteops.utils.redaction import REDACTED, redact_command_text


@dataclass
class CredentialContext:
    """Credenciais de curta duração — limpar após uso quando possível."""

    user: str = ""
    password: str = ""

    def clear(self) -> None:
        """Reduz lifetime da referência; não promete zeroização de memória."""
        self.password = ""

    @property
    def passwords(self) -> List[str]:
        return [self.password] if self.password.strip() else []


def materialize_password_in_argv(
    argv: Sequence[str],
    password: str,
) -> List[str]:
    """
    Injeta a senha real no slot ``-p <placeholder>`` do argv.

    Usado somente no momento da execução. O CommandBuilder mantém apenas
    ``********`` no estado persistente.
    """
    out = [str(a) for a in argv]
    if not (password or "").strip():
        return out
    i = 0
    while i < len(out):
        if out[i].lower() == "-p" and i + 1 < len(out):
            if out[i + 1] in (REDACTED, "********"):
                out[i + 1] = password
            i += 2
            continue
        i += 1
    return out


def resolve_psexec_exe(pstools_path: str) -> str:
    exe = resolve_pstools_tool(pstools_path, ("PsExec64.exe", "PsExec.exe"))
    if exe:
        return os.path.normpath(exe.replace('"', "").replace("'", ""))
    return "PsExec.exe"


def build_psexec_argv(
    *,
    psexec_exe: str,
    host: str,
    remote_argv: Sequence[str],
    creds: Optional[CredentialContext] = None,
    extra_flags: Optional[Sequence[str]] = None,
    include_password: bool = True,
) -> List[str]:
    host = (host or "").strip().strip("\\")
    argv: List[str] = [psexec_exe, f"\\\\{host}"]
    creds = creds or CredentialContext()
    if creds.user.strip():
        argv.extend(["-u", creds.user.strip()])
    if creds.password.strip():
        if include_password:
            argv.extend(["-p", creds.password])
        else:
            argv.extend(["-p", REDACTED])
    if extra_flags:
        argv.extend(list(extra_flags))
    argv.extend(list(remote_argv))
    return argv


@dataclass
class LaunchResult:
    """
    Resultado do *lançamento* local — não necessariamente do comando remoto.

    Com ConPTY (botão Executar), a saída e o exit code local são monitorados
    pelo Executor. Desinstalação PsInfo/Pesquisa permanece em terminal externo.
    """

    ok: bool
    display_command: str
    message: str = ""
    robocopy_started: bool = False
    status: OperationStatus = OperationStatus.UNKNOWN
    remote_monitored: bool = False


class CommandExecutionService:
    """Orquestra preview sanitizado + Robocopy (pipes) / PsExec (ConPTY no log)."""

    def __init__(self, executor, log_fn: Optional[Callable[[str], None]] = None):
        self.executor = executor
        self._log = log_fn or (lambda _m: None)
        self._run_enabled_cb: Optional[Callable[[bool], None]] = None
        self._stop_enabled_cb: Optional[Callable[[bool], None]] = None
        # Robocopy terminou, mas o PsExec do plano ainda vai (ou acabou de) começar.
        self._awaiting_followup = False
        self._plan_cancelled = False

    @property
    def awaiting_followup(self) -> bool:
        return bool(self._awaiting_followup)

    def cancel_plan(self) -> None:
        """Impede o PsExec encadeado após Robocopy se o usuário pediu parada."""
        self._plan_cancelled = True
        self._awaiting_followup = False

    def set_button_callbacks(
        self,
        set_run_enabled: Callable[[bool], None],
        set_stop_enabled: Callable[[bool], None],
    ) -> None:
        self._run_enabled_cb = set_run_enabled
        self._stop_enabled_cb = set_stop_enabled

    def launch_plan(
        self,
        plan: List[CommandSpec],
        *,
        passwords: Optional[Sequence[str]] = None,
        creds: Optional[CredentialContext] = None,
    ) -> LaunchResult:
        passwords = list(passwords or [])
        if creds and creds.password.strip() and creds.password not in passwords:
            passwords.append(creds.password)
        password = passwords[0] if passwords else ""

        if not plan:
            return LaunchResult(
                ok=False,
                display_command="",
                message="Nenhum comando",
                status=OperationStatus.FAILED,
            )

        self._plan_cancelled = False
        self._awaiting_followup = False

        displays = [s.sanitized_display(passwords) for s in plan]
        full_display = "\n".join(d for d in displays if d)
        self._log(f"[DEBUG] Comando completo: {full_display}")
        log_operation("execute", detail=full_display, passwords=passwords)

        robocopy_specs = [
            s for s in plan if (s.metadata or {}).get("kind") == "robocopy"
        ]
        psexec_specs = [
            s for s in plan if (s.metadata or {}).get("kind") != "robocopy"
        ]

        if robocopy_specs and psexec_specs:
            rc = robocopy_specs[0]
            px = psexec_specs[0]

            def after_robocopy(exit_code: int) -> None:
                try:
                    self.executor.finished.disconnect(after_robocopy)
                except Exception:
                    pass
                from remoteops.core.models import is_robocopy_success

                if self._plan_cancelled:
                    self._awaiting_followup = False
                    return

                if is_robocopy_success(exit_code):
                    self._log(
                        f"Robocopy OK (código {exit_code}). Iniciando PsExec (ConPTY)..."
                    )
                    launch = self._launch_psexec_conpty(px, password=password)
                    self._awaiting_followup = False
                    self._log(launch.message)
                    if not launch.ok:
                        if self._run_enabled_cb:
                            self._run_enabled_cb(True)
                        if self._stop_enabled_cb:
                            self._stop_enabled_cb(False)
                else:
                    self._awaiting_followup = False
                    self._log(
                        f"Robocopy falhou (código {exit_code}). "
                        "PsExec nao sera executado."
                    )
                    if self._run_enabled_cb:
                        self._run_enabled_cb(True)
                    if self._stop_enabled_cb:
                        self._stop_enabled_cb(False)

            self._awaiting_followup = True
            self.executor.finished.connect(after_robocopy)
            self.executor.run(rc, passwords=passwords, use_conpty=False)
            return LaunchResult(
                ok=True,
                display_command=full_display,
                robocopy_started=True,
                message="Robocopy iniciado; PsExec pendente do resultado da cópia.",
                status=OperationStatus.STARTED,
                remote_monitored=True,
            )

        target = psexec_specs[0] if psexec_specs else plan[-1]
        if (target.metadata or {}).get("kind") == "robocopy":
            self.executor.run(target, passwords=passwords, use_conpty=False)
            msg = "Robocopy iniciado; saída no log."
            self._log(msg)
            return LaunchResult(
                ok=True,
                display_command=full_display,
                message=msg,
                status=OperationStatus.STARTED,
                remote_monitored=True,
            )

        launch = self._launch_psexec_conpty(target, password=password)
        self._log(launch.message)
        if not launch.ok:
            if self._run_enabled_cb:
                self._run_enabled_cb(True)
            if self._stop_enabled_cb:
                self._stop_enabled_cb(False)
        return LaunchResult(
            ok=launch.ok,
            display_command=full_display,
            message=launch.message,
            status=launch.status,
            remote_monitored=launch.remote_monitored,
        )

    def _launch_psexec_conpty(
        self, spec: CommandSpec, *, password: str = ""
    ) -> LaunchResult:
        """
        PsExec do botão Executar: ConPTY → saída no log + exit code local.

        Limitação inerente: com ``-p``, a senha fica na command line do processo.
        Não é gravada em arquivo/preview; o log redige eco acidental.
        """
        argv = materialize_password_in_argv(spec.argv, password)
        try:
            self.executor.run(
                CommandSpec.from_argv(
                    argv,
                    has_secrets=bool(password),
                    passwords=[password] if password else None,
                    metadata=dict(spec.metadata or {}) | {"kind": "psexec"},
                    prefer_external_console=False,
                ),
                passwords=[password] if password else None,
                use_conpty=True,
            )
        except Exception as exc:
            safe = redact_command_text(
                str(exc), passwords=[password] if password else None
            )
            msg = f"Falha ao lançar PsExec (ConPTY): {safe}"
            return LaunchResult(
                ok=False,
                display_command=spec.display_command,
                message=msg,
                status=OperationStatus.FAILED,
                remote_monitored=False,
            )
        return LaunchResult(
            ok=True,
            display_command=spec.display_command,
            message="Execução iniciada via ConPTY; digite no console de saída (Enter envia, Ctrl+C interrompe).",
            status=OperationStatus.STARTED,
            remote_monitored=True,
        )


@dataclass
class UninstallLaunchResult:
    ok: bool
    display_command: str
    message: str = ""
    status: OperationStatus = OperationStatus.UNKNOWN


class RemoteUninstallService:
    """
    Desinstalação remota via PsExec (-s) em terminal externo.

    Intencionalmente NÃO usa ConPTY — o botão Desinstalar do PsInfo/Pesquisa
    abre console externo (janela permanece aberta). Nunca grava senha em
    arquivos temporários.
    """

    def run(
        self,
        *,
        host: str,
        remote_cmd: str,
        app_label: str,
        pstools_path: str,
        creds: CredentialContext,
        log_tag: str = "PSINFO",
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> UninstallLaunchResult:
        log = log_fn or (lambda _m: None)
        host = (host or "").strip().strip("\\")
        remote_cmd = (remote_cmd or "").strip()
        if not host:
            msg = f"[{log_tag}] Host remoto não informado para desinstalação."
            log(msg)
            return UninstallLaunchResult(
                ok=False, display_command="", message=msg, status=OperationStatus.FAILED
            )
        if not remote_cmd:
            msg = f"[{log_tag}] Comando de desinstalação vazio."
            log(msg)
            return UninstallLaunchResult(
                ok=False, display_command="", message=msg, status=OperationStatus.FAILED
            )

        psexec_exe = resolve_psexec_exe(pstools_path)

        # Sempre parse Windows; se não for cmd explícito, envolve com cmd /c
        low = remote_cmd.lower().lstrip()
        if low.startswith("cmd ") or low.startswith("cmd.exe"):
            remote_argv = split_windows_command_line(remote_cmd)
        else:
            remote_argv = ["cmd", "/c", remote_cmd]

        extra_flags = ["-s", "-accepteula", "-nobanner"]
        real_argv = build_psexec_argv(
            psexec_exe=psexec_exe,
            host=host,
            remote_argv=remote_argv,
            creds=creds,
            extra_flags=extra_flags,
            include_password=True,
        )
        display_argv = build_psexec_argv(
            psexec_exe=psexec_exe,
            host=host,
            remote_argv=remote_argv,
            creds=creds,
            extra_flags=extra_flags,
            include_password=False,
        )
        display_cmd = " ".join(
            quote_for_cmd(a) if (" " in a or "\t" in a) else a for a in display_argv
        )
        display_cmd = redact_command_text(display_cmd, passwords=creds.passwords)

        log(f"[{log_tag}] Desinstalando em {host}: {app_label}")
        log(f"[{log_tag}] {display_cmd}")
        log_operation("uninstall", detail=display_cmd, passwords=creds.passwords)

        try:
            # Terminal externo (sem ConPTY): pedido explícito para PsInfo/Pesquisa.
            open_external_console_argv_keep_open(real_argv)
            msg = (
                f"[{log_tag}] Execução iniciada em terminal externo "
                "(janela permanece aberta); resultado remoto não monitorado."
            )
            log(msg)
            return UninstallLaunchResult(
                ok=True,
                display_command=display_cmd,
                message=msg,
                status=OperationStatus.STARTED,
            )
        except OSError as exc:
            safe = redact_command_text(str(exc), passwords=creds.passwords)
            msg = f"[{log_tag}] Falha ao abrir terminal: {safe}"
            log(msg)
            return UninstallLaunchResult(
                ok=False,
                display_command=display_cmd,
                message=msg,
                status=OperationStatus.FAILED,
            )


RUSTDESK_REMOTE_PATHS = (
    r"C:\Program Files\RustDesk\rustdesk.exe",
    r"C:\Program Files (x86)\RustDesk\rustdesk.exe",
)


class RustDeskService:
    """Localiza RustDesk remoto, obtém ID e abre conexão local."""

    remote_paths = RUSTDESK_REMOTE_PATHS

    def build_get_id_spec(
        self,
        *,
        host: str,
        pstools_path: str,
        creds: CredentialContext,
        remote_path: str,
    ) -> CommandSpec:
        psexec_exe = resolve_psexec_exe(pstools_path)
        argv = build_psexec_argv(
            psexec_exe=psexec_exe,
            host=host,
            remote_argv=[remote_path, "--get-id"],
            creds=creds,
            extra_flags=["-h", "-s", "-accepteula", "-nobanner"],
            include_password=True,
        )
        return CommandSpec.from_argv(
            argv,
            has_secrets=bool(creds.passwords),
            passwords=creds.passwords,
            metadata={"kind": "rustdesk"},
        )

    @staticmethod
    def extract_id(lines: Sequence[str]) -> str:
        for ln in lines:
            cand = re.sub(r"\D", "", str(ln))
            if len(cand) >= 6:
                return cand
        return ""

    @staticmethod
    def is_not_found(err_text: str) -> bool:
        markers = [
            "could not be found",
            "não foi possível encontrar",
            "nao foi possivel encontrar",
            "não foi encontrado",
            "nao foi encontrado",
            "o sistema não pode encontrar",
            "o sistema nao pode encontrar",
        ]
        low = (err_text or "").lower()
        return any(m in low for m in markers)

    @staticmethod
    def is_access_error(err_text: str) -> bool:
        low = (err_text or "").lower()
        return "couldn't access" in low or "couldnt access" in low

    def find_local_rustdesk(self) -> str:
        from remoteops.utils.pstools import probe_rustdesk_local, rustdesk_local_candidates

        info = probe_rustdesk_local()
        if info.get("found"):
            return str(info["path"])
        # Fallback: primeiro candidato conhecido / PATH
        candidates = rustdesk_local_candidates()
        return candidates[0] if candidates else "rustdesk.exe"

    def open_local_connect(self, rustdesk_id: str) -> Tuple[bool, str]:
        from remoteops.core.win_cmd import popen_argv

        local_exe = self.find_local_rustdesk()
        try:
            popen_argv([local_exe, "--connect", rustdesk_id])
            return True, f"{local_exe} --connect {rustdesk_id}"
        except FileNotFoundError:
            return False, "RustDesk não encontrado no PC local."
        except OSError as exc:
            return False, str(exc)
