from __future__ import annotations

from typing import Any, Dict, List, Optional

from datetime import datetime

from remoteops.utils.dates import parse_any_date, to_display_date
from remoteops.utils.inventory.formatters import (
    _EMPTY,
    architecture_label,
    bytes_to_gb,
    format_link_speed_bps,
    format_mhz,
    format_tpm_version,
    normalize_wmi_date,
    safe_str,
    smbios_memory_type,
    volume_used_pct,
)
from remoteops.utils.inventory.models import (
    FieldValue,
    FirmwareData,
    HardwareData,
    IdentityData,
    MemoryModule,
    MemorySummary,
    NetworkAdapter,
    NetworkData,
    PhysicalDisk,
    QueryStatus,
    SecurityData,
    SecurityItem,
    StorageData,
    SystemData,
    UpdatesData,
    VideoAdapter,
    VideoData,
    VolumeInfo,
)
from remoteops.utils.inventory.remote_exec import run_remote_powershell
from remoteops.utils.inventory.psget_sid import run_psgetsid
from remoteops.utils.psinfo import (
    PsInfoHotfix,
    collect_psinfo_raw,
    extract_psinfo_host,
    format_system_display,
    list_remote_hotfixes,
    parse_disks_table,
    parse_psinfo_output,
    patch_system_uptime,
    prepare_disks_for_display,
)

import os


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _field(label: str, value: Any, *, status: QueryStatus = QueryStatus.OK) -> FieldValue:
    return FieldValue(label, safe_str(value), status=status)


def collect_system(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> SystemData:
    stdout, err = collect_psinfo_raw(
        host,
        include_disks=False,
        include_hotfixes=False,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    if not stdout:
        return SystemData(status=QueryStatus.ERROR, error=err or "PsInfo sem dados.")
    parsed = parse_psinfo_output(stdout, host=host)
    patch_system_uptime(
        parsed.system,
        host,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    info_host = extract_psinfo_host(parsed) or host
    rows = format_system_display(
        parsed.system,
        host=info_host,
        tool_version=parsed.tool_version,
    )
    return SystemData(rows=rows, psinfo=parsed, status=QueryStatus.OK)


# ── Scripts PowerShell (executados LOCALMENTE no remoto via PsExec) ─────────

_SCRIPT_HARDWARE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$result = @{
  ComputerSystem = Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 Manufacturer, Model, Name, Domain, TotalPhysicalMemory, NumberOfProcessors, SystemType, UserName, PCSystemType
  ComputerSystemProduct = Get-CimInstance Win32_ComputerSystemProduct | Select-Object -First 1 IdentifyingNumber, UUID, Vendor, Name, Version
  Processors = @(Get-CimInstance Win32_Processor | Select-Object Name, Manufacturer, SocketDesignation, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed, CurrentClockSpeed, Architecture, ProcessorId)
  BaseBoard = Get-CimInstance Win32_BaseBoard | Select-Object -First 1 Manufacturer, Product, SerialNumber, Version
}
$result | ConvertTo-Json -Depth 6 -Compress
"""

_SCRIPT_MEMORY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$result = @{
  Arrays = @(Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MemoryDevices, MaxCapacity)
  Modules = @(Get-CimInstance Win32_PhysicalMemory | Select-Object DeviceLocator, BankLabel, Capacity, SMBIOSMemoryType, Speed, ConfiguredClockSpeed, Manufacturer, PartNumber, SerialNumber)
}
$result | ConvertTo-Json -Depth 6 -Compress
"""

_SCRIPT_STORAGE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$result = @{ Disks = @(); PhysicalDisks = @(); Volumes = @() }
$result.Disks = @(Get-CimInstance Win32_DiskDrive | Select-Object Model, Manufacturer, SerialNumber, FirmwareRevision, InterfaceType, Size, MediaType, Status)
if (Get-Command Get-PhysicalDisk -ErrorAction SilentlyContinue) {
  $result.PhysicalDisks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object FriendlyName, MediaType, Size, SerialNumber, FirmwareVersion, HealthStatus, BusType)
}
if (Get-Command Get-Volume -ErrorAction SilentlyContinue) {
  $result.Volumes = @(Get-Volume -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter -or $_.FileSystemLabel } | Select-Object DriveLetter, FileSystemLabel, FileSystem, Size, SizeRemaining)
}
$result | ConvertTo-Json -Depth 6 -Compress
"""

_SCRIPT_NETWORK = r"""
$ErrorActionPreference = 'SilentlyContinue'
$result = @()
Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object {
  $a = $_
  $cfg = Get-NetIPConfiguration -InterfaceAlias $a.Name -ErrorAction SilentlyContinue
  $dns = (Get-DnsClientServerAddress -InterfaceAlias $a.Name -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses -join ', '
  $ipv4 = @($cfg.IPv4Address.IPAddress) -join ', '
  $ipv6 = @($cfg.IPv6Address.IPAddress) -join ', '
  $prefix = @($cfg.IPv4Address.PrefixLength) -join ', '
  $gw = @($cfg.IPv4DefaultGateway.NextHop) -join ', '
  $dhcp = if ($cfg.IPv4Address) { [string]$cfg.IPv4Address[0].PrefixOrigin } else { '' }
  $result += [PSCustomObject]@{
    Name = $a.Name; Description = $a.InterfaceDescription; Status = $a.Status
    MacAddress = $a.MacAddress; LinkSpeed = $a.LinkSpeed; InterfaceType = $a.InterfaceType
    IPv4 = $ipv4; IPv6 = $ipv6; Prefix = $prefix; Gateway = $gw; DNS = $dns; DHCP = $dhcp
  }
}
$result | ConvertTo-Json -Depth 5 -Compress
"""

_SCRIPT_VIDEO = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-CimInstance Win32_VideoController | Select-Object Name, AdapterCompatibility, DriverVersion, DriverDate, CurrentHorizontalResolution, CurrentVerticalResolution, AdapterRAM, VideoProcessor | ConvertTo-Json -Depth 4 -Compress
"""

_SCRIPT_FIRMWARE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$result = @{ BIOS = $null; SecureBoot = $null; TPM = $null }
$result.BIOS = Get-CimInstance Win32_BIOS | Select-Object -First 1 Manufacturer, SMBIOSBIOSVersion, ReleaseDate, SerialNumber, BIOSVersion
try { $result.SecureBoot = Confirm-SecureBootUEFI } catch { $result.SecureBoot = $null }
try { $result.TPM = Get-Tpm -ErrorAction SilentlyContinue | Select-Object TpmPresent, TpmReady, ManufacturerIdTxt, ManufacturerVersion, SpecVersion } catch { $result.TPM = $null }
$result | ConvertTo-Json -Depth 5 -Compress
"""

_SCRIPT_SECURITY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$result = @{}
try {
  $result.BitLocker = @(Get-BitLockerVolume -ErrorAction SilentlyContinue | Select-Object MountPoint, VolumeStatus, ProtectionStatus)
} catch { $result.BitLocker = $null }
try {
  $result.Defender = Get-MpComputerStatus -ErrorAction SilentlyContinue | Select-Object AntivirusEnabled, RealTimeProtectionEnabled, AMServiceEnabled, AntivirusSignatureLastUpdated
} catch { $result.Defender = $null }
try {
  $result.Firewall = @(Get-NetFirewallProfile -ErrorAction SilentlyContinue | Select-Object Name, Enabled)
} catch { $result.Firewall = $null }
try {
  $result.UAC = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name EnableLUA -ErrorAction SilentlyContinue).EnableLUA
} catch { $result.UAC = $null }
try { $result.SecureBoot = Confirm-SecureBootUEFI } catch { $result.SecureBoot = $null }
try {
  $result.TPM = Get-Tpm -ErrorAction SilentlyContinue | Select-Object TpmPresent, TpmReady, ManufacturerVersion, SpecVersion
} catch { $result.TPM = $null }
$result | ConvertTo-Json -Depth 6 -Compress
"""


def collect_hardware(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> HardwareData:
    data, err = run_remote_powershell(
        host, _SCRIPT_HARDWARE, user=user, password=password, pstools_dir=pstools_dir
    )
    if data is None:
        return HardwareData(status=QueryStatus.ERROR, error=err)

    cs = data.get("ComputerSystem") if isinstance(data, dict) else None
    csp = data.get("ComputerSystemProduct") if isinstance(data, dict) else None
    procs = _as_list(data.get("Processors") if isinstance(data, dict) else None)
    bb = data.get("BaseBoard") if isinstance(data, dict) else None

    pc_type = ""
    if isinstance(cs, dict):
        try:
            t = int(cs.get("PCSystemType") or 0)
            pc_type = {1: "Desktop", 2: "Mobile/Laptop", 3: "Workstation", 4: "Enterprise Server", 5: "SOHO Server"}.get(t, "")
        except (TypeError, ValueError):
            pc_type = ""

    fields: Dict[str, List[FieldValue]] = {
        "Computador": [
            _field("Fabricante", cs.get("Manufacturer") if isinstance(cs, dict) else ""),
            _field("Modelo", cs.get("Model") if isinstance(cs, dict) else ""),
            _field("Nome", cs.get("Name") if isinstance(cs, dict) else ""),
            _field("Número de série", csp.get("IdentifyingNumber") if isinstance(csp, dict) else ""),
            _field("UUID", csp.get("UUID") if isinstance(csp, dict) else ""),
            _field("Tipo", pc_type or safe_str(csp.get("Name") if isinstance(csp, dict) else "")),
        ],
        "Placa-mãe": [
            _field("Fabricante", bb.get("Manufacturer") if isinstance(bb, dict) else ""),
            _field("Produto", bb.get("Product") if isinstance(bb, dict) else ""),
            _field("Número de série", bb.get("SerialNumber") if isinstance(bb, dict) else ""),
            _field("Versão", bb.get("Version") if isinstance(bb, dict) else ""),
        ],
    }

    proc_fields: List[FieldValue] = []
    if procs:
        p0 = procs[0] if isinstance(procs[0], dict) else {}
        proc_fields = [
            _field("Modelo", p0.get("Name")),
            _field("Fabricante", p0.get("Manufacturer")),
            _field("Sockets", len({safe_str(p.get("SocketDesignation"), default="") for p in procs if isinstance(p, dict)})),
            _field("Núcleos", sum(int(p.get("NumberOfCores") or 0) for p in procs if isinstance(p, dict))),
            _field("Processadores lógicos", sum(int(p.get("NumberOfLogicalProcessors") or 0) for p in procs if isinstance(p, dict))),
            _field("Clock atual", format_mhz(p0.get("CurrentClockSpeed"))),
            _field("Clock máximo", format_mhz(p0.get("MaxClockSpeed"))),
            _field("Arquitetura", architecture_label(p0.get("Architecture"))),
        ]
        if len(procs) > 1:
            proc_fields.append(_field("Observação", f"{len(procs)} processadores detectados"))
    fields["Processador"] = proc_fields

    return HardwareData(fields=fields, status=QueryStatus.OK)


def collect_memory(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> MemorySummary:
    data, err = run_remote_powershell(
        host, _SCRIPT_MEMORY, user=user, password=password, pstools_dir=pstools_dir
    )
    if data is None:
        return MemorySummary(status=QueryStatus.ERROR, error=err)

    arrays = _as_list(data.get("Arrays") if isinstance(data, dict) else None)
    modules_raw = _as_list(data.get("Modules") if isinstance(data, dict) else None)

    slots_total = 0
    for arr in arrays:
        if isinstance(arr, dict):
            try:
                slots_total += int(arr.get("MemoryDevices") or 0)
            except (TypeError, ValueError):
                pass

    modules: List[MemoryModule] = []
    total_bytes = 0
    for mod in modules_raw:
        if not isinstance(mod, dict):
            continue
        cap = 0
        try:
            cap = int(mod.get("Capacity") or 0)
        except (TypeError, ValueError):
            pass
        total_bytes += cap
        speed = mod.get("ConfiguredClockSpeed") or mod.get("Speed")
        modules.append(
            MemoryModule(
                slot=safe_str(mod.get("DeviceLocator") or mod.get("BankLabel")),
                capacity_gb=bytes_to_gb(cap),
                memory_type=smbios_memory_type(mod.get("SMBIOSMemoryType")),
                speed_mhz=format_mhz(speed).replace(" MHz", ""),
                manufacturer=safe_str(mod.get("Manufacturer")),
                part_number=safe_str(mod.get("PartNumber")),
                serial=safe_str(mod.get("SerialNumber")),
            )
        )

    used_slots = len(modules)
    free_slots = max(0, slots_total - used_slots) if slots_total else 0

    return MemorySummary(
        total_gb=bytes_to_gb(total_bytes),
        usable_gb=bytes_to_gb(total_bytes),
        slots_total=str(slots_total) if slots_total else _EMPTY,
        slots_used=str(used_slots),
        slots_free=str(free_slots) if slots_total else _EMPTY,
        modules=modules,
        status=QueryStatus.OK,
    )


def collect_storage(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> StorageData:
    stdout, psinfo_err = collect_psinfo_raw(
        host,
        include_disks=True,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    psinfo = parse_psinfo_output(stdout, host=host) if stdout else None
    system_root = psinfo.system.get("System root", "") if psinfo else ""

    data, err = run_remote_powershell(
        host, _SCRIPT_STORAGE, user=user, password=password, pstools_dir=pstools_dir
    )

    physical: List[PhysicalDisk] = []
    volumes: List[VolumeInfo] = []

    if isinstance(data, dict):
        for disk in _as_list(data.get("PhysicalDisks")):
            if not isinstance(disk, dict):
                continue
            size = 0
            try:
                size = int(disk.get("Size") or 0)
            except (TypeError, ValueError):
                pass
            physical.append(
                PhysicalDisk(
                    model=safe_str(disk.get("FriendlyName")),
                    manufacturer="",
                    serial=safe_str(disk.get("SerialNumber")),
                    firmware=safe_str(disk.get("FirmwareVersion")),
                    interface=safe_str(disk.get("BusType")),
                    capacity=bytes_to_gb(size),
                    media_type=safe_str(disk.get("MediaType")),
                    status=safe_str(disk.get("HealthStatus")),
                )
            )
        if not physical:
            for disk in _as_list(data.get("Disks")):
                if not isinstance(disk, dict):
                    continue
                size = 0
                try:
                    size = int(disk.get("Size") or 0)
                except (TypeError, ValueError):
                    pass
                physical.append(
                    PhysicalDisk(
                        model=safe_str(disk.get("Model")),
                        manufacturer=safe_str(disk.get("Manufacturer")),
                        serial=safe_str(disk.get("SerialNumber")),
                        firmware=safe_str(disk.get("FirmwareRevision")),
                        interface=safe_str(disk.get("InterfaceType")),
                        capacity=bytes_to_gb(size),
                        media_type=safe_str(disk.get("MediaType")),
                        status=safe_str(disk.get("Status")),
                    )
                )
        for vol in _as_list(data.get("Volumes")):
            if not isinstance(vol, dict):
                continue
            size = 0
            free = 0
            try:
                size = int(vol.get("Size") or 0)
                free = int(vol.get("SizeRemaining") or 0)
            except (TypeError, ValueError):
                pass
            used = max(0, size - free) if size else 0
            letter = vol.get("DriveLetter")
            letter_s = f"{letter}:" if letter else _EMPTY
            volumes.append(
                VolumeInfo(
                    letter=letter_s,
                    label=safe_str(vol.get("FileSystemLabel")),
                    filesystem=safe_str(vol.get("FileSystem")),
                    capacity=bytes_to_gb(size),
                    used=bytes_to_gb(used),
                    free=bytes_to_gb(free),
                    used_pct=volume_used_pct(size, free),
                )
            )

    # Complemento PsInfo para volumes
    if psinfo and psinfo.disks_raw:
        parsed_rows = parse_disks_table(psinfo.disks_raw)
        rows, _, _ = prepare_disks_for_display(parsed_rows, system_root=system_root)
        if not volumes:
            for row in rows:
                if (row.volume or "").lower() == "total":
                    continue
                size_b = row.size_bytes or 0
                free_b = row.free_bytes or 0
                used_b = row.used_bytes or max(0, size_b - free_b)
                volumes.append(
                    VolumeInfo(
                        letter=row.volume,
                        label=row.label,
                        filesystem=row.format,
                        capacity=row.size or bytes_to_gb(size_b),
                        used=row.used or bytes_to_gb(used_b),
                        free=row.free or bytes_to_gb(free_b),
                        used_pct=volume_used_pct(size_b, free_b) if size_b else None,
                    )
                )

    status = QueryStatus.OK
    error = ""
    if not physical and not volumes and not stdout:
        status = QueryStatus.ERROR
        error = err or psinfo_err or "Não foi possível consultar armazenamento."
    elif err and not physical and not volumes:
        error = err

    return StorageData(
        physical_disks=physical,
        volumes=volumes,
        psinfo_disks_raw=psinfo.disks_raw if psinfo else [],
        system_root=system_root,
        status=status,
        error=error,
    )


def collect_network(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> NetworkData:
    data, err = run_remote_powershell(
        host, _SCRIPT_NETWORK, user=user, password=password, pstools_dir=pstools_dir
    )
    if data is None:
        return NetworkData(status=QueryStatus.ERROR, error=err)

    adapters: List[NetworkAdapter] = []
    for row in _as_list(data):
        if not isinstance(row, dict):
            continue
        adapters.append(
            NetworkAdapter(
                name=safe_str(row.get("Name")),
                description=safe_str(row.get("Description")),
                status=safe_str(row.get("Status")),
                ipv4=safe_str(row.get("IPv4")),
                ipv6=safe_str(row.get("IPv6")),
                prefix=safe_str(row.get("Prefix")),
                gateway=safe_str(row.get("Gateway")),
                dns=safe_str(row.get("DNS")),
                mac=safe_str(row.get("MacAddress")),
                dhcp=safe_str(row.get("DHCP")),
                link_speed=format_link_speed_bps(row.get("LinkSpeed")),
                interface_type=safe_str(row.get("InterfaceType")),
            )
        )
    return NetworkData(adapters=adapters, status=QueryStatus.OK)


def collect_video(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> VideoData:
    data, err = run_remote_powershell(
        host, _SCRIPT_VIDEO, user=user, password=password, pstools_dir=pstools_dir
    )
    if data is None:
        return VideoData(status=QueryStatus.ERROR, error=err)

    adapters: List[VideoAdapter] = []
    for row in _as_list(data):
        if not isinstance(row, dict):
            continue
        hres = row.get("CurrentHorizontalResolution")
        vres = row.get("CurrentVerticalResolution")
        res = ""
        if hres and vres:
            res = f"{hres} × {vres}"
        vram = ""
        try:
            ram = int(row.get("AdapterRAM") or 0)
            if ram > 0:
                vram = bytes_to_gb(ram)
        except (TypeError, ValueError):
            vram = safe_str(row.get("AdapterRAM"))
        adapters.append(
            VideoAdapter(
                name=safe_str(row.get("Name")),
                manufacturer=safe_str(row.get("AdapterCompatibility")),
                driver=safe_str(row.get("VideoProcessor")),
                driver_version=safe_str(row.get("DriverVersion")),
                driver_date=normalize_wmi_date(row.get("DriverDate")),
                resolution=res,
                video_memory=vram,
            )
        )
    return VideoData(adapters=adapters, status=QueryStatus.OK)


def collect_firmware(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> FirmwareData:
    data, err = run_remote_powershell(
        host, _SCRIPT_FIRMWARE, user=user, password=password, pstools_dir=pstools_dir
    )
    if data is None:
        return FirmwareData(status=QueryStatus.ERROR, error=err)

    bios = data.get("BIOS") if isinstance(data, dict) else None
    fields: Dict[str, List[FieldValue]] = {
        "BIOS": [
            _field("Fabricante", bios.get("Manufacturer") if isinstance(bios, dict) else ""),
            _field("Versão", bios.get("SMBIOSBIOSVersion") if isinstance(bios, dict) else ""),
            _field("Data", normalize_wmi_date(bios.get("ReleaseDate") if isinstance(bios, dict) else "")),
            _field("Serial", bios.get("SerialNumber") if isinstance(bios, dict) else ""),
        ],
    }

    sb = data.get("SecureBoot") if isinstance(data, dict) else None
    if sb is None:
        secure = FieldValue("Secure Boot", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE)
    elif sb is True:
        secure = FieldValue("Secure Boot", "Ativado", status=QueryStatus.OK)
    elif sb is False:
        secure = FieldValue("Secure Boot", "Desativado", status=QueryStatus.OK)
    else:
        secure = FieldValue("Secure Boot", safe_str(sb))

    tpm_raw = data.get("TPM") if isinstance(data, dict) else None
    if tpm_raw is None:
        tpm = FieldValue("TPM", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE)
    elif isinstance(tpm_raw, dict):
        if tpm_raw.get("TpmPresent"):
            ver = format_tpm_version(
                tpm_raw.get("SpecVersion"),
                tpm_raw.get("ManufacturerVersion"),
            ) or "Presente"
            tpm = FieldValue("TPM", ver, status=QueryStatus.OK)
        else:
            tpm = FieldValue("TPM", "Indisponível", status=QueryStatus.OK)
    else:
        tpm = FieldValue("TPM", safe_str(tpm_raw))

    return FirmwareData(fields=fields, secure_boot=secure, tpm=tpm, status=QueryStatus.OK)


def _security_state(enabled: Any, *, true_label: str = "Ativado", false_label: str = "Desativado") -> SecurityItem:
    if enabled is None:
        return SecurityItem("", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE)
    if isinstance(enabled, bool):
        return SecurityItem("", true_label if enabled else false_label, status=QueryStatus.OK)
    try:
        n = int(enabled)
        return SecurityItem("", true_label if n else false_label, status=QueryStatus.OK)
    except (TypeError, ValueError):
        return SecurityItem("", safe_str(enabled))


def collect_security(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> SecurityData:
    data, err = run_remote_powershell(
        host, _SCRIPT_SECURITY, user=user, password=password, pstools_dir=pstools_dir
    )
    if data is None:
        return SecurityData(status=QueryStatus.ERROR, error=err)

    items: List[SecurityItem] = []

    bl = data.get("BitLocker") if isinstance(data, dict) else None
    if bl is None:
        items.append(SecurityItem("BitLocker", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))
    else:
        rows = _as_list(bl)
        active = any(
            isinstance(r, dict) and str(r.get("ProtectionStatus", "")).lower() in ("1", "on", "true")
            for r in rows
        )
        if not rows:
            items.append(SecurityItem("BitLocker", "Sem volumes BitLocker", status=QueryStatus.OK))
        else:
            items.append(SecurityItem("BitLocker", "Ativado" if active else "Desativado", status=QueryStatus.OK))

    defender = data.get("Defender") if isinstance(data, dict) else None
    if defender is None:
        items.append(SecurityItem("Microsoft Defender", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))
    elif isinstance(defender, dict):
        av = defender.get("AntivirusEnabled")
        rt = defender.get("RealTimeProtectionEnabled")
        if av is True or rt is True:
            items.append(SecurityItem("Microsoft Defender", "Ativo", status=QueryStatus.OK))
        elif av is False and rt is False:
            items.append(SecurityItem("Microsoft Defender", "Desativado", status=QueryStatus.OK))
        else:
            items.append(SecurityItem("Microsoft Defender", "Parcial / indeterminado", status=QueryStatus.UNAVAILABLE))
    else:
        items.append(SecurityItem("Microsoft Defender", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))

    fw = data.get("Firewall") if isinstance(data, dict) else None
    if fw is None:
        items.append(SecurityItem("Firewall", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))
    else:
        profiles = _as_list(fw)
        enabled = [p for p in profiles if isinstance(p, dict) and p.get("Enabled")]
        if enabled and len(enabled) == len(profiles):
            items.append(SecurityItem("Firewall", "Ativado", status=QueryStatus.OK))
        elif enabled:
            items.append(SecurityItem("Firewall", f"Parcial ({len(enabled)}/{len(profiles)})", status=QueryStatus.OK))
        elif profiles:
            items.append(SecurityItem("Firewall", "Desativado", status=QueryStatus.OK))
        else:
            items.append(SecurityItem("Firewall", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))

    uac = data.get("UAC") if isinstance(data, dict) else None
    uac_item = _security_state(uac)
    uac_item.name = "UAC"
    items.append(uac_item)

    sb = data.get("SecureBoot") if isinstance(data, dict) else None
    if sb is None:
        items.append(SecurityItem("Secure Boot", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))
    elif sb is True:
        items.append(SecurityItem("Secure Boot", "Ativado", status=QueryStatus.OK))
    elif sb is False:
        items.append(SecurityItem("Secure Boot", "Desativado", status=QueryStatus.OK))
    else:
        items.append(SecurityItem("Secure Boot", safe_str(sb), status=QueryStatus.UNAVAILABLE))

    tpm_raw = data.get("TPM") if isinstance(data, dict) else None
    if tpm_raw is None:
        items.append(SecurityItem("TPM", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))
    elif isinstance(tpm_raw, dict) and tpm_raw.get("TpmPresent"):
        ver = format_tpm_version(
            tpm_raw.get("SpecVersion"),
            tpm_raw.get("ManufacturerVersion"),
        ) or "Presente"
        items.append(SecurityItem("TPM", ver, status=QueryStatus.OK))
    elif isinstance(tpm_raw, dict):
        items.append(SecurityItem("TPM", "Indisponível", status=QueryStatus.OK))
    else:
        items.append(SecurityItem("TPM", "Não foi possível consultar", status=QueryStatus.UNAVAILABLE))

    return SecurityData(items=items, status=QueryStatus.OK)


_SCRIPT_IDENTITY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cs = Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 Name, Domain, UserName
@{ Name = $cs.Name; Domain = $cs.Domain; UserName = $cs.UserName } | ConvertTo-Json -Compress
"""


def _split_account(account: str) -> tuple[str, str]:
    text = (account or "").strip()
    if "\\" in text:
        domain, name = text.split("\\", 1)
        return domain.strip(), name.strip()
    return "", text


def _looks_like_computer_account(name: str, host: str) -> bool:
    n = (name or "").strip().strip("\\").rstrip("$").lower()
    if "\\" in n:
        n = n.rsplit("\\", 1)[-1]
    h = (host or "").strip().strip("\\").lower()
    return bool(n) and n == h


def collect_identity(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> IdentityData:
    h = (host or "").strip().strip("\\")
    enrich, _ = run_remote_powershell(
        h, _SCRIPT_IDENTITY, user=user, password=password, pstools_dir=pstools_dir
    )
    wmi_name = ""
    wmi_domain = ""
    wmi_user = ""
    if isinstance(enrich, dict):
        wmi_name = safe_str(enrich.get("Name"), default="")
        wmi_domain = safe_str(enrich.get("Domain"), default="")
        wmi_user = safe_str(enrich.get("UserName"), default="")
        if wmi_name == _EMPTY:
            wmi_name = ""
        if wmi_domain == _EMPTY:
            wmi_domain = ""
        if wmi_user == _EMPTY:
            wmi_user = ""

    query_name = (wmi_name or h).rstrip("$")
    comp_account, comp_sid, comp_err = run_psgetsid(
        h,
        account=f"{query_name}$",
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    parsed_domain, parsed_comp = _split_account(comp_account)
    computer_name = (parsed_comp or wmi_name or h).rstrip("$")
    computer_domain = parsed_domain or wmi_domain

    user_name = ""
    user_sid = ""
    user_err = ""
    if wmi_user and not _looks_like_computer_account(wmi_user, query_name):
        user_account, user_sid, user_err = run_psgetsid(
            h,
            account=wmi_user,
            user=user,
            password=password,
            pstools_dir=pstools_dir,
        )
        user_name = user_account or wmi_user

    status = QueryStatus.OK
    error = ""
    if not comp_sid and not user_sid:
        status = QueryStatus.ERROR
        error = comp_err or user_err or "Não foi possível obter SIDs."

    return IdentityData(
        computer_name=computer_name,
        computer_domain=computer_domain,
        computer_sid=comp_sid,
        user_name=user_name,
        user_sid=user_sid,
        status=status,
        error=error,
    )


def collect_updates(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> UpdatesData:
    stdout, psinfo_err = collect_psinfo_raw(
        host,
        include_disks=False,
        include_hotfixes=True,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    hotfixes: List[PsInfoHotfix] = []
    note = ""
    if stdout:
        parsed = parse_psinfo_output(stdout, host=host)
        hotfixes = list(parsed.hotfixes)
        if hotfixes:
            note = "Hotfixes obtidos do PsInfo (-h)."

    if not hotfixes:
        items, err_hf = list_remote_hotfixes(
            host,
            user=user,
            password=password,
            timeout=90.0,
            pstools_dir=pstools_dir,
        )
        if items:
            hotfixes = items
            via = "PsExec" if err_hf == "via PsExec" else "Get-HotFix"
            note = f"Hotfixes via {via} ({len(items)} item(ns))."
        elif psinfo_err:
            return UpdatesData(status=QueryStatus.ERROR, error=psinfo_err, note=err_hf or "")

    last_id = ""
    last_date = ""
    if hotfixes:
        for hf in hotfixes:
            shown = to_display_date(hf.installed)
            if shown:
                hf.installed = shown
        sorted_hf = sorted(
            hotfixes,
            key=lambda h: (parse_any_date(h.installed) or datetime.min, h.id),
            reverse=True,
        )
        last_id = sorted_hf[0].id
        last_date = sorted_hf[0].installed

    return UpdatesData(
        hotfixes=hotfixes,
        last_update=last_id,
        last_update_date=last_date,
        count=len(hotfixes),
        note=note,
        status=QueryStatus.OK if hotfixes or not psinfo_err else QueryStatus.ERROR,
        error="" if hotfixes else (psinfo_err or "Nenhum hotfix encontrado."),
    )


_SCRIPT_OVERVIEW_ENRICH = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cs = Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 Manufacturer, Model, Domain, Name
$net = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
  Select-Object -First 1 IPAddress, InterfaceAlias
$uptimeSeconds = $null
$os = Get-CimInstance Win32_OperatingSystem | Select-Object -First 1 LastBootUpTime
if ($os -and $os.LastBootUpTime) {
  $uptimeSeconds = [int64]((Get-Date) - [datetime]$os.LastBootUpTime).TotalSeconds
}
@{ ComputerSystem = $cs; Network = $net; UptimeSeconds = $uptimeSeconds } | ConvertTo-Json -Depth 4 -Compress
"""
