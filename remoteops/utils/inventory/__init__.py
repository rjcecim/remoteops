"""Inventário remoto — coleta estruturada via PsInfo, PsExec/PowerShell e PsGetSid."""

from remoteops.utils.inventory.models import InventorySection, QueryStatus
from remoteops.utils.inventory.service import InventoryService

__all__ = ["InventorySection", "InventoryService", "QueryStatus"]
