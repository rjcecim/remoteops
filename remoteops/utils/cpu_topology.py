"""Topologia de processadores do computador remoto para -g e -a do PsExec.

PsExec v2.43:
- ``-g n`` escolhe o grupo de processadores. A numeração é a mesma de
  ``GetActiveProcessorCount``: começa em 0.
- Sem ``-g``, o processo fica no grupo do serviço do PsExec, em geral o grupo 0.
- ``-a`` lista processadores dentro desse grupo. O 1 é o primeiro processador
  do grupo. Um grupo tem no máximo 64 processadores lógicos.
- A soma de processadores do computador não informa o tamanho de cada grupo.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from remoteops.utils.ping import is_valid_host, normalize_host

CPU_QUERY_TIMEOUT = 20.0
_MAX_GROUP_PROCESSORS = 64
_QUERY_FLAGS = ("-accepteula", "-nobanner")

_GROUPS_RE = re.compile(r"^GROUPS\s+(\d+)\s*$", re.IGNORECASE)
_GROUP_RE = re.compile(r"^GROUP\s+(\d+)\s+(\d+)\s*$", re.IGNORECASE)
_COMPUTER_RE = re.compile(r"^COMPUTER\s+(\S+)\s*$", re.IGNORECASE)
_NOISE_RE = re.compile(
    r"^(connecting to\b|starting\b|copyright\b|sysinternals\b|psexec v)",
    re.IGNORECASE,
)

_CPU_SCRIPT = r"""
$src = @'
using System;
using System.Runtime.InteropServices;
public static class RemoteOpsCpuTopology {
  [DllImport("kernel32.dll")]
  public static extern ushort GetActiveProcessorGroupCount();
  [DllImport("kernel32.dll")]
  public static extern uint GetActiveProcessorCount(ushort groupNumber);
}
'@
$__lines = New-Object System.Collections.Generic.List[string]
try {
  Add-Type -TypeDefinition $src -ErrorAction Stop
  $n = [RemoteOpsCpuTopology]::GetActiveProcessorGroupCount()
  $__lines.Add(('GROUPS {0}' -f $n))
  for ($i = 0; $i -lt [int]$n; $i++) {
    $c = [RemoteOpsCpuTopology]::GetActiveProcessorCount([uint16]$i)
    $__lines.Add(('GROUP {0} {1}' -f $i, $c))
  }
  if ($env:COMPUTERNAME) {
    $__lines.Add(('COMPUTER {0}' -f $env:COMPUTERNAME))
  }
} catch {
  $__lines.Add('CPU_TOPOLOGY_ERROR')
}
$__text = ($__lines -join "`r`n")
if (Test-Path variable:__roFile) {
  try {
    [System.IO.File]::WriteAllText(
      $__roFile, $__text, [System.Text.UTF8Encoding]::new($false))
  } catch {}
}
Write-Output $__text
if ($__lines -contains 'CPU_TOPOLOGY_ERROR') { exit 1 }
"""


@dataclass(frozen=True)
class ProcessorGroup:
    group_id: int
    logical_count: int


@dataclass(frozen=True)
class CpuTopology:
    """Topologia lida de um computador. Sem dados, o objeto não deve existir."""

    host: str
    groups: tuple[ProcessorGroup, ...]
    computer: str = ""
    queried_at: Optional[datetime] = None

    @property
    def logical_processors(self) -> int:
        return sum(group.logical_count for group in self.groups)


@dataclass(frozen=True)
class AffinityCheck:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    normalized: str


def _join_numbers(numbers: Sequence[int]) -> str:
    values = [str(int(number)) for number in numbers]
    if len(values) <= 1:
        return values[0] if values else ""
    if len(values) == 2:
        return f"{values[0]} e {values[1]}"
    return ", ".join(values[:-1]) + f" e {values[-1]}"


def _duplicate_message(numbers: Sequence[int]) -> str:
    shown = _join_numbers(numbers)
    if len(numbers) == 1:
        return f"Afinidade CPU repete o processador {shown}."
    return f"Afinidade CPU repete os processadores {shown}."


def _range_message(numbers: Sequence[int], limit: int, group_label: str) -> str:
    shown = _join_numbers(numbers)
    where = f"do {group_label}" if group_label else "do intervalo"
    span = f"{where} (1–{int(limit)})"
    if len(numbers) == 1:
        return f"Afinidade CPU {shown} está fora {span}."
    return f"Afinidade CPU {shown} estão fora {span}."


def topology_summary(topology: CpuTopology) -> str:
    processors = int(topology.logical_processors)
    groups = len(topology.groups)
    processor_word = "processador lógico" if processors == 1 else "processadores lógicos"
    group_word = "grupo CPU" if groups == 1 else "grupos CPU"
    return (
        f"{processors} {processor_word} · {groups} {group_word} · Dados atualizados"
    )


def parse_cpu_topology_output(
    text: str,
) -> tuple[tuple[ProcessorGroup, ...], str, str]:
    """Lê GROUPS/GROUP. Não completa lacunas nem usa a soma como tamanho de grupo."""
    computer = ""
    declared: Optional[int] = None
    found: dict[int, int] = {}
    saw_marker = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or _NOISE_RE.search(line):
            continue
        if line.casefold() == "cpu_topology_error":
            return (
                (),
                "",
                "Não foi possível ler os grupos de processadores neste computador.",
            )
        group_match = _GROUP_RE.match(line)
        if group_match:
            saw_marker = True
            group_id = int(group_match.group(1))
            count = int(group_match.group(2))
            previous = found.get(group_id)
            if previous is not None and previous != count:
                return (), "", "A resposta da topologia é inconsistente."
            found[group_id] = count
            continue
        groups_match = _GROUPS_RE.match(line)
        if groups_match:
            saw_marker = True
            value = int(groups_match.group(1))
            if declared is not None and declared != value:
                return (), "", "A resposta da topologia é inconsistente."
            declared = value
            continue
        computer_match = _COMPUTER_RE.match(line)
        if computer_match:
            name = computer_match.group(1).strip()
            if computer and computer.casefold() != name.casefold():
                return (), "", "A resposta da topologia é inconsistente."
            computer = name
    if not saw_marker or declared is None:
        return (), "", "A consulta não devolveu os grupos de processadores."
    if declared < 1:
        return (), "", "Nenhum grupo de processadores foi informado."
    expected = list(range(declared))
    if sorted(found) != expected:
        return (), "", "A resposta da topologia é inconsistente."
    groups: list[ProcessorGroup] = []
    for group_id in expected:
        count = found[group_id]
        if count < 1 or count > _MAX_GROUP_PROCESSORS:
            return (), "", "A resposta da topologia é inconsistente."
        groups.append(ProcessorGroup(group_id, count))
    return tuple(groups), computer, ""


def classify_cpu_query(error: str) -> tuple[str, str]:
    """Classifica falha de consulta sem devolver senha, argv ou texto de exceção."""
    text = (error or "").strip()
    if not text:
        return "ok", ""
    folded = text.casefold()
    if (
        "acesso negado" in folded
        or "access is denied" in folded
        or "access denied" in folded
        or "logon failure" in folded
        or re.search(r"winerror\s+5\b", folded) is not None
    ):
        return "denied", "Acesso negado."
    if any(
        token in folded
        for token in ("timed out", "timeout", "tempo esgotado", "tempo limite")
    ):
        return "timeout", "Tempo esgotado ao consultar os processadores."
    if (
        "host inválido" in folded
        or "inacessível" in folded
        or "indisponível" in folded
        or "network path" in folded
        or "caminho da rede" in folded
        or "couldn't access" in folded
        or "could not access" in folded
        or "não foi possível acessar" in folded
        or "nao foi possivel acessar" in folded
        or "identificador inválido" in folded
        or "identificador invalido" in folded
        or "rpc server is unavailable" in folded
        or re.search(r"winerror\s+(53|64|67|1203|1231|1232|1722)\b", folded) is not None
    ):
        return "unavailable", "Computador indisponível."
    if "cpu_topology_error" in folded:
        return (
            "failed",
            "Não foi possível ler os grupos de processadores neste computador.",
        )
    return "ok", ""


def classify_affinity_input(
    text: str,
    *,
    max_cpu: Optional[int] = None,
    group_label: str = "",
) -> tuple[str, str]:
    """Estado de digitação: acceptable, intermediate ou invalid.

    Vírgula final é intermediate. Índice inválido e duplicata são invalid.
    Sem ``max_cpu``, o intervalo não é julgado.
    """
    raw = text or ""
    if not raw.strip():
        return "acceptable", ""
    if any(char not in "0123456789, " for char in raw):
        return "invalid", "Afinidade CPU aceita apenas números separados por vírgula."
    squashed = raw.strip().replace(" ", "")
    if squashed.startswith(",") or ",," in squashed:
        return "invalid", "Afinidade CPU aceita apenas números separados por vírgula."
    trailing = squashed.endswith(",")
    body = squashed[:-1] if trailing else squashed
    if not body:
        return "invalid", "Afinidade CPU aceita apenas números separados por vírgula."
    numbers: list[int] = []
    for part in body.split(","):
        if not part.isdigit():
            return "invalid", "Afinidade CPU aceita apenas números separados por vírgula."
        value = int(part)
        if value < 1:
            return "invalid", "Afinidade CPU deve usar processadores a partir de 1."
        numbers.append(value)
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    if duplicates:
        return "invalid", _duplicate_message(duplicates)
    limit: Optional[int]
    try:
        limit = int(max_cpu) if max_cpu is not None else None
    except (TypeError, ValueError):
        limit = None
    if limit is not None and limit >= 1:
        out_of_range = [number for number in numbers if number > limit]
        if out_of_range:
            return "invalid", _range_message(out_of_range, limit, group_label)
    if trailing:
        return "intermediate", "Afinidade CPU está incompleta."
    return "acceptable", ""


def check_affinity(
    text: str,
    *,
    limit: Optional[int] = None,
    group_label: str = "",
) -> AffinityCheck:
    """Validação final. Não remove processadores para tornar o valor executável."""
    raw = (text or "").strip()
    if not raw:
        return AffinityCheck((), (), "")
    state, reason = classify_affinity_input(
        raw, max_cpu=limit, group_label=group_label
    )
    if state == "intermediate":
        return AffinityCheck(("Afinidade CPU está incompleta.",), (), "")
    if state == "invalid":
        return AffinityCheck((reason,), (), "")
    numbers = [int(part) for part in raw.replace(" ", "").split(",") if part]
    normalized = ",".join(str(number) for number in numbers)
    if limit is None:
        return AffinityCheck(
            (),
            ("A afinidade não foi conferida com a topologia do computador remoto.",),
            normalized,
        )
    return AffinityCheck((), (), normalized)


def affinity_limit_for(
    cpu_group: Optional[int],
    groups: Sequence[object],
    *,
    verified: bool,
) -> tuple[Optional[int], str]:
    """Limite 1-based do grupo. Sem -g, o limite é o do grupo 0, se ele foi lido."""
    if not verified:
        return None, ""
    parsed: list[ProcessorGroup] = []
    for item in groups:
        if isinstance(item, ProcessorGroup):
            parsed.append(item)
            continue
        if isinstance(item, (tuple, list)) and len(item) == 2:
            try:
                parsed.append(ProcessorGroup(int(item[0]), int(item[1])))
            except (TypeError, ValueError):
                return None, ""
    target = 0 if cpu_group is None else int(cpu_group)
    for group in parsed:
        if group.group_id == target and group.logical_count >= 1:
            return group.logical_count, f"grupo {target}"
    return None, ""


def check_cpu_group(
    cpu_group: Optional[int],
    groups: Sequence[object],
    *,
    verified: bool,
    unavailable: bool = False,
    block_reason: str = "",
) -> tuple[str, ...]:
    reason = (block_reason or "").strip()
    if unavailable or reason:
        return (reason or "O grupo CPU selecionado não está disponível neste computador.",)
    if cpu_group is None:
        return ()
    if isinstance(cpu_group, bool):
        return ("Grupo CPU (-g) deve ser um inteiro entre 0 e 65535.",)
    try:
        group_id = int(cpu_group)
    except (TypeError, ValueError):
        return ("Grupo CPU (-g) deve ser um inteiro.",)
    if group_id < 0 or group_id > 65535:
        return ("Grupo CPU (-g) deve ser um inteiro entre 0 e 65535.",)
    if not verified:
        return ()
    known = {group.group_id for group in _as_groups(groups)}
    if group_id not in known:
        return (f"Grupo CPU {group_id} não existe neste computador.",)
    return ()


def _as_groups(groups: Sequence[object]) -> tuple[ProcessorGroup, ...]:
    parsed: list[ProcessorGroup] = []
    for item in groups:
        if isinstance(item, ProcessorGroup):
            parsed.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            try:
                parsed.append(ProcessorGroup(int(item[0]), int(item[1])))
            except (TypeError, ValueError):
                continue
    return tuple(parsed)


def unverified_notice(*, affinity: str, cpu_group: Optional[int]) -> str:
    has_affinity = bool((affinity or "").strip())
    has_group = cpu_group is not None
    if has_affinity and has_group:
        return "Grupo e afinidade não foram conferidos com este computador."
    if has_group:
        return "O grupo CPU não foi confirmado neste computador."
    if has_affinity:
        return "A afinidade não foi conferida com este computador."
    return ""


def describe_cpu_status(
    *,
    state: str,
    topology: Optional[CpuTopology],
    affinity: str,
    cpu_group: Optional[int],
    failure: str = "",
    group_block_reason: str = "",
) -> tuple[str, str]:
    """Texto do card e dica. Não inclui contagem que não tenha sido lida."""
    failure = (failure or "").strip()
    block = (group_block_reason or "").strip()
    if state == "querying":
        return (
            "Consultando processadores…",
            "Consultando a topologia do computador remoto.",
        )
    if state == "unavailable":
        return (
            "Indisponível.",
            "O computador remoto não está acessível para a consulta.",
        )
    if state == "available" and topology is not None:
        summary = topology_summary(topology)
        details = [summary]
        if topology.computer:
            details.append(f"Computador {topology.computer}.")
        if topology.queried_at is not None:
            clock = topology.queried_at.astimezone().strftime("%H:%M:%S")
            details.append(f"Consulta às {clock}.")
        messages: list[str] = []
        limit: Optional[int] = None
        label = ""
        if block:
            messages.append(block)
        else:
            limit, label = affinity_limit_for(
                cpu_group, topology.groups, verified=True
            )
        checked = check_affinity(
            affinity,
            limit=None if block else limit,
            group_label=label,
        )
        messages.extend(checked.errors)
        tooltip = " ".join(details)
        if messages:
            return " ".join(messages), tooltip
        if (
            cpu_group is None
            and (affinity or "").strip()
            and len(topology.groups) > 1
        ):
            tooltip = f"{tooltip} Sem -g, a afinidade foi conferida no grupo 0."
        return summary, tooltip

    base = {
        "stale": "Desatualizado.",
        "failed": failure or "Falha ao consultar os processadores.",
        "not_queried": "Não consultado.",
    }.get(state, "Não consultado.")
    if block:
        return block, block
    format_errors = check_affinity(affinity, limit=None).errors
    if format_errors:
        return f"{base} {' '.join(format_errors)}", base
    notice = unverified_notice(affinity=affinity, cpu_group=cpu_group)
    text = f"{base} {notice}" if notice else base
    return text, text


def _public_query_error(text: str, parse_error: str) -> str:
    kind, message = classify_cpu_query(text)
    if kind in {"denied", "timeout", "unavailable", "failed"}:
        return message
    return parse_error or "Falha ao consultar os processadores."


def _run_cancellable(
    argv: Sequence[str],
    timeout: float,
    should_cancel: Optional[Callable[[], bool]],
):
    from remoteops.core.win_cmd import CREATE_NO_WINDOW

    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=flags,
    )
    deadline = time.monotonic() + float(timeout)
    cancelled = False
    timed_out = False
    try:
        while proc.poll() is None:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
        if cancelled or timed_out:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            if cancelled:
                return None
            raise subprocess.TimeoutExpired(argv[0] if argv else "PsExec.exe", timeout)
        out, err = proc.communicate(timeout=2)
        return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def _discard_topology_file(host: str, remote_file: str) -> None:
    from remoteops.winget.result_file import delete_remote_artifact

    name = os.path.basename(remote_file.replace("/", "\\"))
    delete_remote_artifact(
        f"\\\\{host}\\ADMIN$\\Temp\\{name}",
        f"\\\\{host}\\C$\\Windows\\Temp\\{name}",
    )


def _read_topology_file(host: str, remote_file: str) -> tuple[str, str]:
    """Lê o texto gravado em Windows\\Temp. A senha não entra nesse caminho."""
    from remoteops.winget.result_file import delete_remote_artifact

    name = os.path.basename(remote_file.replace("/", "\\"))
    paths = (
        f"\\\\{host}\\ADMIN$\\Temp\\{name}",
        f"\\\\{host}\\C$\\Windows\\Temp\\{name}",
    )
    denied = False
    for _attempt in range(4):
        for path in paths:
            try:
                if not os.path.exists(path):
                    continue
                data = Path(path).read_bytes()
            except OSError as exc:
                if getattr(exc, "winerror", None) in {5, 1326}:
                    denied = True
                continue
            delete_remote_artifact(path)
            return data.decode("utf-8", errors="replace"), ""
        time.sleep(0.1)
    delete_remote_artifact(*paths)
    if denied:
        return "", "Acesso negado."
    return "", ""


def _topology_from_text(
    host: str,
    text: str,
) -> tuple[Optional[CpuTopology], str]:
    groups, computer, parse_error = parse_cpu_topology_output(text)
    if parse_error or not groups:
        return None, parse_error
    return (
        CpuTopology(
            host=host,
            groups=groups,
            computer=computer,
            queried_at=datetime.now(timezone.utc),
        ),
        "",
    )


def query_remote_cpu_topology(
    host: str,
    *,
    user: str = "",
    password: str = "",
    timeout: float = CPU_QUERY_TIMEOUT,
    pstools_dir: str = "",
    run: Optional[Callable[..., object]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    read_result: Optional[Callable[[str], tuple[str, str]]] = None,
) -> tuple[Optional[CpuTopology], str]:
    """Consulta o host com a conta configurada. Não usa os processadores locais.

    A senha segue só até o processo do PsExec. Falhas devolvem frase fixa.
    """
    target = normalize_host(host)
    if not target or not is_valid_host(target):
        return None, "Host inválido."
    if should_cancel is not None and should_cancel():
        return None, ""
    # O PsExec não devolve o stdout do PowerShell quando a saída é um pipe.
    # O script grava o mesmo texto em Windows\Temp e a leitura volta por ADMIN$/C$.
    remote_file = ""
    if run is None or read_result is not None:
        remote_file = f"C:\\Windows\\Temp\\RemoteOpsCpu_{uuid.uuid4().hex[:16]}.txt"
    try:
        from remoteops.core.console_codec import decode_console_bytes
        from remoteops.utils.inventory.remote_exec import build_remote_powershell_argv

        argv = build_remote_powershell_argv(
            target,
            _CPU_SCRIPT,
            user=user or "",
            password=password or "",
            pstools_dir=pstools_dir,
            extra_flags=list(_QUERY_FLAGS),
            result_path=remote_file,
        )
    except Exception:
        return None, "Falha ao consultar os processadores."
    runner = run or (
        lambda command, timeout: _run_cancellable(command, timeout, should_cancel)
    )
    try:
        completed = runner(argv, timeout)
    except subprocess.TimeoutExpired:
        if remote_file and read_result is None:
            _discard_topology_file(target, remote_file)
        return None, "Tempo esgotado ao consultar os processadores."
    except Exception:
        if remote_file and read_result is None:
            _discard_topology_file(target, remote_file)
        return None, "Falha ao consultar os processadores."
    if completed is None or (should_cancel is not None and should_cancel()):
        if remote_file and read_result is None:
            _discard_topology_file(target, remote_file)
        return None, ""
    stdout = getattr(completed, "stdout", b"") or b""
    stderr = getattr(completed, "stderr", b"") or b""
    if isinstance(stdout, str):
        text = stdout
    else:
        text = decode_console_bytes(bytes(stdout))
    if isinstance(stderr, str):
        error_text = stderr
    else:
        error_text = decode_console_bytes(bytes(stderr))
    topology, parse_error = _topology_from_text(target, text)
    if topology is not None:
        if remote_file:
            _read_topology_file(target, remote_file)
        return topology, ""
    file_text = ""
    file_error = ""
    if remote_file:
        reader = read_result or (lambda path: _read_topology_file(target, path))
        try:
            file_text, file_error = reader(remote_file)
        except Exception:
            file_text, file_error = "", "Falha ao consultar os processadores."
    if file_error:
        return None, file_error
    if file_text:
        topology, file_parse_error = _topology_from_text(target, file_text)
        if topology is not None:
            return topology, ""
        parse_error = file_parse_error or parse_error
    combined = "\n".join(part for part in (text, error_text, file_text) if part)
    return None, _public_query_error(combined, parse_error)
