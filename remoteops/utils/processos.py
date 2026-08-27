"""PsList / PsKill / PsSuspend — argv, parsers e formatação."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from remoteops.utils.pstools import get_pstools_dir, resolve_pstools_tool

PSLIST_NAMES: Tuple[str, ...] = ("PsList64.exe", "PsList.exe")
PSKILL_NAMES: Tuple[str, ...] = ("PsKill64.exe", "PsKill.exe")
PSSUSPEND_NAMES: Tuple[str, ...] = ("PsSuspend64.exe", "PsSuspend.exe")

PSLIST_TIMEOUT_SECONDS = 90.0
PSKILL_TIMEOUT_SECONDS = 60.0
PSSUSPEND_TIMEOUT_SECONDS = 60.0


@dataclass
class ProcessRow:
    name: str
    pid: int
    pri: Optional[int] = None
    threads: Optional[int] = None
    handles: Optional[int] = None
    priv_kb: Optional[int] = None
    cpu_time: str = ""
    elapsed_time: str = ""
    vm_kb: Optional[int] = None
    ws_kb: Optional[int] = None
    depth: int = 0
    parent_pid: Optional[int] = None


@dataclass
class ProcessTreeNode:
    process: ProcessRow
    children: List["ProcessTreeNode"] = field(default_factory=list)


@dataclass
class ProcessMemoryInfo:
    name: str
    pid: int
    vm_kb: Optional[int] = None
    ws_kb: Optional[int] = None
    priv_kb: Optional[int] = None
    priv_peak_kb: Optional[int] = None
    faults: Optional[int] = None
    nonpaged_kb: Optional[int] = None
    paged_kb: Optional[int] = None


@dataclass
class ProcessThreadRow:
    tid: int
    pri: Optional[int] = None
    context_switches: Optional[int] = None
    state: str = ""
    user_time: str = ""
    kernel_time: str = ""
    elapsed_time: str = ""


@dataclass
class ProcessDetailInfo:
    name: str
    pid: int
    pri: Optional[int] = None
    cpu_time: str = ""
    threads: Optional[int] = None
    handles: Optional[int] = None
    memory: Optional[ProcessMemoryInfo] = None
    thread_rows: List[ProcessThreadRow] = field(default_factory=list)
    raw_text: str = ""


def build_remote_target(host: str) -> str:
    h = (host or "").strip().strip("\\")
    if not h:
        return ""
    return f"\\\\{h}"


def resolve_pslist_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSLIST_NAMES)


def resolve_pskill_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSKILL_NAMES)


def resolve_pssuspend_exe(pstools_dir: str = "") -> str:
    return resolve_pstools_tool(pstools_dir or get_pstools_dir(), PSSUSPEND_NAMES)


def pslist_available(pstools_dir: str = "") -> bool:
    path = resolve_pslist_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def pskill_available(pstools_dir: str = "") -> bool:
    path = resolve_pskill_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def pssuspend_available(pstools_dir: str = "") -> bool:
    path = resolve_pssuspend_exe(pstools_dir)
    return bool(path) and os.path.isfile(path)


def _append_auth(args: List[str], user: str, password: str) -> None:
    u = (user or "").strip()
    if not u:
        return
    args.extend(["-u", u])
    if (password or "").strip():
        args.extend(["-p", password])


def build_pslist_argv(
    exe: str,
    host: str,
    *,
    tree: bool = False,
    extended: bool = False,
    memory: bool = False,
    threads: bool = False,
    pid: Optional[int] = None,
    nobanner: bool = True,
    user: str = "",
    password: str = "",
) -> List[str]:
    """
    Monta argv do PsList.

    Uso oficial::
        pslist [-d] [-m] [-x] [-t] [-nobanner]
               [\\\\computer [-u user [-p pass]]] [name | pid]
    """
    target = build_remote_target(host)
    if not exe or not target:
        return []
    args = [exe]
    if threads:
        args.append("-d")
    if memory:
        args.append("-m")
    if extended:
        args.append("-x")
    if tree:
        args.append("-t")
    if nobanner:
        args.append("-nobanner")
    args.append(target)
    _append_auth(args, user, password)
    if pid is not None:
        args.append(str(int(pid)))
    return args


def build_pskill_argv(
    exe: str,
    host: str,
    pid: int,
    *,
    tree: bool = False,
    nobanner: bool = True,
    user: str = "",
    password: str = "",
) -> List[str]:
    """
    Monta argv do PsKill.

    Uso oficial::
        pskill [-t] [-nobanner] [\\\\computer [-u user [-p pass]]] <pid>
    """
    target = build_remote_target(host)
    if not exe or not target or pid is None:
        return []
    args = [exe]
    if tree:
        args.append("-t")
    if nobanner:
        args.append("-nobanner")
    args.append(target)
    _append_auth(args, user, password)
    args.append(str(int(pid)))
    return args


def build_pssuspend_argv(
    exe: str,
    host: str,
    pid: int,
    *,
    resume: bool = False,
    nobanner: bool = True,
    user: str = "",
    password: str = "",
) -> List[str]:
    """
    Monta argv do PsSuspend.

    Uso oficial (v1.08)::
        pssuspend [-r] [-nobanner] [\\\\computer [-u user [-p pass]]] <pid>
    """
    target = build_remote_target(host)
    if not exe or not target or pid is None:
        return []
    args = [exe]
    if resume:
        args.append("-r")
    if nobanner:
        args.append("-nobanner")
    args.append(target)
    _append_auth(args, user, password)
    args.append(str(int(pid)))
    return args


def format_memory_kb(kb: Optional[int]) -> str:
    """Converte KB do PsList para texto legível (ex.: 512 KB, 2,4 MB, 1,4 GB)."""
    if kb is None:
        return "—"
    try:
        value = int(kb)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    if value < 1024:
        return f"{value} KB"
    mb = value / 1024.0
    if mb < 1024:
        return _format_decimal(mb) + " MB"
    gb = mb / 1024.0
    return _format_decimal(gb) + " GB"


def _format_decimal(n: float) -> str:
    text = f"{n:.1f}".replace(".", ",")
    if text.endswith(",0"):
        text = text[:-2]
    return text


def is_pslist_usage_text(text: str) -> bool:
    t = (text or "").lower()
    return "usage: pslist" in t or ("pslist shows" in t and "-nobanner" in t)


def is_pskill_usage_text(text: str) -> bool:
    t = (text or "").lower()
    return "usage: pskill" in t


def is_pssuspend_usage_text(text: str) -> bool:
    t = (text or "").lower()
    return "usage: pssuspend" in t


_TIME_RE = r"\d+:\d{2}:\d{2}\.\d+"

_LIST_LINE_RE = re.compile(
    rf"^(?P<name>.+?)\s+"
    rf"(?P<pid>\d+)\s+"
    rf"(?P<pri>-?\d+)\s+"
    rf"(?P<thrd>\d+)\s+"
    rf"(?P<hnd>\d+)\s+"
    rf"(?P<priv>\d+)\s+"
    rf"(?P<cpu>{_TIME_RE})\s+"
    rf"(?P<elapsed>{_TIME_RE})\s*$"
)

_TREE_LINE_RE = re.compile(
    rf"^(?P<indent>\s*)(?P<name>\S.*?)\s+"
    rf"(?P<pid>\d+)\s+"
    rf"(?P<pri>-?\d+)\s+"
    rf"(?P<thrd>\d+)\s+"
    rf"(?P<hnd>\d+)\s+"
    rf"(?P<vm>\d+)\s+"
    rf"(?P<ws>\d+)\s+"
    rf"(?P<priv>\d+)\s*$"
)

_MEM_LINE_RE = re.compile(
    rf"^(?P<name>.+?)\s+"
    rf"(?P<pid>\d+)\s+"
    rf"(?P<vm>\d+)\s+"
    rf"(?P<ws>\d+)\s+"
    rf"(?P<priv>\d+)\s+"
    rf"(?P<privpk>\d+)\s+"
    rf"(?P<faults>\d+)\s+"
    rf"(?P<nonp>\d+)\s+"
    rf"(?P<page>\d+)\s*$"
)

_THREAD_LINE_RE = re.compile(
    rf"^\s*(?P<tid>\d+)\s+"
    rf"(?P<pri>-?\d+)\s+"
    rf"(?P<cswtch>\d+)\s+"
    rf"(?P<state>\S+)\s+"
    rf"(?P<user>{_TIME_RE})\s+"
    rf"(?P<kernel>{_TIME_RE})\s+"
    rf"(?P<elapsed>{_TIME_RE})\s*$"
)

_THREAD_OWNER_RE = re.compile(r"^(?P<name>.+?)\s+(?P<pid>\d+)\s*:\s*$")


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_pslist_list(text: str) -> List[ProcessRow]:
    """Parseia a saída padrão do PsList (CPU-oriented)."""
    rows: List[ProcessRow] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line or _is_noise_line(line):
            continue
        m = _LIST_LINE_RE.match(line)
        if not m:
            continue
        rows.append(
            ProcessRow(
                name=m.group("name").strip(),
                pid=int(m.group("pid")),
                pri=_to_int(m.group("pri")),
                threads=_to_int(m.group("thrd")),
                handles=_to_int(m.group("hnd")),
                priv_kb=_to_int(m.group("priv")),
                cpu_time=m.group("cpu"),
                elapsed_time=m.group("elapsed"),
            )
        )
    return rows


def parse_pslist_tree(text: str) -> Tuple[List[ProcessTreeNode], List[ProcessRow]]:
    """
    Parseia ``PsList -t``.

    Retorna (raízes da árvore, lista plana na ordem visual).
    """
    flat: List[ProcessRow] = []
    stack: List[Tuple[int, ProcessTreeNode]] = []
    roots: List[ProcessTreeNode] = []

    for raw in (text or "").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or _is_noise_line(line.strip()):
            continue
        m = _TREE_LINE_RE.match(line)
        if not m:
            continue
        indent = m.group("indent") or ""
        depth = len(indent.replace("\t", "  ")) // 2
        row = ProcessRow(
            name=m.group("name").strip(),
            pid=int(m.group("pid")),
            pri=_to_int(m.group("pri")),
            threads=_to_int(m.group("thrd")),
            handles=_to_int(m.group("hnd")),
            vm_kb=_to_int(m.group("vm")),
            ws_kb=_to_int(m.group("ws")),
            priv_kb=_to_int(m.group("priv")),
            depth=depth,
        )
        node = ProcessTreeNode(process=row)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            row.parent_pid = parent.process.pid
            parent.children.append(node)
        else:
            roots.append(node)
        stack.append((depth, node))
        flat.append(row)
    return roots, flat


def merge_ws_into_list(
    list_rows: Sequence[ProcessRow],
    tree_rows: Sequence[ProcessRow],
) -> List[ProcessRow]:
    """Copia WS/VM do modo árvore para a lista plana (por PID)."""
    by_pid: Dict[int, ProcessRow] = {r.pid: r for r in tree_rows}
    merged: List[ProcessRow] = []
    for row in list_rows:
        other = by_pid.get(row.pid)
        if other is None:
            merged.append(row)
            continue
        merged.append(
            ProcessRow(
                name=row.name,
                pid=row.pid,
                pri=row.pri if row.pri is not None else other.pri,
                threads=row.threads if row.threads is not None else other.threads,
                handles=row.handles if row.handles is not None else other.handles,
                priv_kb=row.priv_kb if row.priv_kb is not None else other.priv_kb,
                cpu_time=row.cpu_time,
                elapsed_time=row.elapsed_time,
                vm_kb=other.vm_kb,
                ws_kb=other.ws_kb,
                depth=0,
                parent_pid=other.parent_pid,
            )
        )
    return merged


def parse_pslist_memory(text: str) -> Optional[ProcessMemoryInfo]:
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line or _is_noise_line(line):
            continue
        m = _MEM_LINE_RE.match(line)
        if not m:
            continue
        return ProcessMemoryInfo(
            name=m.group("name").strip(),
            pid=int(m.group("pid")),
            vm_kb=_to_int(m.group("vm")),
            ws_kb=_to_int(m.group("ws")),
            priv_kb=_to_int(m.group("priv")),
            priv_peak_kb=_to_int(m.group("privpk")),
            faults=_to_int(m.group("faults")),
            nonpaged_kb=_to_int(m.group("nonp")),
            paged_kb=_to_int(m.group("page")),
        )
    return None


def parse_pslist_threads(text: str) -> Tuple[str, Optional[int], List[ProcessThreadRow]]:
    """Retorna (nome, pid, threads) a partir de ``PsList -d``."""
    name = ""
    pid: Optional[int] = None
    rows: List[ProcessThreadRow] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line or _is_noise_line(line):
            continue
        owner = _THREAD_OWNER_RE.match(line)
        if owner:
            name = owner.group("name").strip()
            pid = _to_int(owner.group("pid"))
            continue
        if line.lstrip().lower().startswith("tid"):
            continue
        m = _THREAD_LINE_RE.match(line)
        if not m:
            continue
        rows.append(
            ProcessThreadRow(
                tid=int(m.group("tid")),
                pri=_to_int(m.group("pri")),
                context_switches=_to_int(m.group("cswtch")),
                state=m.group("state"),
                user_time=m.group("user"),
                kernel_time=m.group("kernel"),
                elapsed_time=m.group("elapsed"),
            )
        )
    return name, pid, rows


def parse_pslist_detail(
    text: str,
    *,
    fallback: Optional[ProcessRow] = None,
) -> ProcessDetailInfo:
    """
    Parseia ``PsList -x PID`` (memória + threads).

    Campos de CPU/prioridade/handles vêm do fallback (lista) quando ausentes.
    """
    memory = parse_pslist_memory(text)
    name, pid, thread_rows = parse_pslist_threads(text)
    if memory is not None:
        name = name or memory.name
        pid = pid if pid is not None else memory.pid
    if fallback is not None:
        name = name or fallback.name
        pid = pid if pid is not None else fallback.pid
    info = ProcessDetailInfo(
        name=name or (fallback.name if fallback else ""),
        pid=int(pid if pid is not None else (fallback.pid if fallback else 0)),
        pri=fallback.pri if fallback else None,
        cpu_time=fallback.cpu_time if fallback else "",
        threads=fallback.threads if fallback else (len(thread_rows) or None),
        handles=fallback.handles if fallback else None,
        memory=memory,
        thread_rows=thread_rows,
        raw_text=text or "",
    )
    if info.threads is None and thread_rows:
        info.threads = len(thread_rows)
    return info


def _is_noise_line(line: str) -> bool:
    low = line.lower().strip()
    if not low:
        return True
    if low.startswith("process information for"):
        return True
    if low.startswith("process memory detail"):
        return True
    if low.startswith("process and thread information"):
        return True
    if low.startswith("thread detail"):
        return True
    if low.startswith("name ") or low == "name":
        return True
    if low.startswith("tid "):
        return True
    if low.startswith("pslist "):
        return True
    if "copyright" in low and "sysinternals" in low:
        return True
    return False


MEMORY_FIELD_LABELS: Dict[str, str] = {
    "vm_kb": "Virtual Memory",
    "ws_kb": "Working Set",
    "priv_kb": "Private Memory",
    "priv_peak_kb": "Private Peak",
    "faults": "Page Faults",
    "nonpaged_kb": "Non-Paged Pool",
    "paged_kb": "Paged Pool",
}

DETAIL_FIELD_ORDER: List[Tuple[str, str]] = [
    ("name", "Nome"),
    ("pid", "PID"),
    ("pri", "Prioridade"),
    ("cpu_time", "CPU Time"),
    ("threads", "Threads"),
    ("handles", "Handles"),
    ("ws_kb", "Working Set"),
    ("vm_kb", "Virtual Memory"),
    ("priv_kb", "Private Memory"),
    ("priv_peak_kb", "Private Peak"),
    ("faults", "Page Faults"),
    ("nonpaged_kb", "Non-Paged Pool"),
    ("paged_kb", "Paged Pool"),
]
