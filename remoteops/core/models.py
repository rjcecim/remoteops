"""Modelos de domínio tipados para construção e execução de comandos.

Mantemos apenas tipos realmente usados pela aplicação. Opções tipadas
(PsExecOptions, MSIOptions, etc.) foram removidas na 2ª rodada porque o
CommandBuilder continua baseado em dicts da UI — abstrações mortas aumentavam
dívida técnica sem benefício.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Sequence

from remoteops.utils.redaction import format_argv_for_display, redact_command_text


class OperationStatus(str, Enum):
    """Estado de alto nível de uma operação administrativa."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


@dataclass
class FileSelection:
    mode: str = "file"  # 'file' | 'folder'
    file: Optional[str] = None
    folder: Optional[str] = None

    @classmethod
    def from_any(cls, selection: Any) -> Optional["FileSelection"]:
        if selection is None:
            return None
        if isinstance(selection, FileSelection):
            return selection
        if isinstance(selection, dict):
            return cls(
                mode=str(selection.get("mode") or "file"),
                file=selection.get("file"),
                folder=selection.get("folder"),
            )
        if isinstance(selection, str):
            return cls(mode="file", file=selection, folder=None)
        return None


@dataclass
class CommandSpec:
    """
    Representação estruturada de um processo a executar.

    ``args`` é a lista para subprocess. No fluxo PsExec via CommandBuilder,
    a senha bruta NÃO fica no builder: o argv de preview usa placeholder e
    a materialização ocorre na execução.
    ``display_command`` é SEMPRE sanitizado — nunca use para executar.
    """

    executable: str
    args: list[str] = field(default_factory=list)
    cwd: Optional[str] = None
    display_command: str = ""
    has_secrets: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    # Quando True, a execução preferida é terminal externo (experiência atual)
    prefer_external_console: bool = False
    # Texto legado multi-linha (robocopy\\npsexec) apenas para display
    legacy_display: str = ""

    @property
    def argv(self) -> list[str]:
        """Lista completa [executable, *args] para Popen."""
        if self.args and self.args[0] == self.executable:
            return list(self.args)
        return [self.executable, *self.args]

    @classmethod
    def from_argv(
        cls,
        argv: Sequence[str],
        *,
        has_secrets: bool = False,
        passwords: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        metadata: Optional[dict] = None,
        prefer_external_console: bool = False,
    ) -> "CommandSpec":
        argv_list = [str(a) for a in argv if a is not None]
        if not argv_list:
            raise ValueError("argv vazio")
        display = format_argv_for_display(argv_list)
        if passwords:
            display = redact_command_text(display, passwords=passwords)
        return cls(
            executable=argv_list[0],
            args=argv_list[1:],
            cwd=cwd,
            display_command=display,
            has_secrets=has_secrets or bool(passwords),
            metadata=dict(metadata or {}),
            prefer_external_console=prefer_external_console,
        )

    def sanitized_display(self, passwords: Optional[Sequence[str]] = None) -> str:
        text = self.display_command or format_argv_for_display(self.argv)
        return redact_command_text(text, passwords=passwords)


@dataclass
class ExecutionResult:
    return_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    cancelled: bool = False
    timed_out: bool = False
    success: bool = False
    exception: Optional[str] = None
    status: OperationStatus = OperationStatus.UNKNOWN
    # Cancelamento local não implica cancelamento remoto (PsExec)
    remote_may_continue: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def finalize(self) -> "ExecutionResult":
        if self.started_at and self.finished_at:
            self.duration_seconds = (self.finished_at - self.started_at).total_seconds()
        if self.cancelled:
            self.status = OperationStatus.CANCELLED
            self.success = False
        elif self.timed_out:
            self.status = OperationStatus.TIMED_OUT
            self.success = False
        elif self.exception:
            self.status = OperationStatus.FAILED
            self.success = False
        elif self.return_code is None:
            self.status = OperationStatus.UNKNOWN
            self.success = False
        elif self.return_code == 0:
            self.status = OperationStatus.COMPLETED
            self.success = True
        else:
            # Robocopy 0-7 tratado pelo chamador via metadata
            self.status = OperationStatus.FAILED
            self.success = False
        return self


def is_robocopy_success(exit_code: int) -> bool:
    """Robocopy: 0-7 = sucesso/avisos; >= 8 = falha."""
    return 0 <= int(exit_code) <= 7
