"""Flags do winget usados no script remoto e na pré-visualização da UI."""

from __future__ import annotations

COMMON_QUERY_FLAGS = ["--accept-source-agreements", "--disable-interactivity"]

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
    "--force",
]


def flags_to_cli(flags: list[str]) -> str:
    return " ".join(flags)
