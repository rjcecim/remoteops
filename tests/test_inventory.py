"""Testes do módulo Inventário — sem rede nem executáveis reais."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.utils.inventory.cache import InventoryCache
from remoteops.utils.inventory.formatters import (
    architecture_label,
    bytes_to_gb,
    format_link_speed_bps,
    format_tpm_version,
    format_uptime_duration,
    is_invalid_psinfo_uptime,
    normalize_wmi_date,
    parse_psinfo_size,
    sanitize_display_text,
    smbios_memory_type,
    volume_used_pct,
)
from remoteops.utils.inventory.models import InventorySection
from remoteops.utils.inventory.psget_sid import (
    build_psgetsid_argv,
    is_psgetsid_eula_text,
    is_psgetsid_usage_text,
    parse_psgetsid_output,
    resolve_psgetsid_exe,
)
from remoteops.utils.inventory.remote_exec import (
    build_remote_powershell_argv,
    parse_json_output,
)
from remoteops.utils.inventory.service import InventoryService
from remoteops.utils.psinfo import (
    PsInfoResult,
    build_overview_from_psinfo,
    format_uptime_duration,
    is_invalid_psinfo_uptime,
)


_HARDWARE_JSON = """
{
  "ComputerSystem": {"Manufacturer": "Dell Inc.", "Model": "Latitude 5450", "Name": "PC01", "Domain": "TCEPA"},
  "ComputerSystemProduct": {"IdentifyingNumber": "SN123", "UUID": "uuid-1"},
  "Processors": [{"Name": "Intel Core Ultra 5", "NumberOfCores": 12, "NumberOfLogicalProcessors": 14, "Architecture": 9}],
  "BaseBoard": {"Manufacturer": "Dell", "Product": "0ABC", "SerialNumber": "BS123"}
}
"""

_MEMORY_JSON = """
{
  "Arrays": [{"MemoryDevices": 2, "MaxCapacity": 34359738368}],
  "Modules": [{"DeviceLocator": "DIMM1", "Capacity": 17179869184, "SMBIOSMemoryType": 34, "Speed": 5600, "Manufacturer": "Samsung"}]
}
"""


class TestFormatters(unittest.TestCase):
    def test_bytes_to_gb(self) -> None:
        self.assertEqual(bytes_to_gb(17179869184), "16.0 GB")

    def test_smbios_ddr5(self) -> None:
        self.assertEqual(smbios_memory_type(34), "DDR5")

    def test_architecture_x64(self) -> None:
        self.assertEqual(architecture_label(9), "x64")

    def test_link_speed(self) -> None:
        self.assertEqual(format_link_speed_bps(1_000_000_000), "1 Gbps")

    def test_volume_used_pct(self) -> None:
        self.assertEqual(volume_used_pct(100, 25), 75.0)

    def test_parse_psinfo_size(self) -> None:
        self.assertGreater(parse_psinfo_size("512 GB"), 0)

    def test_uptime_invalid_psinfo(self) -> None:
        self.assertTrue(is_invalid_psinfo_uptime("Error reading uptime"))
        self.assertFalse(is_invalid_psinfo_uptime("2 days 1 hours 0 minutes 5 seconds"))

    def test_uptime_duration(self) -> None:
        self.assertEqual(
            format_uptime_duration(192),
            "0 days 0 hours 3 minutes 12 seconds",
        )

    def test_dotnet_json_date(self) -> None:
        self.assertEqual(normalize_wmi_date("/Date(1728000000000)/"), "04/10/2024")

    def test_sanitize_tpm_garbage(self) -> None:
        raw = "7.85.4555.0\x00" + "\x00" * 20
        self.assertEqual(format_tpm_version(None, raw), "7.85.4555.0")
        self.assertEqual(sanitize_display_text(raw), "7.85.4555.0")


class TestJsonParsing(unittest.TestCase):
    def test_parse_clean_json(self) -> None:
        data, err = parse_json_output('{"a": 1}')
        self.assertFalse(err)
        self.assertEqual(data, {"a": 1})

    def test_parse_with_prefix_noise(self) -> None:
        raw = "WARNING: foo\n" + _HARDWARE_JSON.strip()
        data, err = parse_json_output(raw)
        self.assertFalse(err)
        self.assertIsInstance(data, dict)

    def test_invalid_json(self) -> None:
        data, err = parse_json_output("not json")
        self.assertIsNone(data)
        self.assertTrue(err)


class TestPsGetSid(unittest.TestCase):
    def test_resolve_prefers_64(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsGetSid64.exe"), "wb").close()
            open(os.path.join(folder, "PsGetSid.exe"), "wb").close()
            resolved = resolve_psgetsid_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsGetSid64.exe")

    def test_build_argv_with_credentials(self) -> None:
        argv = build_psgetsid_argv(
            r"C:\PSTools\PsGetSid64.exe",
            "HOST01",
            account="DOMAIN\\user",
            user="DOMAIN\\admin",
            password="secret",
        )
        self.assertEqual(argv[0], r"C:\PSTools\PsGetSid64.exe")
        self.assertIn("-accepteula", argv)
        self.assertIn(r"\\HOST01", argv)
        self.assertIn("DOMAIN\\user", argv)
        self.assertIn("-u", argv)
        self.assertIn("-p", argv)
        self.assertIn("secret", argv)

    def test_parse_output(self) -> None:
        text = """
        PsGetSid v1.45
        Account for TCEPA\\usuario is:
        S-1-5-21-1234567890-123456789-123456789-1001
        """
        account, sid = parse_psgetsid_output(text)
        self.assertEqual(sid, "S-1-5-21-1234567890-123456789-123456789-1001")
        self.assertIn("usuario", account)

    def test_parse_sid_for_computer_account(self) -> None:
        text = "SID for TCE-PA\\ETSETIN-CAU11$:\nS-1-5-21-1996108351-196294233-20515302-44772"
        account, sid = parse_psgetsid_output(text)
        self.assertEqual(account, r"TCE-PA\ETSETIN-CAU11$")
        self.assertEqual(sid, "S-1-5-21-1996108351-196294233-20515302-44772")
        self.assertNotIn("SID for", account)

    def test_usage_detection(self) -> None:
        self.assertTrue(is_psgetsid_usage_text("Usage: psgetsid [-nobanner]"))

    def test_eula_detection(self) -> None:
        self.assertTrue(
            is_psgetsid_eula_text("SYSINTERNALS SOFTWARE LICENSE TERMS\nThese license terms...")
        )


class TestRemotePowershellArgv(unittest.TestCase):
    def test_argv_structure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsExec64.exe"), "wb").close()
            argv = build_remote_powershell_argv(
                "HOST",
                "Get-CimInstance Win32_OperatingSystem | ConvertTo-Json",
                user="DOM\\u",
                password="p",
                pstools_dir=folder,
            )
            self.assertTrue(argv[0].endswith("PsExec64.exe"))
            self.assertIn(r"\\HOST", argv)
            self.assertIn("powershell.exe", argv)
            self.assertIn("-Command", argv)


class TestCollectorsParsing(unittest.TestCase):
    def test_parse_hardware_from_fixture(self) -> None:
        data, _ = parse_json_output(_HARDWARE_JSON)
        # Simular coleta injetando parse — testamos campos via mock manual
        self.assertIsInstance(data, dict)
        self.assertEqual(data["ComputerSystem"]["Manufacturer"], "Dell Inc.")

    def test_parse_memory_fixture(self) -> None:
        data, _ = parse_json_output(_MEMORY_JSON)
        modules = data.get("Modules", [])
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["SMBIOSMemoryType"], 34)


class TestInventoryCache(unittest.TestCase):
    def test_invalidation_on_host_change(self) -> None:
        cache = InventoryCache()
        cache.set_host("HOST1")
        cache.put(InventorySection.MEMORY, {"x": 1})
        self.assertTrue(cache.has(InventorySection.MEMORY))
        cache.set_host("HOST2")
        self.assertFalse(cache.has(InventorySection.MEMORY))

    def test_section_invalidation(self) -> None:
        cache = InventoryCache()
        cache.set_host("H")
        cache.put(InventorySection.OVERVIEW, object())
        cache.invalidate(InventorySection.OVERVIEW)
        self.assertFalse(cache.has(InventorySection.OVERVIEW))


class TestInventoryService(unittest.TestCase):
    def test_cached_section_not_recollected(self) -> None:
        service = InventoryService()
        service.cache.set_host("cached-host")
        sentinel = object()
        service.cache.put(InventorySection.SYSTEM, sentinel)
        result = service.collect_section(
            InventorySection.SYSTEM,
            "cached-host",
            force=False,
        )
        self.assertIs(result.payload, sentinel)


class TestOverviewUptime(unittest.TestCase):
    def test_overview_uptime_fallback_from_enrich(self) -> None:
        parsed = PsInfoResult(
            host="PC01",
            header=[],
            system={"Uptime": "Error reading uptime", "Kernel version": "Windows 11"},
            applications=[],
            disks_raw=[],
            hotfixes=[],
            raw_text="",
        )
        summary = build_overview_from_psinfo(
            parsed,
            "PC01",
            enrich={"UptimeSeconds": 90061},
        )
        self.assertEqual(summary["uptime"], "1 days 1 hours 1 minutes 1 seconds")
        self.assertFalse(is_invalid_psinfo_uptime(summary["uptime"]))


class TestNetworkPrimary(unittest.TestCase):
    def test_primary_prefers_up_ethernet_over_disconnected_bluetooth(self) -> None:
        from remoteops.ui.inventory.widgets import _is_active_adapter, _pick_primary_adapter, _primary_ipv4
        from remoteops.utils.inventory.models import NetworkAdapter

        adapters = [
            NetworkAdapter(
                name="Conexão de Rede Bluetooth",
                description="Bluetooth Device (Personal Area Network)",
                status="Disconnected",
                ipv4="169.254.55.42",
                mac="64-32-A8-15-ED-A1",
                link_speed="3 Mbps",
            ),
            NetworkAdapter(
                name="Ethernet 6",
                description="Intel(R) Ethernet Connection (11) I219-LM",
                status="Up",
                ipv4="192.168.24.77",
                mac="00-68-EB-BC-69-60",
                gateway="192.168.24.1",
                link_speed="100 Mbps",
            ),
        ]
        primary = _pick_primary_adapter(adapters)
        self.assertIsNotNone(primary)
        assert primary is not None
        self.assertEqual(primary.name, "Ethernet 6")
        self.assertEqual(_primary_ipv4(primary), "192.168.24.77")
        self.assertTrue(_is_active_adapter(primary))
        self.assertFalse(_is_active_adapter(adapters[0]))


class TestSecuritySummary(unittest.TestCase):
    def test_tpm_version_counts_as_active_not_warning(self) -> None:
        from remoteops.ui.inventory.widgets import _security_summary
        from remoteops.utils.inventory.models import SecurityItem

        items = [
            SecurityItem("TPM", "7.85.4555.0"),
            SecurityItem("Firewall", "Ativado"),
            SecurityItem("Secure Boot", "Desativado"),
        ]
        active, warning, disabled = _security_summary(items)
        self.assertEqual(active, 2)
        self.assertEqual(warning, 0)
        self.assertEqual(disabled, 1)


if __name__ == "__main__":
    unittest.main()
