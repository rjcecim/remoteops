"""Scripts de Inventário: Write-RemoteOpsJson e contratos JSON dos coletores."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from remoteops.utils import psinfo
from remoteops.utils.inventory import collectors
from remoteops.utils.inventory.models import QueryStatus
from remoteops.utils.local_accounts import LOCAL_ACCOUNTS_QUERY_SCRIPT


def _inventory_json_scripts() -> list[tuple[str, str]]:
    scripts: list[tuple[str, str]] = []
    for name in sorted(dir(collectors)):
        if name.startswith("_SCRIPT_"):
            scripts.append((f"collectors.{name}", getattr(collectors, name)))
    scripts.append(("psinfo._SCRIPT_UPTIME", psinfo._SCRIPT_UPTIME))
    scripts.append(("local_accounts.LOCAL_ACCOUNTS_QUERY_SCRIPT", LOCAL_ACCOUNTS_QUERY_SCRIPT))
    return scripts


class InventoryScriptEmitTests(unittest.TestCase):
    def test_every_inventory_json_script_uses_write_remoteops_json(self) -> None:
        scripts = _inventory_json_scripts()
        self.assertGreaterEqual(len(scripts), 10)
        for name, script in scripts:
            with self.subTest(script=name):
                self.assertIn("Write-RemoteOpsJson", script)
                self.assertIn("ConvertTo-Json -InputObject", script)
                self.assertIn("Get-Command Write-RemoteOpsJson", script)
                self.assertNotIn("| ConvertTo-Json", script)

    def test_array_scripts_preserve_single_element_collections(self) -> None:
        script = collectors._SCRIPT_NETWORK
        self.assertIn("ConvertTo-Json -InputObject @($result)", script)
        self.assertIn("$json = '[]'", script)

    def test_object_scripts_emit_object_json(self) -> None:
        object_scripts = (
            "_SCRIPT_HARDWARE",
            "_SCRIPT_MEMORY",
            "_SCRIPT_STORAGE",
            "_SCRIPT_VIDEO",
            "_SCRIPT_FIRMWARE",
            "_SCRIPT_SECURITY",
            "_SCRIPT_IDENTITY",
            "_SCRIPT_SYSTEM_ENRICH",
            "_SCRIPT_OVERVIEW_ENRICH",
        )
        for name in object_scripts:
            script = getattr(collectors, name)
            with self.subTest(script=name):
                self.assertIn("ConvertTo-Json -InputObject $result", script)
                self.assertIn("$json = '{}'", script)
                self.assertNotIn("ConvertTo-Json -InputObject @($result)", script)

    def test_video_script_queries_monitors(self) -> None:
        script = collectors._SCRIPT_VIDEO
        self.assertIn("WmiMonitorID", script)
        self.assertIn("ManufacturerName", script)
        self.assertIn("UserFriendlyName", script)
        self.assertIn("SerialNumberID", script)
        self.assertIn("Monitors", script)
        self.assertIn("Adapters", script)


class HardwareCollectorTests(unittest.TestCase):
    def test_multiple_processors(self) -> None:
        payload = {
            "ComputerSystem": {
                "Manufacturer": "Dell",
                "Model": "OptiPlex",
                "Name": "PC1",
                "PCSystemType": 1,
            },
            "ComputerSystemProduct": {"IdentifyingNumber": "SN1", "UUID": "u-1", "Name": ""},
            "Processors": [
                {
                    "Name": "Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz",
                    "Manufacturer": "Intel",
                    "SocketDesignation": "U3E1",
                    "NumberOfCores": 6,
                    "NumberOfLogicalProcessors": 12,
                    "MaxClockSpeed": 2300,
                    "CurrentClockSpeed": 2300,
                    "Architecture": 9,
                },
                {
                    "Name": "Intel(R) Xeon(R) CPU",
                    "Manufacturer": "Intel",
                    "SocketDesignation": "U3E2",
                    "NumberOfCores": 8,
                    "NumberOfLogicalProcessors": 16,
                    "MaxClockSpeed": 2500,
                    "CurrentClockSpeed": 2400,
                    "Architecture": 9,
                },
            ],
            "BaseBoard": {
                "Manufacturer": "Dell",
                "Product": "0XYZ",
                "SerialNumber": "MB1",
                "Version": "A00",
            },
        }
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_hardware("HOST1")
        self.assertEqual(result.status, QueryStatus.OK)
        proc = {f.label: f.value for f in result.fields["Processador"]}
        self.assertEqual(proc["Núcleos"], "14")
        self.assertEqual(proc["Processadores lógicos"], "28")
        self.assertEqual(proc["Observação"], "2 processadores detectados")


class NetworkCollectorTests(unittest.TestCase):
    def test_multiple_adapters(self) -> None:
        payload = [
            {
                "Name": "Ethernet 9",
                "Description": "Fortinet Virtual Ethernet Adapter",
                "Status": "Up",
                "IPv4": "10.0.0.9",
                "IPv6": "",
                "Prefix": "24",
                "Gateway": "10.0.0.1",
                "DNS": "8.8.8.8",
                "MacAddress": "00-11-22-33-44-55",
                "DHCP": "Manual",
                "LinkSpeed": "1 Gbps",
                "InterfaceType": 6,
            },
            {
                "Name": "Wi-Fi",
                "Description": "Intel Wireless",
                "Status": "Up",
                "IPv4": "10.0.0.20",
                "IPv6": "",
                "Prefix": "24",
                "Gateway": "10.0.0.1",
                "DNS": "1.1.1.1",
                "MacAddress": "00-11-22-33-44-66",
                "DHCP": "Dhcp",
                "LinkSpeed": "866 Mbps",
                "InterfaceType": 71,
            },
        ]
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_network("HOST1")
        self.assertEqual(result.status, QueryStatus.OK)
        self.assertEqual([a.name for a in result.adapters], ["Ethernet 9", "Wi-Fi"])
        self.assertIn("Fortinet", result.adapters[0].description)

    def test_empty_adapter_list(self) -> None:
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=([], ""),
        ):
            result = collectors.collect_network("HOST1")
        self.assertEqual(result.status, QueryStatus.OK)
        self.assertEqual(result.adapters, [])

    def test_single_adapter_object_is_accepted(self) -> None:
        payload = {"Name": "Ethernet 9", "Description": "Fortinet Virtual Ethernet Adapter"}
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_network("HOST1")
        self.assertEqual(len(result.adapters), 1)
        self.assertEqual(result.adapters[0].name, "Ethernet 9")


class VideoCollectorTests(unittest.TestCase):
    def test_single_controller_object(self) -> None:
        payload = {
            "Name": "Intel(R) UHD Graphics 630",
            "AdapterCompatibility": "Intel Corporation",
            "DriverVersion": "27.20.100.8681",
            "DriverDate": "20210101000000.000000-000",
            "CurrentHorizontalResolution": 1920,
            "CurrentVerticalResolution": 1080,
            "AdapterRAM": 1073741824,
            "VideoProcessor": "Intel(R) UHD Graphics Family",
        }
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(result.status, QueryStatus.OK)
        self.assertEqual(len(result.adapters), 1)
        self.assertEqual(result.adapters[0].name, "Intel(R) UHD Graphics 630")
        self.assertEqual(result.adapters[0].manufacturer, "Intel Corporation")
        self.assertEqual(result.adapters[0].resolution, "1920 × 1080")

    def test_single_controller_array(self) -> None:
        payload = [{"Name": "Intel(R) UHD Graphics 630", "AdapterCompatibility": "Intel Corporation"}]
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(len(result.adapters), 1)
        self.assertEqual(result.adapters[0].name, "Intel(R) UHD Graphics 630")

    def test_multiple_controllers(self) -> None:
        payload = [
            {"Name": "Intel(R) UHD Graphics 630", "AdapterCompatibility": "Intel Corporation"},
            {"Name": "NVIDIA GeForce GTX 1650", "AdapterCompatibility": "NVIDIA"},
        ]
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(
            [a.name for a in result.adapters],
            ["Intel(R) UHD Graphics 630", "NVIDIA GeForce GTX 1650"],
        )
        self.assertEqual(result.monitors, [])

    def test_combined_payload_with_monitors(self) -> None:
        payload = {
            "Adapters": [
                {"Name": "Intel(R) UHD Graphics 630", "AdapterCompatibility": "Intel Corporation"},
            ],
            "Monitors": [
                {"Manufacturer": "HPN", "Model": "HP P24a G4", "Serial": "BRC22707B4"},
                {"Manufacturer": "HPN", "Model": "HP E24mv G4", "Serial": "CNC2271M97"},
            ],
        }
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(result.status, QueryStatus.OK)
        self.assertEqual(result.adapters[0].name, "Intel(R) UHD Graphics 630")
        self.assertEqual(
            [(m.manufacturer, m.model, m.serial) for m in result.monitors],
            [
                ("HPN", "HP P24a G4", "BRC22707B4"),
                ("HPN", "HP E24mv G4", "CNC2271M97"),
            ],
        )

    def test_single_monitor_object_is_list(self) -> None:
        payload = {
            "Adapters": {"Name": "Intel(R) UHD Graphics 630"},
            "Monitors": {"Manufacturer": "HPN", "Model": "HP P24a G4", "Serial": "BRC22707B4"},
        }
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(len(result.adapters), 1)
        self.assertEqual(len(result.monitors), 1)
        self.assertEqual(result.monitors[0].model, "HP P24a G4")
        self.assertEqual(result.monitors[0].serial, "BRC22707B4")

    def test_empty_monitors_are_valid(self) -> None:
        payload = {"Adapters": [], "Monitors": []}
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(result.status, QueryStatus.OK)
        self.assertEqual(result.adapters, [])
        self.assertEqual(result.monitors, [])

    def test_blank_monitor_rows_are_ignored(self) -> None:
        payload = {
            "Adapters": [],
            "Monitors": [
                {"Manufacturer": "", "Model": "", "Serial": ""},
                {"Manufacturer": "HPN", "Model": "HP P24a G4", "Serial": "BRC22707B4"},
            ],
        }
        with patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(payload, ""),
        ):
            result = collectors.collect_video("HOST1")
        self.assertEqual(len(result.monitors), 1)
        self.assertEqual(result.monitors[0].serial, "BRC22707B4")


if __name__ == "__main__":
    unittest.main()
