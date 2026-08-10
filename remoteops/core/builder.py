"""Montagem estruturada de comandos Robocopy, PsExec e msiexec."""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence, Union

from remoteops.core.models import CommandSpec, FileSelection
from remoteops.core.win_cmdline import split_windows_command_line
from remoteops.utils.pstools import resolve_pstools_tool
from remoteops.utils.redaction import REDACTED, format_argv_for_display, redact_command_text


def _extract_flag_value(combo_text: str) -> str:
    """Extrai o valor da flag do texto do combobox que contém descrição."""
    if not combo_text or combo_text == "Nenhum":
        return ""
    if "(" in combo_text:
        return combo_text.split("(")[0].strip()
    return combo_text.strip()


def _normalize_robocopy_dest(dest: str) -> str:
    dest = (dest or "").strip().replace('"', "").replace("'", "").strip()
    if dest.lower().startswith("c:"):
        dest = dest[2:].lstrip("\\/").replace("/", "\\")
    return "\\".join(part for part in dest.split("\\") if part)


def _split_extra_args(extra: str) -> List[str]:
    """Divide argumentos extras preservando aspas simples (Windows-ish)."""
    s = (extra or "").strip()
    if not s:
        return []
    parts: List[str] = []
    buf: List[str] = []
    in_quote = False
    quote_char = ""
    for ch in s:
        if in_quote:
            if ch == quote_char:
                in_quote = False
            else:
                buf.append(ch)
            continue
        if ch in ('"', "'"):
            in_quote = True
            quote_char = ch
            continue
        if ch.isspace():
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


class CommandBuilder:
    """
    Monta comandos robocopy, PsExec e msiexec.

    Regras de segurança:
    - Métodos ``build_*`` que retornam ``str`` são SEMPRE seguros para display
      (senha mascarada).
    - Use ``build_*_spec`` / ``build_execution_plan`` para obter ``CommandSpec``
      com ``args`` reais (podem conter senha) e ``display_command`` sanitizado.
    - A UI nunca deve receber a senha para mascarar depois.
    """

    def __init__(self) -> None:
        self.psexec_params: dict = {}
        self.msi_params: dict = {}
        self.robocopy_params: Optional[dict] = None
        self.file_path: Optional[str] = None
        self.folder_path: Optional[str] = None
        self.selection: Optional[dict] = None
        self.selection_mode: str = "file"
        self.powershell_params: dict = {}
        self.cmd_params: dict = {}
        # Credencial efêmera: o builder só sabe SE há senha (nunca o valor).
        self._has_password: bool = False

    # ── setters (API legada preservada) ─────────────────────────────────

    def set_file(self, file_path: Union[str, dict, FileSelection, None]) -> None:
        """
        Define arquivo. Aceita path string OU dict/FileSelection (compat UI).
        """
        sel = FileSelection.from_any(file_path)
        if sel is None:
            self.file_path = None
            return
        if isinstance(file_path, dict) or isinstance(file_path, FileSelection):
            self.set_file_selection(sel)
            return
        self.file_path = sel.file
        if not hasattr(self, "selection") or self.selection is None:
            self.selection = {"mode": "file", "file": self.file_path, "folder": None}
            self.selection_mode = "file"
            self.folder_path = None

    def set_psexec_params(self, params: dict) -> None:
        """
        Atualiza parâmetros operacionais do PsExec.

        A chave ``password`` é consumida apenas para definir ``has_password``
        e **não** é armazenada no estado do builder. Para preview basta o
        placeholder ``-p ********``; a senha real só entra no argv na execução.
        """
        clean = dict(params or {})
        raw_pwd = clean.pop("password", None)
        if "has_password" in clean:
            self._has_password = bool(clean.pop("has_password"))
        else:
            self._has_password = bool(str(raw_pwd or "").strip())
        # Defesa: remover qualquer residual
        clean.pop("password", None)
        self.psexec_params = clean

    @property
    def has_password(self) -> bool:
        return self._has_password

    def set_msi_params(self, params: dict) -> None:
        self.msi_params = params or {}

    def set_robocopy_params(self, params: Optional[dict]) -> None:
        self.robocopy_params = params

    def set_powershell_params(self, params: dict) -> None:
        self.powershell_params = params or {}

    def set_cmd_params(self, params: dict) -> None:
        self.cmd_params = params or {}

    def set_file_selection(self, selection: Any) -> None:
        sel = FileSelection.from_any(selection)
        if sel is None:
            self.selection = None
            self.file_path = None
            self.folder_path = None
            self.selection_mode = "file"
            return
        self.selection = {"mode": sel.mode, "file": sel.file, "folder": sel.folder}
        self.file_path = sel.file
        self.folder_path = sel.folder
        self.selection_mode = sel.mode or "file"

    def _passwords_for_redaction(self) -> List[str]:
        # Builder não guarda senha; redaction estrutural cobre -p ********.
        return []

    # ── Robocopy ────────────────────────────────────────────────────────

    def build_robocopy(self) -> str:
        spec = self.build_robocopy_spec()
        return spec.display_command if spec else ""

    def build_robocopy_spec(self) -> Optional[CommandSpec]:
        if not self.robocopy_params or not self.psexec_params:
            return None
        if not self.selection:
            return None
        if self.selection_mode == "folder" and self.folder_path:
            return self._build_robocopy_folder_spec()
        return self._build_robocopy_file_spec()

    def _build_robocopy_file_spec(self) -> Optional[CommandSpec]:
        if not self.robocopy_params or not self.psexec_params:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: parâmetros de robocopy ou psexec ausentes",
            )
        if not self.file_path:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: arquivo não selecionado",
            )
        dest = _normalize_robocopy_dest(self.robocopy_params.get("dest") or "")
        if not dest:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: destino de cópia não especificado",
            )
        switches = self.robocopy_params.get(
            "switches", "/NFL /NDL /NJH /NJS /nc /ns /np"
        )
        file_path = os.path.normpath(self.file_path.strip()) if self.file_path else ""
        file_path = os.path.abspath(file_path) if file_path else ""
        src_dir = os.path.dirname(file_path) if file_path else ""
        file_name = os.path.basename(file_path) if file_path else ""
        if not src_dir or not os.path.isdir(src_dir):
            return CommandSpec(
                executable="robocopy",
                display_command=f"# Erro: Diretório de origem não encontrado: {src_dir}",
            )
        host = (self.psexec_params.get("host") or "").strip().strip("\\")
        if not host:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: host remoto não especificado",
            )
        dest_unc = f"\\\\{host}\\C$\\{dest}"
        argv = ["robocopy", src_dir, dest_unc, file_name, *_split_extra_args(switches)]
        return CommandSpec.from_argv(argv, metadata={"kind": "robocopy"})

    def _build_robocopy_folder_spec(self) -> Optional[CommandSpec]:
        if not self.robocopy_params or not self.psexec_params:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: parâmetros de robocopy ou psexec ausentes",
            )
        if not self.folder_path:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: pasta não selecionada",
            )
        dest = _normalize_robocopy_dest(self.robocopy_params.get("dest") or "")
        if not dest:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: destino de cópia não especificado",
            )
        switches = self.robocopy_params.get(
            "switches", "/NFL /NDL /NJH /NJS /nc /ns /np"
        )
        src_dir = self.folder_path
        folder_name = (
            os.path.basename(os.path.normpath(self.folder_path))
            if self.folder_path
            else ""
        )
        host = (self.psexec_params.get("host") or "").strip().strip("\\")
        if not host:
            return CommandSpec(
                executable="robocopy",
                display_command="# Erro: host remoto não especificado",
            )
        if not src_dir or not os.path.isdir(src_dir):
            return CommandSpec(
                executable="robocopy",
                display_command=f"# Erro: Diretório de origem não encontrado: {src_dir}",
            )
        dest_unc = f"\\\\{host}\\C$\\{dest}\\{folder_name}"
        argv = ["robocopy", src_dir, dest_unc, "/E", *_split_extra_args(switches)]
        return CommandSpec.from_argv(argv, metadata={"kind": "robocopy"})

    # ── PsExec base ─────────────────────────────────────────────────────

    def _resolve_psexec_path(self) -> str:
        pstools = (self.psexec_params.get("psexec_path") or "").strip()
        psexec_path = resolve_pstools_tool(pstools, ("PsExec64.exe", "PsExec.exe"))
        if psexec_path:
            return os.path.normpath(psexec_path.replace('"', "").replace("'", ""))
        return "PsExec.exe"

    def _base_psexec_argv(self, *, include_password: bool = False) -> List[str]:
        """
        Monta argv base do PsExec.

        O builder **nunca** embute a senha bruta. Quando há senha configurada,
        inclui ``-p ********`` para preview. A injeção do valor real ocorre
        apenas na execução (``services.ops.materialize_password_in_argv``).

        O parâmetro ``include_password`` é mantido por compatibilidade de
        assinatura; o valor real da senha não está disponível neste objeto.
        """
        del include_password  # senha bruta não vive no builder
        host = (self.psexec_params.get("host") or "").strip().strip("\\")
        cmd: List[str] = [self._resolve_psexec_path(), f"\\\\{host}"]

        user = (self.psexec_params.get("user") or "").strip()
        if user:
            cmd.extend(["-u", user])

        if self._has_password:
            cmd.extend(["-p", REDACTED])

        for flag in ("-h", "-s", "-l"):
            if self.psexec_params.get(flag):
                cmd.append(flag)

        if self.psexec_params.get("session_interactive"):
            session_id = self.psexec_params.get("session_id", 0)
            if session_id == 0:
                cmd.append("-i")
            else:
                cmd.extend(["-i", str(session_id)])

        priority_value = _extract_flag_value(self.psexec_params.get("priority", ""))
        if priority_value and priority_value != "Nenhum":
            cmd.append(priority_value)

        affinity_value = (self.psexec_params.get("affinity") or "").strip()
        if affinity_value:
            cmd.extend(["-a", affinity_value])

        group_value = _extract_flag_value(self.psexec_params.get("group", ""))
        if group_value and group_value != "Nenhum":
            if "(" in group_value:
                group_value = group_value.split("(")[0].strip()
            cmd.extend(["-g", group_value])

        timeout = self.psexec_params.get("timeout") or 0
        try:
            timeout_i = int(timeout)
        except (TypeError, ValueError):
            timeout_i = 0
        if timeout_i > 0:
            cmd.extend(["-n", str(timeout_i)])

        skip_copy_flags = self.robocopy_params is not None
        for flag in ("-d", "-e", "-c", "-f", "-v", "-accepteula", "-nobanner"):
            if flag in ("-c", "-f") and skip_copy_flags:
                continue
            if self.psexec_params.get(flag):
                cmd.append(flag)
        return cmd

    def _remote_path_after_robocopy(self) -> Optional[str]:
        if not self.robocopy_params:
            return None
        dest = _normalize_robocopy_dest(self.robocopy_params.get("dest") or "")
        if not dest:
            return None
        if self.selection_mode == "folder" and self.folder_path:
            folder_name = os.path.basename(os.path.normpath(self.folder_path))
            relpath = (
                os.path.relpath(self.file_path, self.folder_path)
                if self.file_path and self.folder_path
                else ""
            )
            return f"C:\\{dest}\\{folder_name}\\{relpath}".replace("/", "\\")
        file_name = os.path.basename(self.file_path) if self.file_path else ""
        return f"C:\\{dest}\\{file_name}"

    def _append_extra_args(self, argv: List[str]) -> None:
        extra = (self.psexec_params.get("extra_args") or "").strip()
        if extra:
            argv.extend(_split_extra_args(extra))

    def _spec_from_psexec_argv(
        self,
        remote_parts: Sequence[str],
        *,
        append_extra: bool = True,
    ) -> CommandSpec:
        # Argv com senha mascarada; materialização ocorre na execução.
        real = self._base_psexec_argv()
        real.extend(remote_parts)
        # MSI file-mode já embute extra_args no msiexec (compat legado)
        if append_extra:
            self._append_extra_args(real)
        return CommandSpec.from_argv(
            real,
            has_secrets=self._has_password,
            metadata={"kind": "psexec"},
            prefer_external_console=True,
        )

    def _parse_manual_remote_cmd(self, remote_cmd: str) -> List[str]:
        """Converte comando manual em executable + args (regras Windows)."""
        return split_windows_command_line(remote_cmd)

    # ── PowerShell / CMD helpers ────────────────────────────────────────

    def _powershell_remote_parts(self, exec_path: str) -> List[str]:
        p = self.powershell_params or {}
        parts: List[str] = ["powershell"]
        if p.get("NoProfile"):
            parts.append("-NoProfile")
        if p.get("NoExit"):
            parts.append("-NoExit")
        if p.get("ExecutionPolicy"):
            parts.extend(["-ExecutionPolicy", str(p["ExecutionPolicy"])])
        if p.get("WindowStyle"):
            parts.extend(["-WindowStyle", str(p["WindowStyle"])])
        if p.get("EncodedCommand"):
            parts.extend(["-EncodedCommand", str(p["EncodedCommand"])])
        elif p.get("Command"):
            parts.extend(["-Command", str(p["Command"])])
        elif self.file_path or exec_path:
            parts.extend(["-File", exec_path])
        return parts

    def _cmd_remote_parts(self, exec_path: str) -> List[str]:
        c = self.cmd_params or {}
        parts: List[str] = ["cmd"]
        if c.get("/C"):
            parts.append("/C")
        if c.get("/K"):
            parts.append("/K")
        if c.get("/Q"):
            parts.append("/Q")
        if c.get("/D"):
            parts.append("/D")
        if c.get("/S"):
            parts.append("/S")
        cmd_str = c.get("Command") or exec_path
        if cmd_str:
            # Sem flags /C|/K, default histórico era incluir o path entre aspas
            if not any(c.get(f) for f in ("/C", "/K", "/Q", "/D", "/S")) and not c.get(
                "Command"
            ):
                # Mantém compat: cmd /c "path" era o padrão em folder .bat;
                # em _build_psexec_bat_script o padrão era `cmd {flags} "{cmd_str}"`
                pass
            parts.append(str(cmd_str))
        return parts

    def _resolve_exec_path(self) -> str:
        robocopy_dest = self._remote_path_after_robocopy()
        if robocopy_dest:
            return robocopy_dest
        if self.psexec_params.get("-c") or self.psexec_params.get("-f"):
            return os.path.normpath(self.file_path) if self.file_path else ""
        return os.path.basename(self.file_path) if self.file_path else ""

    # ── builders por tipo ───────────────────────────────────────────────

    def build_psexec(self) -> str:
        spec = self.build_psexec_spec()
        return spec.display_command if spec else "# PsExec.exe \\\\<host> [opções] <comando>"

    def build_psexec_spec(self) -> CommandSpec:
        if not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# PsExec.exe \\\\<host> [opções] <comando>",
            )
        remote_cmd = (self.psexec_params.get("remote_cmd") or "").strip()
        if (not self.file_path and not self.folder_path) and remote_cmd:
            # Comando manual: parse Windows (CommandLineToArgvW), não um único token
            remote_parts = self._parse_manual_remote_cmd(remote_cmd)
            if not remote_parts:
                return CommandSpec(
                    executable=self._resolve_psexec_path(),
                    display_command="# Erro: comando remoto vazio",
                )
            return self._spec_from_psexec_argv(remote_parts)

        if not self.file_path:
            return CommandSpec(
                executable=self._resolve_psexec_path(),
                display_command="# PsExec.exe \\\\<host> [opções] <comando>",
            )

        if self.selection_mode == "folder" and self.folder_path:
            return self._build_psexec_folder_spec()

        ext = os.path.splitext(self.file_path)[1].lower() if self.file_path else ""
        if ext == ".exe":
            return self._build_psexec_exe_spec()
        if ext == ".msi":
            return self._build_psexec_msi_spec()
        if ext == ".ps1":
            return self._build_psexec_ps_script_spec()
        if ext == ".bat":
            return self._build_psexec_bat_script_spec()
        return self._build_psexec_other_spec()

    def _build_psexec_exe_spec(self) -> CommandSpec:
        if not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: parâmetros de psexec ausentes",
            )
        if not self.file_path:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: arquivo não selecionado",
            )
        if self.psexec_params.get("-c") or self.psexec_params.get("-f"):
            target = os.path.normpath(self.file_path)
        else:
            target = os.path.basename(self.file_path)
        return self._spec_from_psexec_argv([target])

    def _build_psexec_msi_spec(self) -> CommandSpec:
        if not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: parâmetros de psexec ausentes",
            )
        if not self.file_path:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: arquivo .msi não selecionado",
            )
        msiexec_argv = self._build_msiexec_argv()
        if not msiexec_argv:
            # Fallback display de erro já está em build_msiexec
            display = self.build_msiexec()
            return CommandSpec(executable="msiexec", display_command=display)
        # extras já vão dentro do msiexec (comportamento legado)
        return self._spec_from_psexec_argv(msiexec_argv, append_extra=False)

    def _build_psexec_ps_script_spec(self) -> CommandSpec:
        if not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: parâmetros de psexec ausentes",
            )
        exec_path = self._resolve_exec_path()
        return self._spec_from_psexec_argv(self._powershell_remote_parts(exec_path))

    def _build_psexec_bat_script_spec(self) -> CommandSpec:
        if not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: parâmetros de psexec ausentes",
            )
        exec_path = self._resolve_exec_path()
        return self._spec_from_psexec_argv(self._cmd_remote_parts(exec_path))

    def _build_psexec_other_spec(self) -> CommandSpec:
        if not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: parâmetros de psexec ausentes",
            )
        if not self.file_path:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: arquivo não selecionado",
            )
        if self.psexec_params.get("-c") or self.psexec_params.get("-f"):
            target = os.path.normpath(self.file_path)
        else:
            target = os.path.basename(self.file_path)
        return self._spec_from_psexec_argv([target])

    def _build_psexec_folder_spec(self) -> CommandSpec:
        if not self.robocopy_params or not self.psexec_params:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: parâmetros de robocopy ou psexec ausentes",
            )
        if not self.file_path or not self.folder_path:
            return CommandSpec(
                executable="PsExec.exe",
                display_command="# Erro: arquivo ou pasta não selecionados",
            )
        exec_path = self._remote_path_after_robocopy() or ""
        ext = os.path.splitext(self.file_path)[1].lower() if self.file_path else ""
        if ext == ".ps1":
            remote = self._powershell_remote_parts(exec_path)
        elif ext == ".bat":
            # Mantém comportamento histórico: cmd /c "path"
            remote = ["cmd", "/c", exec_path]
        elif ext == ".msi":
            # CORREÇÃO: usar opções MSI da UI (antes era só msiexec /i)
            remote = self._build_msiexec_argv(remote_msi_path=exec_path)
            if not remote:
                remote = ["msiexec", "/i", exec_path]
        else:
            remote = [exec_path]
        return self._spec_from_psexec_argv(remote)

    # ── msiexec ─────────────────────────────────────────────────────────

    def _build_msiexec_argv(self, remote_msi_path: Optional[str] = None) -> List[str]:
        if not self.msi_params.get("enable"):
            # Em folder mode forçamos enable implícito se path remoto fornecido
            if remote_msi_path is None:
                return []
        if not self.file_path and not remote_msi_path:
            return []

        if remote_msi_path:
            msi_path = remote_msi_path
        else:
            dest_raw = self.robocopy_params.get("dest") if self.robocopy_params else None
            if not dest_raw or not str(dest_raw).strip():
                return []
            dest = _normalize_robocopy_dest(str(dest_raw))
            if self.selection_mode == "folder" and self.folder_path:
                folder_name = os.path.basename(os.path.normpath(self.folder_path))
                relpath = (
                    os.path.relpath(self.file_path, self.folder_path)
                    if self.file_path and self.folder_path
                    else os.path.basename(self.file_path or "")
                )
                msi_path = f"C:\\{dest}\\{folder_name}\\{relpath}".replace("/", "\\")
            else:
                file_name = os.path.basename(self.file_path) if self.file_path else ""
                msi_path = f"C:\\{dest}\\{file_name}"

        cmd: List[str] = ["msiexec"]
        # Em folder mode sem msi_params.enable, default /i (compat)
        action_value = _extract_flag_value(self.msi_params.get("action", ""))
        if action_value and action_value != "Nenhum":
            cmd.append(action_value)
        elif remote_msi_path and not self.msi_params.get("enable"):
            cmd.append("/i")
        elif self.msi_params.get("enable"):
            # enable sem action explícita — preserva comportamento: ainda precisa de ação
            pass

        cmd.append(msi_path)

        interface_value = _extract_flag_value(self.msi_params.get("interface", ""))
        if interface_value and interface_value != "Nenhum":
            cmd.append(interface_value)

        restart_value = _extract_flag_value(self.msi_params.get("restart", ""))
        if restart_value and restart_value != "Nenhum":
            cmd.append(restart_value)

        if self.msi_params.get("log") and self.msi_params.get("log_file"):
            log_file = str(self.msi_params["log_file"]).strip()
            if log_file:
                cmd.extend(["/l*vx", log_file])

        if self.msi_params.get("repair") and str(self.msi_params["repair"]).strip():
            cmd.append(f"-f{self.msi_params['repair']}")

        if self.msi_params.get("update") and str(self.msi_params["update"]).strip():
            cmd.extend(_split_extra_args(str(self.msi_params["update"])))

        # Nota: extra_args do PsExec eram historicamente anexados ao msiexec;
        # em _spec_from_psexec_argv já anexamos ao final do PsExec. Para MSI
        # file-mode o legado anexava dentro do msiexec — preservamos isso.
        if remote_msi_path is None:
            extra = (self.psexec_params.get("extra_args") or "").strip()
            if extra:
                cmd.extend(_split_extra_args(extra))

        return cmd

    def build_msiexec(self) -> str:
        if not self.msi_params.get("enable"):
            return ""
        if not self.file_path:
            return "# msiexec [opções] <arquivo.msi>"
        dest_raw = self.robocopy_params.get("dest") if self.robocopy_params else None
        if not dest_raw or not str(dest_raw).strip():
            return "# Erro: destino de cópia não especificado"
        argv = self._build_msiexec_argv()
        if not argv:
            return "# Erro: destino de cópia não especificado"
        # Display sem secrets (msiexec em si não tem senha)
        return format_argv_for_display(argv)

    # ── full command / execution plan ───────────────────────────────────

    def build_full_command(self) -> str:
        """String multi-linha sanitizada (robocopy\\npsexec) para preview."""
        plan = self.build_execution_plan()
        displays = [s.sanitized_display(self._passwords_for_redaction()) for s in plan]
        return "\n".join(displays)

    def build_execution_plan(self) -> List[CommandSpec]:
        """
        Plano de execução estruturado.

        Ordem: [robocopy?] + [psexec].
        """
        specs: List[CommandSpec] = []
        rc = self.build_robocopy_spec()
        if rc and rc.display_command and not rc.display_command.startswith("#"):
            specs.append(rc)
        elif rc and rc.argv and rc.executable == "robocopy" and rc.args:
            # spec válido
            if not (rc.display_command or "").startswith("#"):
                specs.append(rc)

        # Escolha do psexec (mesma lógica de build_full_command legado)
        if self.file_path and self.file_path.lower().endswith(".ps1"):
            psexec = self._build_psexec_ps_script_spec()
        elif self.file_path and self.file_path.lower().endswith(".bat"):
            psexec = self._build_psexec_bat_script_spec()
        else:
            psexec = self.build_psexec_spec()
        specs.append(psexec)
        return specs

    def build_display_command(self) -> str:
        """Alias explícito: sempre sanitizado."""
        text = self.build_full_command()
        return redact_command_text(text, passwords=self._passwords_for_redaction())
