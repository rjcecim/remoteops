"""Regras de compatibilidade do PsExec v2.43 usadas pela aba PSExec.

Camadas:
    Interface  →  compute_psexec_option_state()
    Validação  →  validate_psexec_options()
    Builder    →  build_psexec_option_argv()  (sempre sanitiza)

Parâmetros proibidos neste projeto (nunca emitir): ``-r``, ``-w``, ``-x``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional

# ── constantes ──────────────────────────────────────────────────────────

PRIORITIES = (
    "-low",
    "-belownormal",
    "-abovenormal",
    "-high",
    "-realtime",
    "-background",
)

FORBIDDEN_PSEXEC_FLAGS = ("-r", "-w", "-x")

# Pares mutuamente exclusivos: (A, B, motivo)
CONFLICT_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        "-s",
        "-e",
        "A sintaxe do PsExec trata -s e -e como alternativas "
        "(SYSTEM vs. não carregar o perfil da conta).",
    ),
    (
        "-s",
        "-h",
        "-h usa o token elevado da conta informada; -s executa como SYSTEM, "
        "sem token de usuário.",
    ),
    (
        "-s",
        "-l",
        "-l restringe a conta a privilégios de Users; -s executa como SYSTEM.",
    ),
    (
        "-h",
        "-l",
        "-h eleva o token (UAC); -l executa como usuário limitado. Objetivos opostos.",
    ),
    (
        "-f",
        "-v",
        "-f força a cópia mesmo se o arquivo já existir; -v copia só se for "
        "mais novo ou de versão superior.",
    ),
)

# Quando os dois lados estão marcados sem um "último clique", manter este.
_CONFLICT_KEEP: dict[frozenset[str], str] = {
    frozenset({"-s", "-e"}): "-s",
    frozenset({"-s", "-h"}): "-s",
    frozenset({"-s", "-l"}): "-s",
    frozenset({"-h", "-l"}): "-h",
    frozenset({"-f", "-v"}): "-f",
}

_FLAG_ATTR = {
    "-h": "flag_h",
    "-s": "flag_s",
    "-e": "flag_e",
    "-l": "flag_l",
    "-c": "flag_c",
    "-f": "flag_f",
    "-v": "flag_v",
    "-d": "flag_d",
    "-arm": "flag_arm",
    "-accepteula": "flag_accepteula",
    "-nobanner": "flag_nobanner",
}

# Tooltips-base (significados oficiais do PsExec v2.43)
TOOLTIPS = {
    "-h": (
        "Se o destino for Vista ou superior, executa o processo com o token "
        "elevado da conta, se disponível."
    ),
    "-s": "Executa o processo remoto na conta SYSTEM.",
    "-e": "Não carrega o perfil da conta informada em -u.",
    "-l": (
        "Executa como usuário limitado (remove o grupo Administrators; no Vista+ "
        "roda com integridade baixa)."
    ),
    "-i": (
        "Torna o processo interativo na sessão remota. Sem ID, o PsExec usa a "
        "sessão console — não é a sessão 0 de serviços do Windows."
    ),
    "session_id": (
        "Sessão remota para -i. «não especificar» gera apenas -i (console do "
        "PsExec). Com o host online, a lista mostra os IDs reais do computador."
    ),
    "-c": "Copia o programa especificado para o sistema remoto antes de executá-lo.",
    "-f": "Forçar cópia mesmo que o arquivo já exista no computador remoto.",
    "-v": (
        "Copiar apenas se a versão for superior ou o arquivo for mais novo "
        "do que o que já está no computador remoto."
    ),
    "-d": (
        "Não aguardar o processo remoto terminar. O PsExec retorna imediatamente; "
        "o RemoteOps não receberá a saída nem o código de retorno do processo remoto."
    ),
    "-n": "Timeout em segundos para conectar ao computador remoto (0 = sem timeout).",
    "-a": (
        "Processadores nos quais o processo poderá executar (-a n,n,...). "
        "Pode ser usado sem -g. O grupo CPU é opcional e só é necessário em "
        "sistemas com mais de 64 processadores."
    ),
    "-g": (
        "Grupo de processadores (-g n). Opcional. Relevante em hosts com mais "
        "de 64 processadores. Não é obrigatório para usar afinidade (-a)."
    ),
    "-arm": "Indica ao PsExec que o computador remoto utiliza arquitetura ARM.",
    "-accepteula": "Aceitar automaticamente o EULA do PsExec.",
    "-nobanner": "Não exibir o banner do PsExec.",
    "-u": r"Usuário no formato DOMAIN\user. Usado para autenticar no host remoto.",
    "-p": (
        "Senha da conta em -u. Se o usuário estiver preenchido e a senha vazia, "
        "-p é omitido e o PsExec pode solicitar a senha no console."
    ),
    "extra_args": (
        "Argumentos do programa executado no host remoto — não são parâmetros "
        "do PsExec. Não use este campo para flags como -s, -h ou -x."
    ),
    "priority": "Prioridade do processo remoto. Apenas uma opção por vez.",
    "-low": "Prioridade baixa.",
    "-belownormal": "Prioridade abaixo do normal.",
    "-abovenormal": "Prioridade acima do normal.",
    "-high": "Prioridade alta.",
    "-realtime": "Prioridade de tempo real (pode instabilizar o host remoto).",
    "-background": (
        "Prioridade de memória e E/S ociosa (Windows Vista ou superior). "
        "Não é o mesmo que -d (não aguardar o término)."
    ),
    "priority_default": "Prioridade padrão do sistema (nenhuma flag de prioridade).",
}


# ── modelo ──────────────────────────────────────────────────────────────

@dataclass
class PsExecOptions:
    """Estado operacional das opções da aba PSExec (sem host/senha bruta)."""

    user: str = ""
    has_password: bool = False
    flag_h: bool = False
    flag_s: bool = False
    flag_e: bool = False
    flag_l: bool = False
    session_interactive: bool = False
    session_id: Optional[int] = None
    priority: str = ""
    cpu_group: Optional[int] = None
    affinity: str = ""
    timeout: int = 0
    flag_d: bool = False
    flag_c: bool = False
    flag_f: bool = False
    flag_v: bool = False
    flag_accepteula: bool = False
    flag_nobanner: bool = False
    flag_arm: bool = False
    extra_args: str = ""
    copy_allowed: bool = True

    def is_checked(self, key: str) -> bool:
        if key == "-i":
            return bool(self.session_interactive)
        attr = _FLAG_ATTR.get(key)
        if attr is None:
            raise KeyError(key)
        return bool(getattr(self, attr))

    def with_flag(self, key: str, value: bool) -> "PsExecOptions":
        if key == "-i":
            return replace(self, session_interactive=bool(value))
        attr = _FLAG_ATTR.get(key)
        if attr is None:
            raise KeyError(key)
        return replace(self, **{attr: bool(value)})


@dataclass
class WidgetState:
    enabled: bool = True
    checked: Optional[bool] = None
    tooltip: str = ""
    reason: str = ""


@dataclass
class PsExecUiState:
    """Resultado de um recálculo completo da aba."""

    options: PsExecOptions
    widgets: dict[str, WidgetState] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.widgets is None:
            self.widgets = {}


class PsExecOptionsError(ValueError):
    """Combinação inválida de opções do PsExec."""

    def __init__(self, errors: Iterable[str]):
        self.errors = [str(e) for e in errors if e]
        super().__init__("; ".join(self.errors) if self.errors else "Opções PsExec inválidas")


# ── conversão dict ↔ modelo ─────────────────────────────────────────────

def _parse_cpu_group(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text or text.casefold() in {"nenhum", "none", "n/a"}:
        return None
    head = text.split("(")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def _parse_timeout(raw) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _parse_session_id(raw) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_priority(raw) -> str:
    text = str(raw or "").strip()
    if text in PRIORITIES:
        return text
    # Texto de combo legado: "-low  Baixa"
    head = text.split()[0] if text else ""
    return head if head in PRIORITIES else ""


def options_from_params(params: Optional[dict]) -> PsExecOptions:
    """Converte o dict usado pelo CommandBuilder em ``PsExecOptions``."""
    p = dict(params or {})
    group = p.get("cpu_group")
    if group is None:
        group = _parse_cpu_group(p.get("group"))
    else:
        group = _parse_cpu_group(group)
    user = str(p.get("user") or "").strip()
    return PsExecOptions(
        user=user,
        has_password=bool(p.get("has_password")) and bool(user),
        flag_h=bool(p.get("-h")),
        flag_s=bool(p.get("-s")),
        flag_e=bool(p.get("-e")),
        flag_l=bool(p.get("-l")),
        session_interactive=bool(p.get("session_interactive")),
        session_id=_parse_session_id(p.get("session_id")),
        priority=_parse_priority(p.get("priority", "")),
        cpu_group=group,
        affinity=str(p.get("affinity") or "").strip(),
        timeout=_parse_timeout(p.get("timeout", 0)),
        flag_d=bool(p.get("-d")),
        flag_c=bool(p.get("-c")),
        flag_f=bool(p.get("-f")),
        flag_v=bool(p.get("-v")),
        flag_accepteula=bool(p.get("-accepteula")),
        flag_nobanner=bool(p.get("-nobanner")),
        flag_arm=bool(p.get("-arm")),
        extra_args=str(p.get("extra_args") or ""),
        copy_allowed=bool(p.get("copy_allowed", True)),
    )


def options_to_params(opts: PsExecOptions) -> dict:
    """Espelha o dict histórico do CommandBuilder (sem senha)."""
    return {
        "user": opts.user,
        "has_password": bool(opts.has_password) and bool(opts.user.strip()),
        "-h": opts.flag_h,
        "-s": opts.flag_s,
        "-e": opts.flag_e,
        "-l": opts.flag_l,
        "session_interactive": opts.session_interactive,
        "session_id": opts.session_id,
        "priority": opts.priority,
        "cpu_group": opts.cpu_group,
        "affinity": opts.affinity,
        "timeout": opts.timeout if opts.timeout > 0 else 0,
        "-d": opts.flag_d,
        "-c": opts.flag_c,
        "-f": opts.flag_f,
        "-v": opts.flag_v,
        "-accepteula": opts.flag_accepteula,
        "-nobanner": opts.flag_nobanner,
        "-arm": opts.flag_arm,
        "extra_args": opts.extra_args,
        "copy_allowed": opts.copy_allowed,
    }


# ── resolução de conflitos ──────────────────────────────────────────────

def _conflicts_of(flag: str) -> tuple[str, ...]:
    found: list[str] = []
    for a, b, _reason in CONFLICT_PAIRS:
        if flag == a:
            found.append(b)
        elif flag == b:
            found.append(a)
    return tuple(found)


def _conflict_reason(a: str, b: str) -> str:
    pair = frozenset({a, b})
    for left, right, reason in CONFLICT_PAIRS:
        if frozenset({left, right}) == pair:
            return reason
    return f"{a} e {b} são incompatíveis."


def _unavailable_because(flag: str, other: str) -> str:
    return f"{flag} está indisponível porque {other} já está selecionado."


def sanitize_psexec_options(
    opts: PsExecOptions,
    *,
    trigger: Optional[str] = None,
) -> PsExecOptions:
    """Resolve conflitos de forma determinística (última linha de defesa)."""
    resolved = replace(opts)

    if not resolved.copy_allowed:
        resolved = replace(resolved, flag_c=False, flag_f=False, flag_v=False)
    if not resolved.flag_c:
        resolved = replace(resolved, flag_f=False, flag_v=False)

    if not resolved.user.strip():
        resolved = replace(resolved, user="", has_password=False)
    if not resolved.session_interactive:
        resolved = replace(resolved, session_id=None)

    trigger_on = bool(trigger) and trigger in _FLAG_ATTR and resolved.is_checked(trigger)
    if trigger_on:
        for other in _conflicts_of(trigger):
            if resolved.is_checked(other):
                resolved = resolved.with_flag(other, False)
    else:
        for a, b, _reason in CONFLICT_PAIRS:
            if resolved.is_checked(a) and resolved.is_checked(b):
                keep = _CONFLICT_KEEP[frozenset({a, b})]
                drop = b if keep == a else a
                resolved = resolved.with_flag(drop, False)

    if not resolved.flag_c:
        resolved = replace(resolved, flag_f=False, flag_v=False)

    priority = _parse_priority(resolved.priority)
    timeout = _parse_timeout(resolved.timeout)
    session_id = (
        _parse_session_id(resolved.session_id) if resolved.session_interactive else None
    )
    return replace(resolved, priority=priority, timeout=timeout, session_id=session_id)


def compute_psexec_option_state(
    opts: PsExecOptions,
    *,
    trigger: Optional[str] = None,
) -> PsExecUiState:
    """Recalcula checks, habilitados e tooltips a partir do estado completo."""
    resolved = sanitize_psexec_options(opts, trigger=trigger)
    widgets: dict[str, WidgetState] = {}

    def _checkbox(flag: str, checked: bool, enabled: bool, reason: str = "") -> None:
        base = TOOLTIPS.get(flag, "")
        widgets[flag] = WidgetState(
            enabled=enabled,
            checked=checked,
            tooltip=reason or base,
            reason=reason,
        )

    copy_reason = (
        "As opções de cópia (-c/-f/-v) não se aplicam quando o arquivo "
        "é transferido via Robocopy."
    )
    c_enabled = resolved.copy_allowed
    _checkbox(
        "-c",
        resolved.flag_c,
        c_enabled,
        copy_reason if not c_enabled else "",
    )

    f_reason = ""
    f_enabled = bool(c_enabled and resolved.flag_c and not resolved.flag_v)
    if not c_enabled:
        f_reason = copy_reason
    elif not resolved.flag_c:
        f_reason = "-f requer que a opção -c esteja habilitada."
    elif resolved.flag_v:
        f_reason = _unavailable_because("-f", "-v")
    _checkbox("-f", resolved.flag_f, f_enabled, f_reason)

    v_reason = ""
    v_enabled = bool(c_enabled and resolved.flag_c and not resolved.flag_f)
    if not c_enabled:
        v_reason = copy_reason
    elif not resolved.flag_c:
        v_reason = "-v requer que a opção -c esteja habilitada."
    elif resolved.flag_f:
        v_reason = _unavailable_because("-v", "-f")
    _checkbox("-v", resolved.flag_v, v_enabled, v_reason)

    def _priv_enabled(flag: str) -> tuple[bool, str]:
        for other in _conflicts_of(flag):
            if resolved.is_checked(other):
                return False, _unavailable_because(flag, other)
        return True, ""

    for flag, checked in (
        ("-h", resolved.flag_h),
        ("-s", resolved.flag_s),
        ("-e", resolved.flag_e),
        ("-l", resolved.flag_l),
    ):
        enabled, reason = _priv_enabled(flag)
        _checkbox(flag, checked, enabled, reason)

    user_filled = bool(resolved.user.strip())
    widgets["-u"] = WidgetState(enabled=True, tooltip=TOOLTIPS["-u"])
    widgets["-p"] = WidgetState(
        enabled=user_filled,
        tooltip=(
            TOOLTIPS["-p"]
            if user_filled
            else "A senha só pode ser informada quando houver um usuário (-u)."
        ),
        reason="" if user_filled else "A senha só pode ser informada quando houver um usuário (-u).",
    )

    widgets["-i"] = WidgetState(
        enabled=True,
        checked=resolved.session_interactive,
        tooltip=TOOLTIPS["-i"],
    )
    widgets["session_id"] = WidgetState(
        enabled=resolved.session_interactive,
        tooltip=(
            TOOLTIPS["session_id"]
            if resolved.session_interactive
            else "O ID da sessão só se aplica quando a opção interativa (-i) está ativa."
        ),
        reason=(
            ""
            if resolved.session_interactive
            else "O ID da sessão só se aplica quando a opção interativa (-i) está ativa."
        ),
    )

    widgets["-d"] = WidgetState(
        enabled=True,
        checked=resolved.flag_d,
        tooltip=TOOLTIPS["-d"],
    )
    widgets["-arm"] = WidgetState(
        enabled=True,
        checked=resolved.flag_arm,
        tooltip=TOOLTIPS["-arm"],
    )
    widgets["-accepteula"] = WidgetState(
        enabled=True,
        checked=resolved.flag_accepteula,
        tooltip=TOOLTIPS["-accepteula"],
    )
    widgets["-nobanner"] = WidgetState(
        enabled=True,
        checked=resolved.flag_nobanner,
        tooltip=TOOLTIPS["-nobanner"],
    )
    widgets["-a"] = WidgetState(enabled=True, tooltip=TOOLTIPS["-a"])
    widgets["-g"] = WidgetState(enabled=True, tooltip=TOOLTIPS["-g"])
    widgets["-n"] = WidgetState(enabled=True, tooltip=TOOLTIPS["-n"])
    widgets["extra_args"] = WidgetState(enabled=True, tooltip=TOOLTIPS["extra_args"])
    widgets["priority"] = WidgetState(enabled=True, tooltip=TOOLTIPS["priority"])

    return PsExecUiState(options=resolved, widgets=widgets)


# ── validação independente ──────────────────────────────────────────────

def validate_psexec_options(opts: PsExecOptions) -> list[str]:
    """Valida o estado sem corrigi-lo. Lista vazia = válido."""
    errors: list[str] = []

    if not opts.copy_allowed and (opts.flag_c or opts.flag_f or opts.flag_v):
        errors.append(
            "As opções de cópia (-c/-f/-v) não se aplicam quando o arquivo "
            "é transferido via Robocopy."
        )
    if opts.flag_f and not opts.flag_c:
        errors.append("-f requer que a opção -c esteja habilitada.")
    if opts.flag_v and not opts.flag_c:
        errors.append("-v requer que a opção -c esteja habilitada.")
    if opts.flag_f and opts.flag_v:
        errors.append("-f e -v são mutuamente exclusivos (alternativas de -c).")

    for a, b, reason in CONFLICT_PAIRS:
        if a in {"-f", "-v"}:
            continue
        if opts.is_checked(a) and opts.is_checked(b):
            errors.append(f"{a} e {b} são incompatíveis: {reason}")

    if opts.has_password and not opts.user.strip():
        errors.append("A senha (-p) exige um usuário (-u).")

    if opts.session_id is not None and not opts.session_interactive:
        errors.append("O ID da sessão só se aplica quando -i está ativo.")

    priority = str(opts.priority or "").strip()
    if priority and priority not in PRIORITIES:
        errors.append(f"Prioridade inválida: {priority}.")

    if opts.timeout is not None:
        try:
            timeout_i = int(opts.timeout)
        except (TypeError, ValueError):
            errors.append("Timeout (-n) deve ser um inteiro em segundos.")
        else:
            if timeout_i < 0:
                errors.append("Timeout (-n) não pode ser negativo.")

    if opts.cpu_group is not None:
        try:
            int(opts.cpu_group)
        except (TypeError, ValueError):
            errors.append("Grupo CPU (-g) deve ser um inteiro.")

    affinity = (opts.affinity or "").strip()
    if affinity:
        parts = [p.strip() for p in affinity.split(",") if p.strip()]
        if not parts or any(not p.isdigit() or int(p) < 1 for p in parts):
            errors.append("Afinidade CPU (-a) deve ser uma lista de processadores 1-based.")
        elif len(parts) != len(set(parts)):
            errors.append("Afinidade CPU (-a) não pode repetir processadores.")

    return errors


def assert_psexec_options(opts: PsExecOptions) -> PsExecOptions:
    """Levanta ``PsExecOptionsError`` se o estado for inválido."""
    errors = validate_psexec_options(opts)
    if errors:
        raise PsExecOptionsError(errors)
    return opts


# ── builder de flags ────────────────────────────────────────────────────

def build_psexec_option_argv(opts: PsExecOptions) -> list[str]:
    """
    Gera somente as flags do PsExec (sem exe, host, -u/-p, cmd ou args extras).

    Sempre sanitiza antes de emitir — nunca devolve combinações impossíveis
    nem ``-r``/``-w``/``-x``.
    """
    opts = sanitize_psexec_options(opts)
    cmd: list[str] = []

    if opts.flag_accepteula:
        cmd.append("-accepteula")
    if opts.flag_nobanner:
        cmd.append("-nobanner")

    if opts.timeout > 0:
        cmd.extend(["-n", str(int(opts.timeout))])

    if opts.flag_h:
        cmd.append("-h")
    if opts.flag_l:
        cmd.append("-l")
    if opts.flag_s:
        cmd.append("-s")
    elif opts.flag_e:
        cmd.append("-e")

    if opts.session_interactive:
        if opts.session_id is None:
            cmd.append("-i")
        else:
            cmd.extend(["-i", str(int(opts.session_id))])

    if opts.flag_c:
        cmd.append("-c")
        if opts.flag_f:
            cmd.append("-f")
        elif opts.flag_v:
            cmd.append("-v")

    if opts.flag_d:
        cmd.append("-d")

    if opts.priority in PRIORITIES:
        cmd.append(opts.priority)

    if opts.cpu_group is not None:
        cmd.extend(["-g", str(int(opts.cpu_group))])

    affinity = (opts.affinity or "").strip()
    if affinity:
        cmd.extend(["-a", affinity])

    if opts.flag_arm:
        cmd.append("-arm")

    for forbidden in FORBIDDEN_PSEXEC_FLAGS:
        if forbidden in cmd:
            cmd = [tok for tok in cmd if tok != forbidden]

    return cmd


def build_psexec_prefix_argv(
    *,
    executable: str,
    host: str,
    user: str = "",
    password_placeholder: Optional[str] = None,
    opts: Optional[PsExecOptions] = None,
) -> list[str]:
    """
    ``PsExec.exe \\\\HOST [opções]`` — sem o comando remoto.

    ``password_placeholder`` só é incluído quando há usuário. O valor real
    da senha nunca deve ser passado aqui no preview.
    """
    opts = sanitize_psexec_options(opts or PsExecOptions())
    host = (host or "").strip().strip("\\")
    argv = [executable, f"\\\\{host}"]
    argv.extend(build_psexec_option_argv(opts))

    user = (user or "").strip()
    if not user:
        return argv

    auth = ["-u", user]
    if password_placeholder:
        auth.extend(["-p", password_placeholder])

    insert_at = 2
    while insert_at < len(argv) and argv[insert_at] in ("-accepteula", "-nobanner"):
        insert_at += 1
    return argv[:insert_at] + auth + argv[insert_at:]
