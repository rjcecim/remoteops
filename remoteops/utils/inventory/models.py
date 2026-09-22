from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from remoteops.utils.psinfo import PsInfoHotfix, PsInfoResult


class InventorySection(str, Enum):
    OVERVIEW = "overview"
    SYSTEM = "system"
    HARDWARE = "hardware"
    MEMORY = "memory"
    STORAGE = "storage"
    NETWORK = "network"
    VIDEO = "video"
    FIRMWARE = "firmware"
    SECURITY = "security"
    IDENTITY = "identity"
    UPDATES = "updates"


class QueryStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    TIMEOUT = "timeout"
    NOT_QUERIED = "not_queried"


class CollectionState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    ERROR = "error"


@dataclass
class FieldValue:
    label: str
    value: str
    status: QueryStatus = QueryStatus.OK
    tooltip: str = ""


@dataclass
class SecurityItem:
    name: str
    state: str
    status: QueryStatus = QueryStatus.OK
    detail: str = ""


@dataclass
class MemoryModule:
    slot: str = ""
    capacity_gb: str = ""
    memory_type: str = ""
    speed_mhz: str = ""
    manufacturer: str = ""
    part_number: str = ""
    serial: str = ""


@dataclass
class MemorySummary:
    total_gb: str = ""
    usable_gb: str = ""
    slots_total: str = ""
    slots_used: str = ""
    slots_free: str = ""
    modules: List[MemoryModule] = field(default_factory=list)
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class PhysicalDisk:
    model: str = ""
    manufacturer: str = ""
    serial: str = ""
    firmware: str = ""
    interface: str = ""
    capacity: str = ""
    media_type: str = ""
    status: str = ""


@dataclass
class VolumeInfo:
    letter: str = ""
    label: str = ""
    filesystem: str = ""
    capacity: str = ""
    used: str = ""
    free: str = ""
    used_pct: Optional[float] = None


@dataclass
class StorageData:
    physical_disks: List[PhysicalDisk] = field(default_factory=list)
    volumes: List[VolumeInfo] = field(default_factory=list)
    psinfo_disks_raw: List[str] = field(default_factory=list)
    system_root: str = ""
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class NetworkAdapter:
    name: str = ""
    description: str = ""
    status: str = ""
    ipv4: str = ""
    ipv6: str = ""
    prefix: str = ""
    gateway: str = ""
    dns: str = ""
    mac: str = ""
    dhcp: str = ""
    link_speed: str = ""
    interface_type: str = ""


@dataclass
class NetworkData:
    adapters: List[NetworkAdapter] = field(default_factory=list)
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class VideoAdapter:
    name: str = ""
    manufacturer: str = ""
    driver: str = ""
    driver_version: str = ""
    driver_date: str = ""
    resolution: str = ""
    video_memory: str = ""


@dataclass
class MonitorInfo:
    manufacturer: str = ""
    model: str = ""
    serial: str = ""


@dataclass
class VideoData:
    adapters: List[VideoAdapter] = field(default_factory=list)
    monitors: List[MonitorInfo] = field(default_factory=list)
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class HardwareData:
    fields: Dict[str, List[FieldValue]] = field(default_factory=dict)
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class FirmwareData:
    fields: Dict[str, List[FieldValue]] = field(default_factory=dict)
    secure_boot: FieldValue = field(default_factory=lambda: FieldValue("Secure Boot", ""))
    tpm: FieldValue = field(default_factory=lambda: FieldValue("TPM", ""))
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class SecurityData:
    items: List[SecurityItem] = field(default_factory=list)
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class IdentityData:
    computer_name: str = ""
    computer_domain: str = ""
    computer_sid: str = ""
    user_name: str = ""
    user_sid: str = ""
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class UpdatesData:
    hotfixes: List[PsInfoHotfix] = field(default_factory=list)
    last_update: str = ""
    last_update_date: str = ""
    count: int = 0
    note: str = ""
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class OverviewData:
    hostname: str = ""
    manufacturer: str = ""
    model: str = ""
    os_summary: str = ""
    os_name: str = ""
    os_version: str = ""
    os_build: str = ""
    os_architecture: str = ""
    domain: str = ""
    username: str = ""
    last_boot: str = ""
    uptime: str = ""
    cpu_summary: str = ""
    cpu_detail: str = ""
    cpu_cores: str = ""
    cpu_logical: str = ""
    cpu_clock: str = ""
    memory_summary: str = ""
    memory_detail: str = ""
    storage_summary: str = ""
    storage_detail: str = ""
    storage_filesystem: str = ""
    storage_capacity: str = ""
    storage_free: str = ""
    network_summary: str = ""
    network_detail: str = ""
    network_adapter: str = ""
    network_ip: str = ""
    network_speed: str = ""
    network_gateway: str = ""
    security_summary: str = ""
    security_detail: str = ""
    updates_summary: str = ""
    updates_detail: str = ""
    collected_host: str = ""
    requested_host: str = ""
    host_mismatch: bool = False
    completeness: str = "complete"
    psinfo: Optional[PsInfoResult] = None
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass
class SystemData:
    rows: List[tuple[str, str, str]] = field(default_factory=list)
    source_note: str = ""
    psinfo: Optional[PsInfoResult] = None
    status: QueryStatus = QueryStatus.OK
    error: str = ""


@dataclass(frozen=True)
class QueryContext:
    """Identidade estável de uma consulta de inventário.

    Capturada na criação da solicitação. Não deve ser reconstruída a partir
    do host da interface quando a consulta terminar.
    """

    host: str
    section: InventorySection
    request_id: int
    generation: int

    def matches(self, other: Optional["QueryContext"]) -> bool:
        if other is None:
            return False
        return (
            self.host == other.host
            and self.section == other.section
            and self.request_id == other.request_id
            and self.generation == other.generation
        )


@dataclass
class SectionResult:
    section: InventorySection
    status: QueryStatus = QueryStatus.OK
    error: str = ""
    payload: Any = None
    query: Optional[QueryContext] = None
