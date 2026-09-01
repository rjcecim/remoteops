"""Compatibilidade da aba Inventário (ex-PsInfo).

- **UI da aba:** ``remoteops.ui.tabs.inventario`` (``InventarioTab`` / ``PsInfoTab``)
- **Comando PsInfo:** ``remoteops.utils.psinfo`` — execução, parse, discos, hotfixes, uptime
- **Inventário completo:** ``remoteops.utils.inventory`` — WMI/PsExec por seção
"""

from remoteops.ui.tabs.inventario import InventarioTab, PsInfoTab
from remoteops.utils.psinfo import (
    build_overview_from_psinfo,
    collect_psinfo_raw,
    format_system_display,
    parse_psinfo_output,
    run_psinfo,
)

__all__ = [
    "InventarioTab",
    "PsInfoTab",
    "run_psinfo",
    "collect_psinfo_raw",
    "parse_psinfo_output",
    "format_system_display",
    "build_overview_from_psinfo",
]
