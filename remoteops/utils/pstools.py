from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

from remoteops.utils.app_settings import KEY_PSTOOLS_DIR, load_setting, save_portable_settings

# Pasta padrão das PSTools (PsExec, PsInfo, etc.)
DEFAULT_PSTOOLS_DIR = r"C:\PSTools"
# Compat: nome histórico aponta para o padrão
PSTOOLS_DIR = DEFAULT_PSTOOLS_DIR

_runtime_dir: Optional[str] = None


def normalize_pstools_dir(path: str) -> str:
    """
    Normaliza o campo PSTools.
    Aceita pasta (C:\\PSTools\\) ou caminho antigo para um .exe (usa o diretório).
    """
    p = (path or "").strip().replace('"', "").replace("'", "")
    if not p:
        return ""
    p = os.path.normpath(p)
    if p.lower().endswith(".exe"):
        p = os.path.dirname(p)
    return p


def _load_from_settings() -> str:
    raw = load_setting(KEY_PSTOOLS_DIR, "")
    normalized = normalize_pstools_dir(str(raw or ""))
    return normalized or DEFAULT_PSTOOLS_DIR


def get_pstools_dir() -> str:
    """Pasta PSTools em uso (persistida em settings.ini; padrão C:\\PSTools)."""
    global _runtime_dir
    if _runtime_dir is None:
        _runtime_dir = _load_from_settings()
    return _runtime_dir


def set_pstools_dir(path: str, *, persist: bool = True) -> str:
    """
    Define a pasta PSTools em runtime e, se persist=True, grava o snapshot
    completo em settings.ini. Em falha, mantém o valor anterior.
    """
    global _runtime_dir
    normalized = normalize_pstools_dir(path) or DEFAULT_PSTOOLS_DIR
    if persist:
        save_portable_settings({KEY_PSTOOLS_DIR: normalized})
    _runtime_dir = normalized
    return normalized


def _bitness_of_name(name: str) -> str:
    """Classifica o executável Sysinternals como ``64`` ou ``32`` pelo nome."""
    n = (name or "").lower()
    # Ex.: PsExec64.exe, handle64.exe, psfile64.exe, handle64a.exe (ARM64)
    if "64" in os.path.splitext(n)[0]:
        return "64"
    return "32"


def _names_64_then_32(names: Sequence[str]) -> List[str]:
    """Ordena nomes: variantes 64-bit primeiro, depois 32-bit (ordem relativa preservada)."""
    cleaned = [n for n in names if n]
    sixty_four = [n for n in cleaned if _bitness_of_name(n) == "64"]
    thirty_two = [n for n in cleaned if _bitness_of_name(n) != "64"]
    return sixty_four + thirty_two


def resolve_pstools_tool(pstools_dir: str, names: Sequence[str]) -> str:
    """
    Resolve o caminho de uma ferramenta dentro da pasta.

    Prefere a variante 64-bit; se não existir, usa a 32-bit. Se nenhuma
    existir, devolve pasta+primeiro nome 64 (ou 32), para mensagem de erro.
    """
    base = normalize_pstools_dir(pstools_dir)
    ordered = _names_64_then_32(names)
    if not ordered:
        return ""
    if not base:
        return ordered[0]
    for name in ordered:
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(base, ordered[0])


def _probe_tool_variants(
    folder: str, label: str, names: Sequence[str]
) -> Dict[str, object]:
    """Inspeciona variantes 32/64 de uma ferramenta na pasta."""
    path_64 = ""
    path_32 = ""
    for name in names:
        candidate = os.path.join(folder, name)
        if not os.path.isfile(candidate):
            continue
        bit = _bitness_of_name(name)
        if bit == "64" and not path_64:
            path_64 = candidate
        elif bit == "32" and not path_32:
            path_32 = candidate
    found_64 = bool(path_64)
    found_32 = bool(path_32)
    found = found_64 or found_32
    # Preferência de execução: 64-bit, depois 32-bit
    resolved = path_64 or path_32
    if not resolved and names:
        resolved = os.path.join(folder, names[0])
    return {
        "label": label,
        "names": list(names),
        "path": resolved,
        "found": found,
        "found_64": found_64,
        "found_32": found_32,
        "path_64": path_64,
        "path_32": path_32,
    }


# Ferramentas da pasta PSTools (Handle tem pasta própria — ver handle.py).
PSTOOLS_PROBE_TOOLS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("PsExec", ("PsExec64.exe", "PsExec.exe")),
    ("PsInfo", ("PsInfo64.exe", "PsInfo.exe")),
    ("PsPing", ("PsPing64.exe", "PsPing.exe")),
    ("PsList", ("PsList64.exe", "PsList.exe")),
    ("PsKill", ("PsKill64.exe", "PsKill.exe")),
    ("PsSuspend", ("PsSuspend64.exe", "PsSuspend.exe")),
    ("PsService", ("PsService64.exe", "PsService.exe")),
    ("PsShutdown", ("PsShutdown64.exe", "PsShutdown.exe")),
    ("PsLoggedOn", ("PsLoggedon64.exe", "PsLoggedon.exe")),
    ("PsFile", ("psfile64.exe", "PsFile64.exe", "psfile.exe", "PsFile.exe")),
    ("PsGetSid", ("PsGetSid64.exe", "PsGetSid.exe")),
    ("PsPasswd", ("PsPasswd64.exe", "PsPasswd.exe")),
)


def probe_pstools(pstools_dir: Optional[str] = None) -> Dict[str, object]:
    """
    Inspeciona a pasta PSTools e retorna status dos binários principais.

    Handle NÃO fica aqui (pasta própria). A escolha 64→32 na execução
    é feita por ``resolve_pstools_tool``.
    """
    base = normalize_pstools_dir(pstools_dir or get_pstools_dir()) or DEFAULT_PSTOOLS_DIR
    dir_ok = os.path.isdir(base)
    items: List[Dict[str, object]] = []
    found_count = 0
    if dir_ok:
        for label, names in PSTOOLS_PROBE_TOOLS:
            item = _probe_tool_variants(base, label, names)
            if item.get("found"):
                found_count += 1
            items.append(item)
    else:
        # Pasta ausente: ainda devolve a lista esperada (found=False) para UI/API.
        for label, names in PSTOOLS_PROBE_TOOLS:
            items.append(
                {
                    "label": label,
                    "names": list(names),
                    "path": os.path.join(base, names[0]) if names else "",
                    "found": False,
                    "found_64": False,
                    "found_32": False,
                    "path_64": "",
                    "path_32": "",
                }
            )

    psexec_ok = bool(items and items[0].get("found"))
    psinfo_ok = bool(len(items) > 1 and items[1].get("found"))
    return {
        "dir": base,
        "dir_ok": dir_ok,
        "tools": items,
        "ok_count": found_count,
        "total": len(items),
        # Saudável enquanto PsExec e PsInfo existirem; demais são opcionais.
        "healthy": dir_ok and psexec_ok and psinfo_ok,
    }


def rustdesk_local_candidates() -> List[str]:
    """Caminhos locais usuais do RustDesk (instalação em Program Files)."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return [
        os.path.join(pf, "RustDesk", "rustdesk.exe"),
        os.path.join(pfx86, "RustDesk", "rustdesk.exe"),
    ]


def probe_rustdesk_local() -> Dict[str, object]:
    """Status do RustDesk instalado localmente (não usa a pasta PSTools)."""
    candidates = rustdesk_local_candidates()
    for path in candidates:
        if os.path.isfile(path):
            return {
                "found": True,
                "path": path,
                "candidates": candidates,
            }
    return {
        "found": False,
        "path": candidates[0] if candidates else r"C:\Program Files\RustDesk\rustdesk.exe",
        "candidates": candidates,
    }
