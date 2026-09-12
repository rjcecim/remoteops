from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from remoteops.utils.inventory.models import InventorySection, QueryContext


def normalize_inventory_host(host: str) -> str:
    return (host or "").strip().strip("\\").lower()


class InventoryCache:
    """Cache do host corrente — gravação atômica com identidade da solicitação."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._host: str = ""
        self._generation: int = 0
        self._next_request_id: int = 0
        self._sections: Dict[InventorySection, Any] = {}
        self._accepted_request: Dict[InventorySection, int] = {}

    @property
    def host(self) -> str:
        with self._lock:
            return self._host

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def set_host(self, host: str) -> None:
        h = normalize_inventory_host(host)
        with self._lock:
            if h != self._host:
                self._host = h
                self._generation += 1
                self._sections.clear()
                self._accepted_request.clear()

    def begin_query(self, host: str, section: InventorySection) -> QueryContext:
        h = normalize_inventory_host(host)
        with self._lock:
            if h != self._host:
                self._host = h
                self._generation += 1
                self._sections.clear()
                self._accepted_request.clear()
            self._next_request_id += 1
            request_id = self._next_request_id
            self._accepted_request[section] = request_id
            return QueryContext(
                host=h,
                section=section,
                request_id=request_id,
                generation=self._generation,
            )

    def is_current(self, query: QueryContext) -> bool:
        with self._lock:
            return self._is_current_unlocked(query)

    def get(self, section: InventorySection) -> Optional[Any]:
        with self._lock:
            return self._sections.get(section)

    def get_if_current(self, query: QueryContext) -> Optional[Any]:
        with self._lock:
            if not self._is_current_unlocked(query):
                return None
            return self._sections.get(query.section)

    def put_if_current(self, query: QueryContext, data: Any) -> bool:
        """Valida e grava na mesma seção crítica — sem janela entre checagem e escrita."""
        with self._lock:
            if not self._is_current_unlocked(query):
                return False
            self._sections[query.section] = data
            return True

    def invalidate(self, section: Optional[InventorySection] = None) -> None:
        with self._lock:
            if section is None:
                self._generation += 1
                self._sections.clear()
                self._accepted_request.clear()
                return
            self._sections.pop(section, None)
            self._accepted_request.pop(section, None)

    def has(self, section: InventorySection) -> bool:
        with self._lock:
            return section in self._sections

    def _is_current_unlocked(self, query: QueryContext) -> bool:
        return (
            query.host == self._host
            and query.generation == self._generation
            and self._accepted_request.get(query.section) == query.request_id
        )
