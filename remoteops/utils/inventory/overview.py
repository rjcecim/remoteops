"""Montagem da Visão geral: CIM/WMI como fonte primária, PsInfo só como fallback."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from remoteops.utils.inventory.cache import normalize_inventory_host
from remoteops.utils.inventory.collectors import _SCRIPT_OVERVIEW_ENRICH, _as_list
from remoteops.utils.inventory.formatters import (
    UNAVAILABLE,
    display_or_unavailable,
    format_last_boot,
    format_link_speed_bps,
    format_memory_from_bytes,
    format_mhz,
    format_os_architecture,
    format_storage_bytes,
    format_uptime,
    inventory_hosts_match,
    is_unavailable,
    optional_byte_count,
    optional_int,
    safe_str,
)
from remoteops.utils.inventory.models import CollectionState, OverviewData, QueryStatus
from remoteops.utils.inventory.remote_exec import run_remote_powershell
from remoteops.utils.psinfo import (
    PsInfoResult,
    collect_psinfo_raw,
    extract_psinfo_host,
    parse_disks_table,
    parse_psinfo_output,
    parse_size_to_bytes,
    patch_system_uptime,
    prepare_disks_for_display,
    uptime_from_enrich,
)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _field(value: Any) -> str:
    return display_or_unavailable(value)


def _looks_like_computer_account(name: str, host: str) -> bool:
    n = (name or "").strip().strip("\\").rstrip("$").lower()
    if "\\" in n:
        n = n.rsplit("\\", 1)[-1]
    h = normalize_inventory_host(host).split(".")[0]
    return bool(n) and bool(h) and n == h


def _join_parts(*parts: str) -> str:
    values = [p.strip() for p in parts if p and not is_unavailable(p)]
    return " · ".join(values)


def _positive_count(value: Any) -> str:
    n = optional_int(value)
    if n is None or n <= 0:
        return UNAVAILABLE
    return str(n)


def _pick_system_disk(disks: List[Any]) -> Optional[dict]:
    rows = [d for d in disks if isinstance(d, dict)]
    for row in rows:
        letter = str(row.get("DeviceID") or row.get("letter") or "").upper()
        if letter.startswith("C"):
            return row
    return rows[0] if rows else None


def _security_snapshot(sec: Any) -> tuple[str, str]:
    data = _as_dict(sec)
    if not data:
        return UNAVAILABLE, ""

    parts: List[str] = []
    details: List[str] = []

    bl = data.get("BitLocker")
    if bl is None:
        details.append("BitLocker: Não disponível")
    else:
        rows = _as_list(bl)
        active = any(
            isinstance(r, dict) and str(r.get("ProtectionStatus", "")).lower() in ("1", "on", "true")
            for r in rows
        )
        label = "BitLocker ativo" if active else "BitLocker desativado"
        if not rows:
            label = "BitLocker sem volumes"
        parts.append(label)

    defender = data.get("Defender")
    if isinstance(defender, dict):
        av = defender.get("AntivirusEnabled")
        rt = defender.get("RealTimeProtectionEnabled")
        if av is True or rt is True:
            parts.append("Defender ativo")
        elif av is False and rt is False:
            parts.append("Defender desativado")
        else:
            details.append("Defender: Não disponível")
    elif defender is None:
        details.append("Defender: Não disponível")

    fw = data.get("Firewall")
    if fw is None:
        details.append("Firewall: Não disponível")
    else:
        profiles = [p for p in _as_list(fw) if isinstance(p, dict)]
        enabled = [p for p in profiles if p.get("Enabled")]
        if profiles and len(enabled) == len(profiles):
            parts.append("Firewall ativo")
        elif enabled:
            parts.append(f"Firewall parcial ({len(enabled)}/{len(profiles)})")
        elif profiles:
            parts.append("Firewall desativado")
        else:
            details.append("Firewall: Não disponível")

    uac = data.get("UAC")
    if uac is None:
        details.append("UAC: Não disponível")
    else:
        try:
            on = bool(int(uac))
        except (TypeError, ValueError):
            on = bool(uac)
        parts.append("UAC ativo" if on else "UAC desativado")

    if not parts:
        return UNAVAILABLE, " · ".join(details)
    return " · ".join(parts), " · ".join(details)


def _updates_snapshot(upd: Any) -> tuple[str, str]:
    data = _as_dict(upd)
    if not data:
        return UNAVAILABLE, ""
    count = optional_int(data.get("Count"))
    if count is None:
        return UNAVAILABLE, ""
    last_id = safe_str(data.get("LastId"), default="")
    last_date = format_last_boot(data.get("LastDate"))
    if last_date == UNAVAILABLE:
        last_date = ""
    noun = "hotfix" if count == 1 else "hotfixes"
    detail = _join_parts(last_id if last_id and last_id != UNAVAILABLE else "", last_date)
    return f"{count} {noun}", detail


def _memory_from_psinfo(text: str) -> tuple[str, str]:
    raw = safe_str(text, default="")
    if is_unavailable(raw):
        return UNAVAILABLE, ""
    size_b = parse_size_to_bytes(raw)
    if size_b > 0:
        return format_memory_from_bytes(size_b)
    return raw, ""


def _storage_from_cim(disks: List[Any]) -> tuple[str, str, str, str, str]:
    disk = _pick_system_disk(disks)
    if disk is None:
        return UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, ""
    letter = safe_str(disk.get("DeviceID"), default="")
    fs = _field(disk.get("FileSystem"))
    capacity = format_storage_bytes(optional_byte_count(disk.get("Size")))
    free = format_storage_bytes(optional_byte_count(disk.get("FreeSpace")))
    summary = _join_parts(letter if letter != UNAVAILABLE else "", fs, capacity)
    detail = f"{free} livres" if not is_unavailable(free) else UNAVAILABLE
    return (
        summary or UNAVAILABLE,
        detail,
        fs,
        capacity,
        free,
    )


def _storage_from_psinfo(parsed: Optional[PsInfoResult]) -> tuple[str, str, str, str, str]:
    if parsed is None or not parsed.disks_raw:
        return UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, ""
    rows = parse_disks_table(parsed.disks_raw)
    display, _, _root = prepare_disks_for_display(
        rows, system_root=(parsed.system or {}).get("System root", "")
    )
    fixed = [r for r in display if r.type.lower() == "fixed" and (r.volume or "").lower() != "total"]
    if not fixed:
        return UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, ""
    main = fixed[0]
    fs = _field(main.format)
    capacity = _field(main.size)
    free = _field(main.free)
    letter = safe_str(main.volume, default="")
    summary = _join_parts(letter, fs, capacity)
    detail = f"{free} livres" if not is_unavailable(free) else UNAVAILABLE
    return summary or UNAVAILABLE, detail, fs, capacity, free


def _network_from_enrich(net: Any) -> tuple[str, str, str, str, str, str]:
    data = _as_dict(net)
    if not data:
        return UNAVAILABLE, "", UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE
    ip = _field(data.get("IPAddress"))
    adapter = _field(data.get("Description") or data.get("InterfaceAlias"))
    alias = safe_str(data.get("InterfaceAlias"), default="")
    gateway = _field(data.get("Gateway") or data.get("DefaultIPGateway"))
    speed_raw = data.get("Speed") or data.get("LinkSpeed")
    speed = format_link_speed_bps(speed_raw) if speed_raw not in (None, "") else UNAVAILABLE
    if is_unavailable(speed):
        speed = UNAVAILABLE
    summary = ip if not is_unavailable(ip) else UNAVAILABLE
    detail_parts = [
        p
        for p in (
            adapter if not is_unavailable(adapter) else "",
            alias if alias and alias != adapter and not is_unavailable(alias) else "",
            f"Velocidade: {speed}" if not is_unavailable(speed) else "",
            f"Gateway: {gateway}" if not is_unavailable(gateway) else "",
        )
        if p
    ]
    return (
        summary,
        " · ".join(detail_parts),
        adapter,
        ip,
        speed,
        gateway,
    )


def _cpu_from_cim(cpu: Any) -> tuple[str, str, str, str, str]:
    data = _as_dict(cpu)
    if not data:
        return UNAVAILABLE, "", UNAVAILABLE, UNAVAILABLE, UNAVAILABLE
    name = _field(data.get("Name"))
    cores = _positive_count(data.get("NumberOfCores"))
    logical = _positive_count(data.get("NumberOfLogicalProcessors"))
    clock = format_mhz(data.get("MaxClockSpeed") or data.get("CurrentClockSpeed"))
    if is_unavailable(clock):
        clock = UNAVAILABLE
    detail = _join_parts(
        f"Núcleos: {cores}" if not is_unavailable(cores) else "",
        f"Lógicos: {logical}" if not is_unavailable(logical) else "",
        f"Clock: {clock}" if not is_unavailable(clock) else "",
    )
    return name, detail, cores, logical, clock


def _completeness(data: OverviewData) -> str:
    if data.host_mismatch or data.status == QueryStatus.ERROR:
        return CollectionState.ERROR.value
    core_missing = any(
        is_unavailable(v)
        for v in (data.os_name, data.memory_summary, data.hostname)
    )
    important_missing = any(
        is_unavailable(v)
        for v in (
            data.cpu_summary,
            data.storage_capacity,
            data.network_ip,
            data.os_version,
            data.os_build,
            data.os_architecture,
        )
    )
    if core_missing or important_missing:
        return CollectionState.PARTIAL.value
    return CollectionState.COMPLETE.value


def build_overview_data(
    host: str,
    *,
    enrich: Optional[dict] = None,
    psinfo: Optional[PsInfoResult] = None,
) -> OverviewData:
    """Mapeia CIM (primário) e PsInfo (fallback) para OverviewData.

    Não inventa valores. Campo ausente → «Não disponível».
    Memória CIM usa TotalPhysicalMemory em bytes, convertida uma única vez.
    """
    requested = (host or "").strip().strip("\\")
    payload = enrich if isinstance(enrich, dict) else {}
    sysinfo = (psinfo.system if psinfo else {}) or {}

    cs = _as_dict(payload.get("ComputerSystem"))
    osinfo = _as_dict(payload.get("OperatingSystem"))
    cpu_raw = payload.get("Processor") or payload.get("Processors")
    if isinstance(cpu_raw, list):
        cpu = _as_dict(cpu_raw[0] if cpu_raw else None)
    else:
        cpu = _as_dict(cpu_raw)

    collected = safe_str(cs.get("Name"), default="")
    if is_unavailable(collected) and psinfo is not None:
        collected = extract_psinfo_host(psinfo) or ""
    if is_unavailable(collected):
        collected = ""

    mismatch = bool(collected) and not inventory_hosts_match(requested, collected)
    hostname = collected or requested

    manufacturer = _field(cs.get("Manufacturer"))
    model = _field(cs.get("Model"))
    domain = _field(cs.get("Domain"))
    username_raw = safe_str(cs.get("UserName"), default="")
    if is_unavailable(username_raw) or _looks_like_computer_account(username_raw, hostname):
        username = UNAVAILABLE
    else:
        username = username_raw

    os_name = _field(osinfo.get("Caption"))
    os_version = _field(osinfo.get("Version"))
    os_build = _field(osinfo.get("BuildNumber"))
    os_architecture = (
        format_os_architecture(osinfo.get("OSArchitecture"))
        if osinfo.get("OSArchitecture") not in (None, "")
        else UNAVAILABLE
    )
    last_boot = (
        format_last_boot(osinfo.get("LastBootUpTime"))
        if osinfo.get("LastBootUpTime") not in (None, "")
        else UNAVAILABLE
    )

    if is_unavailable(os_name):
        os_name = _field(sysinfo.get("Product type"))
    if is_unavailable(os_version):
        os_version = _field(sysinfo.get("Kernel version") or sysinfo.get("Product version"))
    if is_unavailable(os_build):
        os_build = _field(sysinfo.get("Kernel build number"))

    memory_bytes = optional_byte_count(cs.get("TotalPhysicalMemory"))
    if memory_bytes is not None:
        memory_summary, memory_detail = format_memory_from_bytes(memory_bytes)
    else:
        memory_summary, memory_detail = _memory_from_psinfo(sysinfo.get("Physical memory", ""))

    cpu_summary, cpu_detail, cpu_cores, cpu_logical, cpu_clock = _cpu_from_cim(cpu)
    if is_unavailable(cpu_summary):
        cpu_summary = _field(sysinfo.get("Processor type"))
    if is_unavailable(cpu_clock):
        clock_ps = format_mhz(sysinfo.get("Processor speed"))
        cpu_clock = clock_ps if not is_unavailable(clock_ps) else UNAVAILABLE
    if is_unavailable(cpu_detail):
        cpu_detail = _join_parts(
            f"Núcleos: {cpu_cores}" if not is_unavailable(cpu_cores) else "",
            f"Lógicos: {cpu_logical}" if not is_unavailable(cpu_logical) else "",
            f"Clock: {cpu_clock}" if not is_unavailable(cpu_clock) else "",
        )

    disks = _as_list(payload.get("Disks"))
    storage_summary, storage_detail, storage_fs, storage_cap, storage_free = _storage_from_cim(disks)
    if is_unavailable(storage_summary):
        storage_summary, storage_detail, storage_fs, storage_cap, storage_free = _storage_from_psinfo(
            psinfo
        )

    (
        network_summary,
        network_detail,
        network_adapter,
        network_ip,
        network_speed,
        network_gateway,
    ) = _network_from_enrich(payload.get("Network"))

    uptime = ""
    if payload.get("UptimeSeconds") is not None:
        uptime = uptime_from_enrich(payload) or ""
    if not uptime:
        uptime = format_uptime(sysinfo.get("Uptime", ""))
    if is_unavailable(uptime):
        uptime = UNAVAILABLE

    security_summary, security_detail = _security_snapshot(payload.get("Security"))
    updates_summary, updates_detail = _updates_snapshot(payload.get("Updates"))

    os_summary = _join_parts(os_name, os_version, os_build, os_architecture) or UNAVAILABLE

    data = OverviewData(
        hostname=hostname,
        manufacturer=manufacturer,
        model=model,
        os_summary=os_summary,
        os_name=os_name,
        os_version=os_version,
        os_build=os_build,
        os_architecture=os_architecture,
        domain=domain,
        username=username,
        last_boot=last_boot,
        uptime=uptime,
        cpu_summary=cpu_summary,
        cpu_detail=cpu_detail,
        cpu_cores=cpu_cores,
        cpu_logical=cpu_logical,
        cpu_clock=cpu_clock,
        memory_summary=memory_summary,
        memory_detail=memory_detail,
        storage_summary=storage_summary,
        storage_detail=storage_detail,
        storage_filesystem=storage_fs,
        storage_capacity=storage_cap,
        storage_free=storage_free,
        network_summary=network_summary,
        network_detail=network_detail,
        network_adapter=network_adapter,
        network_ip=network_ip,
        network_speed=network_speed,
        network_gateway=network_gateway,
        security_summary=security_summary,
        security_detail=security_detail,
        updates_summary=updates_summary,
        updates_detail=updates_detail,
        collected_host=collected,
        requested_host=requested,
        host_mismatch=mismatch,
        psinfo=psinfo,
        status=QueryStatus.ERROR if mismatch else QueryStatus.OK,
        error=(
            f"O host retornado ({collected}) não corresponde ao host solicitado ({requested})."
            if mismatch
            else ""
        ),
    )
    data.completeness = _completeness(data)
    return data


def overview_to_legacy_dict(data: OverviewData) -> dict:
    return {
        "hostname": data.hostname,
        "manufacturer": data.manufacturer,
        "model": data.model,
        "os_summary": data.os_summary,
        "domain": data.domain,
        "uptime": data.uptime,
        "cpu_summary": data.cpu_summary,
        "cpu_detail": data.cpu_detail,
        "memory_summary": data.memory_summary,
        "memory_detail": data.memory_detail,
        "storage_summary": data.storage_summary,
        "storage_detail": data.storage_detail,
        "network_summary": data.network_summary,
        "network_detail": data.network_detail,
        "security_summary": data.security_summary,
        "security_detail": data.security_detail,
        "updates_summary": data.updates_summary,
        "updates_detail": data.updates_detail,
        "psinfo": data.psinfo,
    }


def collect_overview(
    host: str,
    *,
    user: str = "",
    password: str = "",
    pstools_dir: str = "",
) -> OverviewData:
    requested = (host or "").strip().strip("\\")
    if not requested:
        return OverviewData(
            requested_host="",
            status=QueryStatus.ERROR,
            completeness=CollectionState.ERROR.value,
            error="Host remoto não informado.",
        )

    stdout, ps_err = collect_psinfo_raw(
        requested,
        include_disks=True,
        include_hotfixes=False,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    parsed = parse_psinfo_output(stdout, host=requested) if stdout else None
    if parsed is not None:
        patch_system_uptime(
            parsed.system,
            requested,
            user=user,
            password=password,
            pstools_dir=pstools_dir,
        )

    enrich, cim_err = run_remote_powershell(
        requested,
        _SCRIPT_OVERVIEW_ENRICH,
        user=user,
        password=password,
        pstools_dir=pstools_dir,
    )
    enrich_dict = enrich if isinstance(enrich, dict) else None

    if parsed is None and enrich_dict is None:
        return OverviewData(
            hostname=requested,
            requested_host=requested,
            status=QueryStatus.ERROR,
            completeness=CollectionState.ERROR.value,
            error=cim_err or ps_err or "Não foi possível consultar o host.",
        )

    data = build_overview_data(requested, enrich=enrich_dict, psinfo=parsed)
    if data.host_mismatch:
        return OverviewData(
            hostname=requested,
            requested_host=requested,
            collected_host=data.collected_host,
            host_mismatch=True,
            status=QueryStatus.ERROR,
            completeness=CollectionState.ERROR.value,
            error=(
                f"O host retornado ({data.collected_host}) não corresponde "
                f"ao host solicitado ({requested})."
            ),
        )
    if parsed is None and enrich_dict is not None and cim_err:
        data.error = ""
    return data
