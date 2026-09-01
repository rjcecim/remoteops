from __future__ import annotations

from typing import Any, Callable, Optional

from remoteops.utils.inventory.cache import InventoryCache
from remoteops.utils.inventory.collectors import (
    _SCRIPT_OVERVIEW_ENRICH,
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
    OverviewData,
    QueryStatus,
    SectionResult,
)
from remoteops.utils.inventory.remote_exec import run_remote_powershell
from remoteops.utils.psinfo import (
    build_overview_from_psinfo,
    collect_psinfo_raw,
    parse_psinfo_output,
)


class InventoryService:
    """Orquestra coleta sob demanda com cache por host."""

    def __init__(self, cache: Optional[InventoryCache] = None) -> None:
        self.cache = cache or InventoryCache()

    def bind_host(self, host: str) -> None:
        self.cache.set_host(host)

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
    ) -> SectionResult:
        self.bind_host(host)
        if not force:
            cached = self.cache.get(section)
            if cached is not None:
                return SectionResult(section=section, status=QueryStatus.OK, payload=cached)

        if should_abort and should_abort():
            return SectionResult(section=section, status=QueryStatus.ERROR, error="Cancelado.")

        collectors = {
            InventorySection.OVERVIEW: self._collect_overview,
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
        fn = collectors.get(section)
        if fn is None:
            return SectionResult(section=section, status=QueryStatus.ERROR, error="Seção desconhecida.")

        payload = fn(
            host,
            user=user,
            password=password,
            pstools_dir=pstools_dir,
        )
        status = getattr(payload, "status", QueryStatus.OK)
        error = getattr(payload, "error", "") or ""
        self.cache.put(section, payload)
        return SectionResult(section=section, status=status, error=error, payload=payload)

    def _collect_overview(
        self,
        host: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
    ) -> OverviewData:
        stdout, err = collect_psinfo_raw(
            host,
            include_disks=True,
            include_hotfixes=False,
            user=user,
            password=password,
            pstools_dir=pstools_dir,
        )
        if not stdout:
            return OverviewData(status=QueryStatus.ERROR, error=err or "PsInfo sem dados.")

        parsed = parse_psinfo_output(stdout, host=host)
        enrich, _ = run_remote_powershell(
            host, _SCRIPT_OVERVIEW_ENRICH, user=user, password=password, pstools_dir=pstools_dir
        )
        summary = build_overview_from_psinfo(parsed, host, enrich=enrich if isinstance(enrich, dict) else None)

        overview = OverviewData(
            hostname=summary["hostname"],
            manufacturer=summary["manufacturer"],
            model=summary["model"],
            os_summary=summary["os_summary"],
            domain=summary["domain"],
            uptime=summary["uptime"],
            cpu_summary=summary["cpu_summary"],
            cpu_detail=summary["cpu_detail"],
            memory_summary=summary["memory_summary"],
            memory_detail=summary["memory_detail"],
            storage_summary=summary["storage_summary"],
            storage_detail=summary["storage_detail"],
            network_summary=summary["network_summary"],
            network_detail=summary["network_detail"],
            security_summary=summary["security_summary"],
            security_detail=summary["security_detail"],
            updates_summary=summary["updates_summary"],
            updates_detail=summary["updates_detail"],
            psinfo=summary["psinfo"],
            status=QueryStatus.OK,
        )
        return overview

    def invalidate_all(self) -> None:
        self.cache.invalidate()

    def invalidate_section(self, section: InventorySection) -> None:
        self.cache.invalidate(section)
