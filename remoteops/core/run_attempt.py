"""Tentativa de execução: plano congelado e fase operacional.

A pré-visualização e o lançamento devem usar o mesmo ``CommandSpec``.
A senha nunca entra neste módulo — só o placeholder já presente no argv.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Sequence

from remoteops.core.models import CommandSpec


class RunPhase(str, Enum):
    """Fase da tentativa ativa. IDLE = nenhuma tentativa em andamento."""

    IDLE = "idle"
    PRECHECKING = "prechecking"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


_BUSY_PHASES = frozenset(
    {
        RunPhase.PRECHECKING,
        RunPhase.STARTING,
        RunPhase.RUNNING,
        RunPhase.STOPPING,
    }
)


def freeze_plan(specs: Sequence[CommandSpec]) -> tuple[CommandSpec, ...]:
    """Cópia defensiva do plano (args e metadata independentes)."""
    frozen: list[CommandSpec] = []
    for spec in specs:
        frozen.append(
            replace(
                spec,
                args=list(spec.args),
                metadata=dict(spec.metadata or {}),
            )
        )
    return tuple(frozen)


def plan_blocking_errors(specs: Sequence[CommandSpec]) -> list[str]:
    """Erros estruturados do plano (não depende só de ``# Erro`` no display)."""
    errors: list[str] = []
    meta_keys = (
        "psexec_errors",
        "powershell_errors",
        "cmd_errors",
        "robocopy_errors",
    )
    for spec in specs:
        meta = spec.metadata or {}
        for key in meta_keys:
            raw = meta.get(key) or []
            if isinstance(raw, str):
                errors.append(raw)
            else:
                errors.extend(str(item) for item in raw if item)
        display = (spec.display_command or "").strip()
        if display.startswith("# Erro") and not any(meta.get(key) for key in meta_keys):
            errors.append(display.lstrip("# ").strip())
    return errors


@dataclass(frozen=True)
class RunAttempt:
    """Uma tentativa de Executar, com plano já validado."""

    attempt_id: str
    host: str
    plan: tuple[CommandSpec, ...]
    external_console: bool
    display_command: str = ""
    metadata: dict = field(default_factory=dict)

    @staticmethod
    def create(
        *,
        host: str,
        plan: Sequence[CommandSpec],
        external_console: bool,
        display_command: str = "",
    ) -> "RunAttempt":
        frozen = freeze_plan(plan)
        return RunAttempt(
            attempt_id=uuid.uuid4().hex,
            host=(host or "").strip(),
            plan=frozen,
            external_console=bool(external_console),
            display_command=display_command or "\n".join(
                s.display_command for s in frozen if s.display_command
            ),
            metadata={},
        )


def phase_is_busy(phase: RunPhase) -> bool:
    return phase in _BUSY_PHASES


def new_execution_blocked(
    *,
    phase_busy: bool,
    executor_busy: bool,
    awaiting_followup: bool,
    # Compatibilidade com chamadas/testes antigos:
    stop_enabled: Optional[bool] = None,
    precheck_pending: Optional[bool] = None,
) -> bool:
    """True quando já existe tentativa, execução ou Robocopy→PsExec pendente."""
    busy = bool(phase_busy or executor_busy or awaiting_followup)
    if stop_enabled is not None:
        busy = busy or bool(stop_enabled)
    if precheck_pending is not None:
        busy = busy or bool(precheck_pending)
    return busy


def run_button_tooltip(phase: RunPhase, *, can_run: bool) -> str:
    if phase == RunPhase.PRECHECKING:
        return "Verificando TCP 445…"
    if phase == RunPhase.STARTING:
        return "Iniciando a operação…"
    if phase == RunPhase.RUNNING:
        return "Uma operação já está em execução."
    if phase == RunPhase.STOPPING:
        return "Encerrando a operação…"
    if can_run:
        return "Executar"
    return "Informe um computador acessível na porta TCP 445."


def stop_button_tooltip(phase: RunPhase) -> str:
    if phase == RunPhase.PRECHECKING:
        return "Cancelar a verificação de TCP 445."
    if phase == RunPhase.STOPPING:
        return "Encerramento já solicitado."
    if phase in {RunPhase.STARTING, RunPhase.RUNNING}:
        return (
            "Encerrar sessão (envia exit). "
            "Segundo clique encerra o processo local."
        )
    return "Nenhuma operação para encerrar."
