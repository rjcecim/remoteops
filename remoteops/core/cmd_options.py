"""Regras reais do cmd.exe usadas pela aba CMD.

Camadas:
    Interface  →  compute_cmd_option_state()
    Validação  →  validate_cmd_options()
    Builder    →  build_cmd_remote_argv()  (sempre sanitiza)

Sintaxe de referência (``cmd.exe /?``)::

    CMD [/A | /U] [/Q] [/D] [/E:ON | /E:OFF]
        [/F:ON | /F:OFF] [/V:ON | /V:OFF]
        [[/S] [/C | /K] cadeia]

Aliases ``/X`` ``/Y`` ``/R`` e ``/T:fg`` não são emitidos.
``/S`` não é exposto na UI: o builder aplica quando há cadeia após ``/C``/``/K``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

MODE_C = "c"
MODE_K = "k"
MODES = (MODE_C, MODE_K)

TRI_SYSTEM = ""
TRI_ON = "on"
TRI_OFF = "off"
TRI_STATES = (TRI_SYSTEM, TRI_ON, TRI_OFF)

ENC_SYSTEM = ""
ENC_ANSI = "a"
ENC_UNICODE = "u"
ENC_STATES = (ENC_SYSTEM, ENC_ANSI, ENC_UNICODE)

TOOLTIPS = {
    "mode": (
        "Modo do cmd.exe: /C executa a cadeia e encerra; "
        "/K executa (se houver cadeia) e permanece aberto."
    ),
    "mode_c": "Executa o comando especificado e encerra o cmd.exe.",
    "mode_k": (
        "Executa o comando (se houver) e mantém a sessão CMD aberta. "
        "Comandos seguintes compartilham o mesmo processo (cwd, variáveis)."
    ),
    "/D": (
        "Desativa AutoRun do Registro "
        "(HKLM/HKCU\\Software\\Microsoft\\Command Processor\\AutoRun). "
        "Recomendado em ferramenta administrativa."
    ),
    "/Q": "Desativa o echo (equivalente a ECHO OFF). No terminal, o prompt continua visível.",
    "/S": (
        "Altera o tratamento de aspas da cadeia após /C ou /K. "
        "O builder aplica automaticamente quando há comando."
    ),
    "extensions": (
        "Extensões de comando (/E:ON ou /E:OFF). "
        "Padrão do sistema costuma ser ON no Windows atual."
    ),
    "delayed_expansion": (
        "Expansão atrasada de variáveis com !var! (/V:ON ou /V:OFF). "
        "Necessário para ler variáveis alteradas no mesmo bloco."
    ),
    "completion": (
        "Conclusão de nomes de arquivo/pasta com Tab (/F:ON ou /F:OFF). "
        "Só faz sentido em sessão interativa (/K)."
    ),
    "encoding": (
        "Formato de saída de comandos internos redirecionados: "
        "/A = ANSI, /U = Unicode. Não substitui o encoding do ConPTY."
    ),
    "command": (
        "Cadeia do CMD após /C ou /K. Operadores (& && || | > >> < ^) "
        "e aspas são preservados. Várias linhas são unidas com &."
    ),
}


class CmdOptionsError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class CmdOptions:
    """Estado tipado da aba CMD (nunca /C e /K juntos)."""

    mode: str = MODE_C
    disable_autorun: bool = True
    quiet: bool = False
    extensions: str = TRI_SYSTEM
    delayed_expansion: str = TRI_SYSTEM
    completion: str = TRI_SYSTEM
    encoding: str = ENC_SYSTEM
    command: str = ""


@dataclass(frozen=True)
class CmdWidgetState:
    enabled: bool = True
    tooltip: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CmdUiState:
    options: CmdOptions
    widgets: dict[str, CmdWidgetState]


def normalize_cmd_chain(text: str) -> str:
    """
    Normaliza a cadeia CMD.

    Uma linha: preservada (operadores intactos).
    Várias linhas: unidas com `` & `` (execução sequencial no mesmo cmd.exe).
    Não substitui quebras por espaço e não remove ``& | < > ^ % !``.
    """
    if text is None:
        return ""
    lines = [ln.rstrip() for ln in str(text).splitlines()]
    lines = [ln.strip() for ln in lines if ln.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    return " & ".join(lines)


def _norm_mode(raw) -> str:
    text = str(raw or "").strip().lower()
    if text in ("k", "/k"):
        return MODE_K
    return MODE_C


def _norm_tri(raw) -> str:
    text = str(raw or "").strip().lower()
    if text in ("on", "/e:on", "/v:on", "/f:on", "1", "true"):
        return TRI_ON
    if text in ("off", "/e:off", "/v:off", "/f:off", "0", "false"):
        return TRI_OFF
    return TRI_SYSTEM


def _norm_encoding(raw) -> str:
    text = str(raw or "").strip().lower()
    if text in ("a", "/a", "ansi"):
        return ENC_ANSI
    if text in ("u", "/u", "unicode", "utf-16"):
        return ENC_UNICODE
    return ENC_SYSTEM


def options_from_params(params: Optional[dict]) -> CmdOptions:
    """Aceita o dict da UI (novo) e o legado com checkboxes /C /K.

    Se /C e /K vierem ambos True sem ``mode``, assume /C para montagem
    (nunca emite os dois). ``validate_cmd_params`` rejeita esse estado.
    """
    p = dict(params or {})
    if "mode" in p:
        mode = _norm_mode(p.get("mode"))
    elif p.get("/K") and not p.get("/C"):
        mode = MODE_K
    else:
        mode = MODE_C
    return CmdOptions(
        mode=mode,
        disable_autorun=bool(p["/D"]) if "/D" in p else bool(p.get("disable_autorun", True)),
        quiet=bool(p.get("/Q") or p.get("quiet")),
        extensions=_norm_tri(p.get("extensions")),
        delayed_expansion=_norm_tri(p.get("delayed_expansion")),
        completion=_norm_tri(p.get("completion")),
        encoding=_norm_encoding(p.get("encoding")),
        command=str(p.get("Command") or p.get("command") or ""),
    )


def options_to_params(opts: CmdOptions) -> dict:
    """Dict consumido pelo CommandBuilder (chaves novas + legado /C /K)."""
    opts = sanitize_cmd_options(opts)
    return {
        "mode": opts.mode,
        "/C": opts.mode == MODE_C,
        "/K": opts.mode == MODE_K,
        "/D": opts.disable_autorun,
        "/Q": opts.quiet,
        "/S": bool(normalize_cmd_chain(opts.command)),
        "extensions": opts.extensions,
        "delayed_expansion": opts.delayed_expansion,
        "completion": opts.completion,
        "encoding": opts.encoding,
        "Command": opts.command,
    }


def sanitize_cmd_options(opts: CmdOptions) -> CmdOptions:
    """Resolve conflitos: um modo, um lado de cada par, /F só em /K."""
    mode = _norm_mode(opts.mode)
    completion = _norm_tri(opts.completion)
    if mode != MODE_K:
        completion = TRI_SYSTEM
    return replace(
        opts,
        mode=mode,
        extensions=_norm_tri(opts.extensions),
        delayed_expansion=_norm_tri(opts.delayed_expansion),
        completion=completion,
        encoding=_norm_encoding(opts.encoding),
        command=opts.command if opts.command is not None else "",
    )


def validate_cmd_options(opts: CmdOptions) -> list[str]:
    """Rejeita estados impossíveis (UI, dict legado, chamada programática)."""
    errors: list[str] = []
    mode = str(opts.mode or "").strip().lower()
    if mode not in MODES:
        errors.append(f"Modo CMD inválido: {opts.mode!r} (use /C ou /K).")
    if opts.extensions not in TRI_STATES:
        errors.append("Extensões CMD devem ser padrão, /E:ON ou /E:OFF.")
    if opts.delayed_expansion not in TRI_STATES:
        errors.append("Expansão atrasada deve ser padrão, /V:ON ou /V:OFF.")
    if opts.completion not in TRI_STATES:
        errors.append("Conclusão deve ser padrão, /F:ON ou /F:OFF.")
    if opts.encoding not in ENC_STATES:
        errors.append("Encoding deve ser padrão, /A ou /U.")
    if mode == MODE_C and _norm_tri(opts.completion) != TRI_SYSTEM:
        errors.append("/F só se aplica a sessão interativa (/K).")
    return errors


def validate_cmd_params(params: Optional[dict]) -> list[str]:
    """Validação da camada de dict (UI, config, chamada programática)."""
    p = dict(params or {})
    errors: list[str] = []
    if "mode" not in p and p.get("/C") and p.get("/K"):
        errors.append("/C e /K são mutuamente exclusivos.")
    if "mode" in p:
        raw_mode = str(p.get("mode") or "").strip().lower()
        if raw_mode not in ("c", "/c", "k", "/k"):
            errors.append(f"Modo CMD inválido: {p.get('mode')!r} (use /C ou /K).")
    enc = str(p.get("encoding") or "").strip().lower()
    if enc and enc not in ("a", "/a", "ansi", "u", "/u", "unicode", "utf-16"):
        errors.append("Encoding deve ser padrão, /A ou /U.")
    # Valida o estado bruto (antes de sanitizar /F em /C, etc.).
    errors.extend(validate_cmd_options(options_from_params(p)))
    seen: set[str] = set()
    unique: list[str] = []
    for err in errors:
        if err not in seen:
            seen.add(err)
            unique.append(err)
    return unique


def compute_cmd_option_state(opts: CmdOptions) -> CmdUiState:
    """Habilita/desabilita e tooltips a partir do estado sanitizado."""
    resolved = sanitize_cmd_options(opts)
    widgets: dict[str, CmdWidgetState] = {
        "mode": CmdWidgetState(True, TOOLTIPS["mode"]),
        "/D": CmdWidgetState(True, TOOLTIPS["/D"]),
        "/Q": CmdWidgetState(True, TOOLTIPS["/Q"]),
        "extensions": CmdWidgetState(True, TOOLTIPS["extensions"]),
        "delayed_expansion": CmdWidgetState(True, TOOLTIPS["delayed_expansion"]),
        "encoding": CmdWidgetState(True, TOOLTIPS["encoding"]),
        "command": CmdWidgetState(True, TOOLTIPS["command"]),
    }
    if resolved.mode == MODE_K:
        widgets["completion"] = CmdWidgetState(True, TOOLTIPS["completion"])
    else:
        reason = "A conclusão (/F) só está disponível em sessão interativa (/K)."
        widgets["completion"] = CmdWidgetState(False, reason, reason)
    return CmdUiState(options=resolved, widgets=widgets)


def build_cmd_remote_argv(
    opts: CmdOptions,
    *,
    fallback_command: str = "",
) -> list[str]:
    """
    ``cmd [opções] [/S] [/C|/K] [cadeia]``.

    A cadeia é **um** argumento (não é fatiada em tokens). Assim ``&&``,
    pipes e aspas chegam intactos ao argv do PsExec (CommandLineToArgvW).

    Não use este argv em CreateProcess **do cmd.exe**: o cmd relê a
    command line com ``/S`` e trata ``\\"`` do CRT como lixo. Para lançar
    cmd.exe direto, use ``build_cmd_command_line``.

    Ordem oficial: [/A|/U] [/Q] [/D] [/E] [/F] [/V] [/S] [/C|/K] cadeia.
    Sempre sanitiza — nunca emite /C e /K juntos nem pares ON+OFF.
    """
    opts = sanitize_cmd_options(opts)
    errors = validate_cmd_options(opts)
    if errors:
        raise CmdOptionsError(errors)

    chain = normalize_cmd_chain(opts.command) or normalize_cmd_chain(fallback_command)
    parts: list[str] = ["cmd"]

    if opts.encoding == ENC_ANSI:
        parts.append("/A")
    elif opts.encoding == ENC_UNICODE:
        parts.append("/U")
    if opts.quiet:
        parts.append("/Q")
    if opts.disable_autorun:
        parts.append("/D")
    if opts.extensions == TRI_ON:
        parts.append("/E:ON")
    elif opts.extensions == TRI_OFF:
        parts.append("/E:OFF")
    if opts.mode == MODE_K:
        if opts.completion == TRI_ON:
            parts.append("/F:ON")
        elif opts.completion == TRI_OFF:
            parts.append("/F:OFF")
    if opts.delayed_expansion == TRI_ON:
        parts.append("/V:ON")
    elif opts.delayed_expansion == TRI_OFF:
        parts.append("/V:OFF")

    mode_flag = "/K" if opts.mode == MODE_K else "/C"
    if chain:
        parts.append("/S")
        parts.append(mode_flag)
        parts.append(chain)
    else:
        parts.append(mode_flag)
    return parts


def build_cmd_command_line(opts: CmdOptions, *, fallback_command: str = "") -> str:
    """
    lpCommandLine para CreateProcess **do cmd.exe** (não do PsExec).

    ``cmd.exe`` relê GetCommandLineW com as regras de /S — não as do CRT.
    Por isso aspas internas da cadeia NÃO são escapadas com ``\\``.

    O argv do PsExec continua sendo ``build_cmd_remote_argv`` (CRT/CommandLineToArgvW).
    """
    argv = build_cmd_remote_argv(opts, fallback_command=fallback_command)
    if len(argv) < 2:
        return "cmd"
    chain = ""
    flags = argv
    if len(argv) >= 3 and argv[-2] in ("/C", "/K") and "/S" in argv:
        chain = argv[-1]
        flags = argv[:-1]
    head = " ".join(flags)
    if not chain:
        return head
    return f'{head} "{chain}"'
