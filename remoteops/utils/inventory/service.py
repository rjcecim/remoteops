from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from remoteops.utils.inventory.cache import InventoryCache
from remoteops.utils.inventory.collectors import (
    collect_firmware,
    collect_hardware,
    collect_identity,
    collect_memory,
    collect_network,
    collect_security,
    collect_storage,
    collect_system,
    collect_updates,
    collect_video,
)
from remoteops.utils.inventory.models import (
    InventorySection,
    QueryContext,
    QueryStatus,
    SectionResult,
)
from remoteops.utils.inventory.overview import collect_overview


class InventoryService:
    """Orquestra coleta sob demanda com cache por host."""

    def __init__(
        self,
        cache: Optional[InventoryCache] = None,
        collectors: Optional[Dict[InventorySection, Callable[..., Any]]] = None,
    ) -> None:
        self.cache = cache or InventoryCache()
        self._collector_overrides = collectors or {}

    def bind_host(self, host: str) -> None:
        self.cache.set_host(host)

    def begin_query(self, host: str, section: InventorySection) -> QueryContext:
        return self.cache.begin_query(host, section)

    def collect_section(
        self,
        section: InventorySection,
        host: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        force: bool = False,
        should_abort: Optional[Callable[[], bool]] = None,
        query: Optional[QueryContext] = None,
    ) -> SectionResult:
        if query is None:
            query = self.cache.begin_query(host, section)
        elif not self.cache.is_current(query):
            return SectionResult(
                section=section,
                status=QueryStatus.ERROR,
                error="Consulta obsoleta.",
                query=query,
            )

        if not force:
            cached = self.cache.get_if_current(query)
            if cached is not None:
                return SectionResult(
                    section=section,
                    status=QueryStatus.OK,
                    payload=cached,
                    query=query,
                )

        if should_abort and should_abort():
            return SectionResult(
                section=section,
                status=QueryStatus.ERROR,
                error="Cancelado.",
                query=query,
            )

        fn = self._collector_for(section)
        if fn is None:
            return SectionResult(
                section=section,
                status=QueryStatus.ERROR,
                error="Seção desconhecida.",
                query=query,
            )

        payload = fn(
            host,
            user=user,
            password=password,
            pstools_dir=pstools_dir,
        )
        if should_abort and should_abort():
            return SectionResult(
                section=section,
                status=QueryStatus.ERROR,
                error="Cancelado.",
                query=query,
            )

        status = getattr(payload, "status", QueryStatus.OK)
        error = getattr(payload, "error", "") or ""
        if not self.cache.put_if_current(query, payload):
            return SectionResult(
                section=section,
                status=QueryStatus.ERROR,
                error="Consulta obsoleta.",
                query=query,
            )
        return SectionResult(
            section=section,
            status=status,
            error=error,
            payload=payload,
            query=query,
        )

    def _collector_for(self, section: InventorySection) -> Optional[Callable[..., Any]]:
        override = self._collector_overrides.get(section)
        if override is not None:
            return override
        collectors: Dict[InventorySection, Callable[..., Any]] = {
            InventorySection.OVERVIEW: lambda h, **kw: collect_overview(h, **kw),
            InventorySection.SYSTEM: lambda h, **kw: collect_system(h, **kw),
            InventorySection.HARDWARE: lambda h, **kw: collect_hardware(h, **kw),
            InventorySection.MEMORY: lambda h, **kw: collect_memory(h, **kw),
            InventorySection.STORAGE: lambda h, **kw: collect_storage(h, **kw),
            InventorySection.NETWORK: lambda h, **kw: collect_network(h, **kw),
            InventorySection.VIDEO: lambda h, **kw: collect_video(h, **kw),
            InventorySection.FIRMWARE: lambda h, **kw: collect_firmware(h, **kw),
            InventorySection.SECURITY: lambda h, **kw: collect_security(h, **kw),
            InventorySection.IDENTITY: lambda h, **kw: collect_identity(h, **kw),
            InventorySection.UPDATES: lambda h, **kw: collect_updates(h, **kw),
        }
        return collectors.get(section)

    def invalidate_all(self) -> None:
        self.cache.invalidate()

    def invalidate_section(self, section: InventorySection) -> None:
        self.cache.invalidate(section)
