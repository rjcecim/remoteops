from __future__ import annotations

from typing import Any, Dict, Optional

from remoteops.utils.inventory.models import InventorySection


class InventoryCache:
    """Cache por host — invalida ao trocar o alvo remoto."""

    def __init__(self) -> None:
        self._host: str = ""
        self._sections: Dict[InventorySection, Any] = {}

    @property
    def host(self) -> str:
        return self._host

    def set_host(self, host: str) -> None:
        h = (host or "").strip().strip("\\").lower()
        if h != self._host:
            self._host = h
            self._sections.clear()

    def get(self, section: InventorySection) -> Optional[Any]:
        return self._sections.get(section)

    def put(self, section: InventorySection, data: Any) -> None:
        self._sections[section] = data

    def invalidate(self, section: Optional[InventorySection] = None) -> None:
        if section is None:
            self._sections.clear()
        else:
            self._sections.pop(section, None)

    def has(self, section: InventorySection) -> bool:
        return section in self._sections
