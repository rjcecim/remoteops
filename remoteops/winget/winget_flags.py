"""Flags do winget usados no script remoto e na pré-visualização da UI."""

from __future__ import annotations

COMMON_QUERY_FLAGS = ["--accept-source-agreements", "--disable-interactivity"]

# Consulta da aba Atualizações: listar upgrades, sem --all/--silent/--accept-package-agreements.
UPGRADE_QUERY_FLAGS = [
    "--source",
    "winget",
    "--accept-source-agreements",
    "--disable-interactivity",
]

# Busca da aba Busca: mesma fonte das instalações (winget), sem --silent.
SEARCH_QUERY_FLAGS = [
    "--source",
    "winget",
    "--accept-source-agreements",
    "--disable-interactivity",
]

# install / upgrade / uninstall por --id: busca exata e só a fonte winget
# (evita consulta à msstore, timeout comum sob SYSTEM).
COMMON_EXEC_FLAGS = [
    "--exact",
    "--source",
    "winget",
    "--accept-source-agreements",
    "--accept-package-agreements",
    "--disable-interactivity",
    "--silent",
]

# upgrade --all: sem --exact (não há ID); ainda fixa a fonte winget.
COMMON_UPGRADE_ALL_FLAGS = [
    "--source",
    "winget",
    "--accept-source-agreements",
    "--accept-package-agreements",
    "--disable-interactivity",
    "--silent",
]

COMMON_UNINSTALL_FLAGS = [
    "--exact",
    "--source",
    "winget",
    "--accept-source-agreements",
    "--disable-interactivity",
    "--silent",
]


def flags_to_cli(flags: list[str]) -> str:
    return " ".join(flags)


def unique_valid_ids(ids: list[str] | None) -> list[str]:
    """IDs não vazios, na ordem original, sem duplicatas."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in ids or []:
        pkg_id = str(raw or "").strip()
        if not pkg_id or pkg_id in seen:
            continue
        seen.add(pkg_id)
        out.append(pkg_id)
    return out
