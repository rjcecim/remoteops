"""Regras do Windows PowerShell (powershell.exe) usadas pela aba PowerShell.

Camadas:
    Interface  →  compute_powershell_option_state()
    Validação  →  validate_powershell_options() / validate_powershell_params()
    Builder    →  build_powershell_remote_argv()

Não há escolha silenciosa entre -Command, -EncodedCommand e -File.
Aliases de pwsh.exe e parâmetros sem uso no RemoteOps não são emitidos.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from typing import Optional

MODE_COMMAND = "command"
MODE_ENCODED = "encoded"
MODE_FILE = "file"
MODE_SESSION = "session"
MODES = (MODE_COMMAND, MODE_ENCODED, MODE_FILE, MODE_SESSION)

ENC_SRC_TEXT = "text"
ENC_SRC_B64 = "base64"
ENC_SOURCES = (ENC_SRC_TEXT, ENC_SRC_B64)

EXEC_POLICIES = (
    "",
    "Bypass",
    "Unrestricted",
    "RemoteSigned",
    "AllSigned",
    "Restricted",
    "Undefined",
)

TOOLTIPS = {
    "mode": (
        "Modo do powershell.exe: um entre -Command, -EncodedCommand, "
        "-File ou sessão persistente."
    ),
    "mode_command": "Executa o código e encerra (powershell.exe -Command).",
    "mode_encoded": (
        "Envia -EncodedCommand (UTF-16LE + Base64). "
        "Mais robusto para aspas, quebras de linha e caracteres especiais."
    ),
    "mode_file": "Executa o script .ps1 com -File e argumentos do script.",
    "mode_session": (
        "Abre uma sessão PowerShell persistente (ConPTY). "
        "cwd, variáveis e funções valem para o próximo comando."
    ),
    "no_logo": "Oculta o banner Windows PowerShell / Copyright no início da sessão.",
    "no_profile": (
        "Não carrega perfis (CurrentUser/AllUsers). "
        "Recomendado em administração remota: execução previsível, sem aliases do host."
    ),
    "non_interactive": (
        "Não solicita interação (Read-Host, prompts). "
        "Incompatível com sessão persistente."
    ),
    "execution_policy": (
        "Política desta invocação apenas — não grava no Registro. "
        "Padrão do sistema = não emitir -ExecutionPolicy."
    ),
    "working_directory": (
        "Equivalente a -WorkingDirectory no host remoto. "
        "Não é validado como caminho local."
    ),
    "command": "Código PowerShell após -Command. Pipelines, aspas e quebras de linha são preservados.",
    "encoded_command": "Base64 UTF-16LE já pronto para -EncodedCommand. Não recodifique.",
    "encode_source": "Texto a codificar (RemoteOps gera Base64) ou Base64 já pronto.",
    "file_args": "Argumentos do script após -File, não parâmetros do powershell.exe.",
}


class PowerShellOptionsError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class PowerShellOptions:
    mode: str = MODE_COMMAND
    no_logo: bool = True
    no_profile: bool = True
    non_interactive: bool = False
    execution_policy: str = ""
    working_directory: str = ""
    command: str = ""
    encoded_command: str = ""
    encode_from_text: bool = True
    file_args: str = ""


@dataclass(frozen=True)
class PsWidgetState:
    enabled: bool = True
    tooltip: str = ""
    reason: str = ""
    visible: bool = True


@dataclass(frozen=True)
class PsUiState:
    options: PowerShellOptions
    widgets: dict[str, PsWidgetState]


def encode_powershell_command(text: str) -> str:
    """UTF-16LE + Base64 — o formato que powershell.exe -EncodedCommand espera."""
    return base64.b64encode((text or "").encode("utf-16le")).decode("ascii")


def decode_encoded_command(blob: str) -> tuple[Optional[str], Optional[str]]:
    """Devolve (texto, None) ou (None, erro). Não levanta."""
    raw = (blob or "").strip()
    if not raw:
        return None, "EncodedCommand vazio."
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "EncodedCommand não é Base64 válido."
    if len(data) % 2 != 0:
        return None, "EncodedCommand não é UTF-16LE válido (número ímpar de bytes)."
    try:
        return data.decode("utf-16le"), None
    except Exception:
        return None, "Falha ao decodificar EncodedCommand como UTF-16LE."


def _norm_mode(raw) -> str:
    text = str(raw or "").strip().lower().replace("-", "")
    if text in ("encoded", "encodedcommand"):
        return MODE_ENCODED
    if text in ("file",):
        return MODE_FILE
    if text in ("session", "interactive", "terminal"):
        return MODE_SESSION
    return MODE_COMMAND


def _norm_exec_policy(raw) -> str:
    text = str(raw or "").strip()
    if not text or text.lower() in ("nenhum", "none", "padrão", "padrao", "default"):
        return ""
    for known in EXEC_POLICIES:
        if known and known.lower() == text.lower():
            return known
    return text


def options_from_params(params: Optional[dict]) -> PowerShellOptions:
    """Aceita o dict da UI (novo) e o legado Command/EncodedCommand/NoExit."""
    p = dict(params or {})
    if "mode" in p:
        mode = _norm_mode(p.get("mode"))
    else:
        has_c = bool(str(p.get("Command") or "").strip())
        has_e = bool(str(p.get("EncodedCommand") or "").strip())
        if has_c and has_e:
            mode = MODE_COMMAND
        elif has_e:
            mode = MODE_ENCODED
        elif p.get("NoExit") and not has_e:
            mode = MODE_SESSION
        else:
            mode = MODE_COMMAND
    enc_src = str(p.get("encode_source") or "").strip().lower()
    encode_from_text = enc_src != ENC_SRC_B64
    if "encode_from_text" in p:
        encode_from_text = bool(p.get("encode_from_text"))
    return PowerShellOptions(
        mode=mode,
        no_logo=bool(p["NoLogo"]) if "NoLogo" in p else bool(p.get("no_logo", True)),
        no_profile=bool(p["NoProfile"]) if "NoProfile" in p else bool(p.get("no_profile", True)),
        non_interactive=bool(p.get("NonInteractive") or p.get("non_interactive")),
        execution_policy=_norm_exec_policy(p.get("ExecutionPolicy") or p.get("execution_policy")),
        working_directory=str(p.get("WorkingDirectory") or p.get("working_directory") or ""),
        command=str(p.get("Command") or p.get("command") or ""),
        encoded_command=str(p.get("EncodedCommand") or p.get("encoded_command") or ""),
        encode_from_text=encode_from_text,
        file_args=str(p.get("FileArgs") or p.get("file_args") or ""),
    )


def options_to_params(opts: PowerShellOptions) -> dict:
    return {
        "mode": opts.mode,
        "NoLogo": opts.no_logo,
        "NoProfile": opts.no_profile,
        "NonInteractive": opts.non_interactive,
        "NoExit": False,
        "ExecutionPolicy": opts.execution_policy,
        "WorkingDirectory": opts.working_directory,
        "Command": opts.command,
        "EncodedCommand": opts.encoded_command,
        "encode_from_text": opts.encode_from_text,
        "encode_source": ENC_SRC_TEXT if opts.encode_from_text else ENC_SRC_B64,
        "FileArgs": opts.file_args,
    }


def validate_powershell_options(opts: PowerShellOptions) -> list[str]:
    errors: list[str] = []
    mode = str(opts.mode or "").strip().lower()
    if mode not in MODES:
        errors.append(f"Modo PowerShell inválido: {opts.mode!r}.")
    if opts.execution_policy and opts.execution_policy not in EXEC_POLICIES:
        errors.append(
            f"ExecutionPolicy inválida: {opts.execution_policy!r}."
        )
    if mode == MODE_SESSION and opts.non_interactive:
        errors.append("-NonInteractive não pode ser usado em sessão interativa.")
    if mode == MODE_COMMAND and not str(opts.command or "").strip():
        errors.append("Modo Command exige o código PowerShell.")
    if mode == MODE_ENCODED:
        if opts.encode_from_text:
            if not str(opts.command or "").strip():
                errors.append("Informe o texto a codificar para -EncodedCommand.")
        else:
            _text, err = decode_encoded_command(opts.encoded_command)
            if err:
                errors.append(err)
    return errors


def validate_powershell_params(
    params: Optional[dict],
    *,
    file_path: str = "",
) -> list[str]:
    p = dict(params or {})
    errors: list[str] = []
    has_c = bool(str(p.get("Command") or "").strip())
    has_e = bool(str(p.get("EncodedCommand") or "").strip())
    if "mode" not in p and has_c and has_e:
        errors.append("-Command e -EncodedCommand são mutuamente exclusivos.")
    if "mode" not in p and has_c and str(file_path or "").strip():
        errors.append("-Command e -File são mutuamente exclusivos.")
    if "mode" not in p and has_e and str(file_path or "").strip():
        errors.append("-EncodedCommand e -File são mutuamente exclusivos.")
    if p.get("Sta") and p.get("Mta"):
        errors.append("-Sta e -Mta são mutuamente exclusivos.")
    if "mode" in p:
        raw_mode = str(p.get("mode") or "").strip().lower().replace("-", "")
        aliases = {
            "command",
            "encoded",
            "encodedcommand",
            "file",
            "session",
            "interactive",
            "terminal",
        }
        if raw_mode not in aliases:
            errors.append(f"Modo PowerShell inválido: {p.get('mode')!r}.")
    opts = options_from_params(p)
    errors.extend(validate_powershell_options(opts))
    if opts.mode == MODE_FILE and not str(file_path or "").strip():
        errors.append("Modo Script (-File) exige um arquivo .ps1.")
    seen: set[str] = set()
    unique: list[str] = []
    for err in errors:
        if err not in seen:
            seen.add(err)
            unique.append(err)
    return unique


def compute_powershell_option_state(opts: PowerShellOptions) -> PsUiState:
    mode = _norm_mode(opts.mode)
    resolved = replace(
        opts,
        mode=mode,
        execution_policy=_norm_exec_policy(opts.execution_policy),
        non_interactive=False if mode == MODE_SESSION else bool(opts.non_interactive),
    )
    widgets: dict[str, PsWidgetState] = {
        "mode": PsWidgetState(True, TOOLTIPS["mode"]),
        "no_logo": PsWidgetState(True, TOOLTIPS["no_logo"]),
        "no_profile": PsWidgetState(True, TOOLTIPS["no_profile"]),
        "execution_policy": PsWidgetState(True, TOOLTIPS["execution_policy"]),
        "working_directory": PsWidgetState(True, TOOLTIPS["working_directory"]),
        "encode_source": PsWidgetState(
            mode == MODE_ENCODED,
            TOOLTIPS["encode_source"]
            if mode == MODE_ENCODED
            else "-EncodedCommand não está disponível neste modo.",
            visible=(mode == MODE_ENCODED),
        ),
        "file_args": PsWidgetState(
            mode == MODE_FILE,
            TOOLTIPS["file_args"]
            if mode == MODE_FILE
            else "Argumentos do script só existem no modo -File.",
            visible=(mode == MODE_FILE),
        ),
    }
    if mode == MODE_SESSION:
        widgets["non_interactive"] = PsWidgetState(
            False,
            "-NonInteractive não pode ser usado em um terminal interativo.",
            "-NonInteractive não pode ser usado em um terminal interativo.",
        )
    else:
        widgets["non_interactive"] = PsWidgetState(True, TOOLTIPS["non_interactive"])

    if mode == MODE_ENCODED and not resolved.encode_from_text:
        widgets["command"] = PsWidgetState(
            False,
            "O modo Base64 pronto usa o campo EncodedCommand, não o texto.",
            visible=False,
        )
        widgets["encoded_command"] = PsWidgetState(True, TOOLTIPS["encoded_command"])
    elif mode == MODE_ENCODED:
        widgets["command"] = PsWidgetState(True, TOOLTIPS["encode_source"])
        widgets["encoded_command"] = PsWidgetState(
            False,
            "O RemoteOps gera o Base64 a partir do texto.",
            visible=False,
        )
    elif mode == MODE_FILE:
        reason = "-Command não está disponível porque o modo -File está selecionado."
        widgets["command"] = PsWidgetState(False, reason, reason, visible=False)
        widgets["encoded_command"] = PsWidgetState(
            False,
            "-EncodedCommand não está disponível no modo -File.",
            visible=False,
        )
    elif mode == MODE_SESSION:
        widgets["command"] = PsWidgetState(
            True,
            "Comando inicial opcional. Sem texto, abre só a sessão. Com texto, usa -NoExit -Command.",
        )
        widgets["encoded_command"] = PsWidgetState(
            False,
            "-EncodedCommand não está disponível no modo sessão.",
            visible=False,
        )
    else:
        widgets["command"] = PsWidgetState(True, TOOLTIPS["command"])
        widgets["encoded_command"] = PsWidgetState(
            False,
            "-EncodedCommand não está disponível porque o modo -Command está selecionado.",
            visible=False,
        )
    return PsUiState(options=resolved, widgets=widgets)


def _split_file_args(extra: str) -> list[str]:
    text = (extra or "").strip()
    if not text:
        return []
    from remoteops.core.win_cmdline import split_windows_command_line

    return split_windows_command_line(text)


def build_powershell_remote_argv(
    opts: PowerShellOptions,
    *,
    file_path: str = "",
) -> list[str]:
    """
    ``powershell [opções] [-Command|-EncodedCommand|-File] …``.

    A cadeia -Command / o Base64 / o caminho -File são um argumento cada.
    Nunca emite -Command junto com -EncodedCommand ou -File.
    """
    errors = validate_powershell_params(
        options_to_params(opts),
        file_path=file_path,
    )
    if errors:
        raise PowerShellOptionsError(errors)

    parts: list[str] = ["powershell"]
    if opts.no_logo:
        parts.append("-NoLogo")
    if opts.no_profile:
        parts.append("-NoProfile")
    if opts.mode != MODE_SESSION and opts.non_interactive:
        parts.append("-NonInteractive")
    if opts.execution_policy:
        parts.extend(["-ExecutionPolicy", opts.execution_policy])
    wd = str(opts.working_directory or "").strip()
    if wd:
        parts.extend(["-WorkingDirectory", wd])

    if opts.mode == MODE_COMMAND:
        parts.extend(["-Command", opts.command])
    elif opts.mode == MODE_ENCODED:
        if opts.encode_from_text:
            blob = encode_powershell_command(opts.command)
        else:
            blob = str(opts.encoded_command or "").strip()
        parts.extend(["-EncodedCommand", blob])
    elif opts.mode == MODE_FILE:
        parts.extend(["-File", file_path])
        parts.extend(_split_file_args(opts.file_args))
    elif opts.mode == MODE_SESSION:
        initial = str(opts.command or "").strip()
        if initial:
            parts.append("-NoExit")
            parts.extend(["-Command", opts.command])
    return parts
