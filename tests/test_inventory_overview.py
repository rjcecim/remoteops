"""Visão geral: coleta CIM, conversões, host e renderização."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget

from remoteops.ui.tabs.inventario import InventarioTab
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.utils.inventory.formatters import (
    UNAVAILABLE,
    format_memory_from_bytes,
    format_os_architecture,
    inventory_hosts_match,
    optional_byte_count,
    optional_int,
)
from remoteops.utils.inventory.models import (
    CollectionState,
    InventorySection,
    OverviewData,
    QueryStatus,
    SectionResult,
)
from remoteops.utils.inventory.overview import (
    build_overview_data,
    collect_overview,
)
from remoteops.utils.inventory.service import InventoryService
from remoteops.utils.psinfo import PsInfoResult, parse_psinfo_output


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _section_texts(tab: InventarioTab, section: InventorySection) -> str:
    page = tab._section_pages[section]
    return " ".join(lbl.text() for lbl in page.findChildren(QLabel) if lbl.text())


def _has_loading(tab: InventarioTab, section: InventorySection) -> bool:
    page = tab._section_pages[section]
    return page.findChild(QWidget, "inventoryLoadingState") is not None


def _has_spinner(tab: InventarioTab, section: InventorySection) -> bool:
    page = tab._section_pages[section]
    return any(isinstance(w, DotsSpinner) for w in page.findChildren(QWidget))


CAP10_BYTES = 8_455_716_864  # ~7.87 GiB


def _cim_etseadm() -> dict:
    return {
        "ComputerSystem": {
            "Name": "ETSEADM-CAP10",
            "Manufacturer": "Positivo Tecnologia SA",
            "Model": "D6200",
            "Domain": "tce.pa",
            "UserName": r"ETSEADM-CAP10\tce_admin",
            "TotalPhysicalMemory": CAP10_BYTES,
        },
        "OperatingSystem": {
            "Caption": "Microsoft Windows 11 Pro",
            "Version": "10.0.26100",
            "BuildNumber": "26100",
            "OSArchitecture": "64-bit",
            "LastBootUpTime": "2026-09-17T10:15:00",
        },
        "Processor": {
            "Name": "Intel(R) Core(TM) i5-8600 CPU @ 3.10GHz",
            "NumberOfCores": 6,
            "NumberOfLogicalProcessors": 6,
            "MaxClockSpeed": 3096,
        },
        "Disks": [
            {
                "DeviceID": "C:",
                "FileSystem": "NTFS",
                "Size": 254_803_968_000,
                "FreeSpace": 107_224_268_800,
            }
        ],
        "Network": {
            "Description": "Realtek PCIe GbE Family Controller",
            "IPAddress": "192.168.4.119",
            "Gateway": "192.168.4.1",
            "Speed": 100_000_000,
            "InterfaceAlias": "Ethernet 5",
        },
        "UptimeSeconds": 3600 * 24 * 4,
        "Security": {
            "BitLocker": [{"MountPoint": "C:", "ProtectionStatus": 0}],
            "Defender": {"AntivirusEnabled": True, "RealTimeProtectionEnabled": True},
            "Firewall": [{"Name": "Domain", "Enabled": True}],
            "UAC": 1,
        },
        "Updates": {"Count": 42, "LastId": "KB5065426", "LastDate": "2026-09-10"},
    }


def _psinfo_windows10() -> PsInfoResult:
    raw = """
System information for \\\\ETSEADM-CAP10:

Uptime:                    4 days 0 hours 0 minutes 0 seconds
Kernel version:            10.0.26100
Kernel build number:       26100
Product type:              Windows 10 Pro, Multiprocessor Free
Product version:           6.3
Processors:                6
Processor type:            Intel Core i5-8600 CPU @ 3.10 GHz
Processor speed:           3.1 GHz
Physical memory:           32474 MB
System root:               C:\\WINDOWS

Volume Type       Format     Label                      Size         Free    Free
      C: Fixed      NTFS                            237.37 GB    99.86 GB     42%
"""
    return parse_psinfo_output(raw, host="ETSEADM-CAP10")


class MemoryConversionTests(unittest.TestCase):
    def test_bytes_to_gb_for_cap10(self) -> None:
        primary, detail = format_memory_from_bytes(CAP10_BYTES)
        self.assertIn("GB", primary)
        self.assertTrue(primary.startswith("7.8"))
        self.assertIn("MB", detail)
        self.assertNotIn("32474", primary)
        self.assertNotIn("32474", detail)

    def test_missing_bytes_are_unavailable_not_zero(self) -> None:
        primary, detail = format_memory_from_bytes(None)
        self.assertEqual(primary, UNAVAILABLE)
        self.assertEqual(detail, "")
        primary0, _ = format_memory_from_bytes(0)
        self.assertEqual(primary0, UNAVAILABLE)

    def test_optional_byte_count_rejects_empty(self) -> None:
        self.assertIsNone(optional_byte_count(None))
        self.assertIsNone(optional_byte_count(""))
        self.assertIsNone(optional_byte_count(0))
        self.assertIsNone(optional_int(None))
        self.assertIsNone(optional_int(""))


class OsIdentificationTests(unittest.TestCase):
    def test_windows_11_caption_wins_over_psinfo_product_type(self) -> None:
        data = build_overview_data(
            "ETSEADM-CAP10",
            enrich=_cim_etseadm(),
            psinfo=_psinfo_windows10(),
        )
        self.assertIn("Windows 11 Pro", data.os_name)
        self.assertNotIn("Windows 10", data.os_name)
        self.assertEqual(data.os_version, "10.0.26100")
        self.assertEqual(data.os_build, "26100")
        self.assertEqual(data.os_architecture, "64 bits")

    def test_windows_10_caption_is_kept(self) -> None:
        enrich = _cim_etseadm()
        enrich["OperatingSystem"]["Caption"] = "Microsoft Windows 10 Pro"
        enrich["OperatingSystem"]["Version"] = "10.0.19045"
        enrich["OperatingSystem"]["BuildNumber"] = "19045"
        data = build_overview_data("HOST10", enrich=enrich)
        self.assertIn("Windows 10 Pro", data.os_name)
        self.assertNotIn("Windows 11", data.os_name)
        self.assertEqual(data.os_version, "10.0.19045")
        self.assertEqual(data.os_build, "19045")

    def test_os_architecture_labels(self) -> None:
        self.assertEqual(format_os_architecture("64-bit"), "64 bits")
        self.assertEqual(format_os_architecture("32-bit"), "32 bits")
        self.assertEqual(format_os_architecture(""), UNAVAILABLE)


class OverviewMappingTests(unittest.TestCase):
    def test_cim_payload_maps_identity_cpu_memory_storage_network(self) -> None:
        data = build_overview_data("ETSEADM-CAP10", enrich=_cim_etseadm())
        self.assertEqual(data.hostname, "ETSEADM-CAP10")
        self.assertEqual(data.manufacturer, "Positivo Tecnologia SA")
        self.assertEqual(data.model, "D6200")
        self.assertEqual(data.domain, "tce.pa")
        self.assertEqual(data.username, r"ETSEADM-CAP10\tce_admin")
        self.assertIn("i5-8600", data.cpu_summary)
        self.assertEqual(data.cpu_cores, "6")
        self.assertEqual(data.cpu_logical, "6")
        self.assertEqual(data.cpu_clock, "3096 MHz")
        self.assertIn("GB", data.memory_summary)
        self.assertTrue(data.memory_summary.startswith("7.8"))
        self.assertNotIn("32474", data.memory_summary)
        self.assertEqual(data.storage_filesystem, "NTFS")
        self.assertIn("237", data.storage_capacity)
        self.assertTrue(
            data.storage_free.startswith("99.8"),
            data.storage_free,
        )
        self.assertEqual(data.network_ip, "192.168.4.119")
        self.assertEqual(data.network_gateway, "192.168.4.1")
        self.assertEqual(data.network_speed, "100 Mbps")
        self.assertIn("Realtek", data.network_adapter)
        self.assertIn("Defender", data.security_summary)
        self.assertIn("42", data.updates_summary)
        self.assertFalse(data.host_mismatch)
        self.assertEqual(data.completeness, CollectionState.COMPLETE.value)

    def test_psinfo_memory_is_ignored_when_cim_bytes_exist(self) -> None:
        data = build_overview_data(
            "ETSEADM-CAP10",
            enrich=_cim_etseadm(),
            psinfo=_psinfo_windows10(),
        )
        self.assertNotIn("32474", data.memory_summary)
        self.assertIn("GB", data.memory_summary)

    def test_null_and_missing_fields_are_unavailable(self) -> None:
        data = build_overview_data(
            "HOSTA",
            enrich={
                "ComputerSystem": {"Name": "HOSTA", "TotalPhysicalMemory": None},
                "OperatingSystem": {},
                "Processor": {},
                "Disks": [],
                "Network": {},
            },
        )
        self.assertEqual(data.os_name, UNAVAILABLE)
        self.assertEqual(data.os_version, UNAVAILABLE)
        self.assertEqual(data.os_build, UNAVAILABLE)
        self.assertEqual(data.os_architecture, UNAVAILABLE)
        self.assertEqual(data.username, UNAVAILABLE)
        self.assertEqual(data.memory_summary, UNAVAILABLE)
        self.assertEqual(data.cpu_cores, UNAVAILABLE)
        self.assertEqual(data.cpu_logical, UNAVAILABLE)
        self.assertEqual(data.cpu_clock, UNAVAILABLE)
        self.assertEqual(data.network_gateway, UNAVAILABLE)
        self.assertEqual(data.security_summary, UNAVAILABLE)
        self.assertEqual(data.updates_summary, UNAVAILABLE)
        self.assertNotEqual(data.memory_summary, "0")
        self.assertNotEqual(data.memory_summary, "0 MB")
        self.assertEqual(data.completeness, CollectionState.PARTIAL.value)

    def test_partial_cim_keeps_available_fields(self) -> None:
        data = build_overview_data(
            "HOSTA",
            enrich={
                "ComputerSystem": {
                    "Name": "HOSTA",
                    "Manufacturer": "Dell",
                    "TotalPhysicalMemory": CAP10_BYTES,
                },
                "OperatingSystem": {"Caption": "Microsoft Windows 11 Pro"},
            },
        )
        self.assertIn("Windows 11", data.os_name)
        self.assertEqual(data.manufacturer, "Dell")
        self.assertIn("GB", data.memory_summary)
        self.assertEqual(data.os_version, UNAVAILABLE)
        self.assertEqual(data.completeness, CollectionState.PARTIAL.value)

    def test_host_mismatch_is_flagged(self) -> None:
        enrich = _cim_etseadm()
        enrich["ComputerSystem"]["Name"] = "OUTRO-HOST"
        data = build_overview_data("ETSEADM-CAP10", enrich=enrich)
        self.assertTrue(data.host_mismatch)
        self.assertEqual(data.collected_host, "OUTRO-HOST")
        self.assertEqual(data.status, QueryStatus.ERROR)

    def test_fqdn_matches_short_name(self) -> None:
        self.assertTrue(inventory_hosts_match("ETSEADM-CAP10", "etseadm-cap10.tce.pa"))
        self.assertTrue(inventory_hosts_match(r"\\ETSEADM-CAP10", "ETSEADM-CAP10"))
        self.assertFalse(inventory_hosts_match("HOSTA", "HOSTB"))

    def test_computer_account_is_not_shown_as_user(self) -> None:
        enrich = _cim_etseadm()
        enrich["ComputerSystem"]["UserName"] = "ETSEADM-CAP10$"
        data = build_overview_data("ETSEADM-CAP10", enrich=enrich)
        self.assertEqual(data.username, UNAVAILABLE)

    def test_zero_hotfixes_is_valid(self) -> None:
        enrich = _cim_etseadm()
        enrich["Updates"] = {"Count": 0, "LastId": None, "LastDate": None}
        data = build_overview_data("ETSEADM-CAP10", enrich=enrich)
        self.assertEqual(data.updates_summary, "0 hotfixes")


class CollectOverviewTests(unittest.TestCase):
    def test_remote_error_returns_error_state(self) -> None:
        with patch(
            "remoteops.utils.inventory.overview.collect_psinfo_raw",
            return_value=("", "timeout remoto"),
        ), patch(
            "remoteops.utils.inventory.overview.run_remote_powershell",
            return_value=(None, "PsExec falhou"),
        ):
            data = collect_overview("HOSTA")
        self.assertEqual(data.status, QueryStatus.ERROR)
        self.assertEqual(data.completeness, CollectionState.ERROR.value)
        self.assertIn("PsExec", data.error)

    def test_host_returned_different_from_requested(self) -> None:
        enrich = _cim_etseadm()
        enrich["ComputerSystem"]["Name"] = "OUTRO-HOST"
        with patch(
            "remoteops.utils.inventory.overview.collect_psinfo_raw",
            return_value=("", ""),
        ), patch(
            "remoteops.utils.inventory.overview.run_remote_powershell",
            return_value=(enrich, ""),
        ):
            data = collect_overview("ETSEADM-CAP10")
        self.assertTrue(data.host_mismatch)
        self.assertEqual(data.status, QueryStatus.ERROR)
        self.assertNotEqual(data.os_name, "Microsoft Windows 11 Pro")
        self.assertNotIn("32474", data.memory_summary)

    def test_new_collect_uses_cim_not_stale_psinfo_memory(self) -> None:
        with patch(
            "remoteops.utils.inventory.overview.collect_psinfo_raw",
            return_value=("System information for \\\\ETSEADM-CAP10:\nPhysical memory: 32474 MB\n", ""),
        ), patch(
            "remoteops.utils.inventory.overview.run_remote_powershell",
            return_value=(_cim_etseadm(), ""),
        ):
            data = collect_overview("ETSEADM-CAP10")
        self.assertIn("Windows 11", data.os_name)
        self.assertNotIn("32474", data.memory_summary)
        self.assertIn("GB", data.memory_summary)


class OverviewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_app()

    def setUp(self) -> None:
        self.host_edit = QLineEdit("ETSEADM-CAP10")

    def tearDown(self) -> None:
        tab = getattr(self, "tab", None)
        if tab is not None:
            tab.shutdown(wait_ms=2000)
            tab.deleteLater()
        self.app.processEvents()

    def _make_tab(self, service: InventoryService | None = None) -> InventarioTab:
        self.tab = InventarioTab(host_source=self.host_edit, service=service)
        return self.tab

    def _full_overview(self, host: str = "ETSEADM-CAP10") -> OverviewData:
        enrich = _cim_etseadm()
        enrich["ComputerSystem"]["Name"] = host
        if "UserName" in enrich["ComputerSystem"]:
            enrich["ComputerSystem"]["UserName"] = rf"{host}\tce_admin"
        return build_overview_data(host, enrich=enrich)

    def test_ui_shows_os_version_build_arch_user_network_security(self) -> None:
        tab = self._make_tab()
        tab._render_section(InventorySection.OVERVIEW, self._full_overview())
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("Windows 11 Pro", texts)
        self.assertNotIn("Windows 10 Pro", texts)
        self.assertIn("10.0.26100", texts)
        self.assertIn("26100", texts)
        self.assertIn("64 bits", texts)
        self.assertIn("tce_admin", texts)
        self.assertIn("tce.pa", texts)
        self.assertIn("192.168.4.119", texts)
        self.assertIn("100 Mbps", texts)
        self.assertIn("192.168.4.1", texts)
        self.assertIn("Núcleos: 6", texts)
        self.assertIn("Lógicos: 6", texts)
        self.assertIn("3096 MHz", texts)
        self.assertIn("NTFS", texts)
        self.assertNotIn("Ver seção", texts)
        self.assertIn("Defender", texts)
        self.assertIn("hotfix", texts)
        self.assertNotIn("32474", texts)
        self.assertFalse(_has_spinner(tab, InventorySection.OVERVIEW))

    def test_host_change_clears_previous_overview(self) -> None:
        self.host_edit.setText("HOSTA")
        tab = self._make_tab()
        tab._render_section(InventorySection.OVERVIEW, self._full_overview("HOSTA"))
        self.assertIn("Windows 11", _section_texts(tab, InventorySection.OVERVIEW))
        self.host_edit.setText("HOSTB")
        self.app.processEvents()
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertNotIn("Windows 11", texts)
        self.assertNotIn("tce_admin", texts)
        self.assertNotIn("192.168.4.119", texts)

    def test_new_collect_replaces_previous_values(self) -> None:
        self.host_edit.setText("HOSTA")
        first = self._full_overview("HOSTA")
        second = build_overview_data(
            "HOSTA",
            enrich={
                "ComputerSystem": {
                    "Name": "HOSTA",
                    "Manufacturer": "Lenovo",
                    "Model": "ThinkCentre",
                    "TotalPhysicalMemory": CAP10_BYTES,
                    "UserName": r"HOSTA\novo",
                },
                "OperatingSystem": {
                    "Caption": "Microsoft Windows 10 Pro",
                    "Version": "10.0.19045",
                    "BuildNumber": "19045",
                    "OSArchitecture": "64-bit",
                },
                "Processor": {
                    "Name": "Intel Xeon",
                    "NumberOfCores": 8,
                    "NumberOfLogicalProcessors": 16,
                    "MaxClockSpeed": 2500,
                },
                "Disks": [{"DeviceID": "C:", "FileSystem": "NTFS", "Size": 100, "FreeSpace": 50}],
                "Network": {"IPAddress": "10.0.0.8", "Gateway": "10.0.0.1", "Speed": 1_000_000_000},
                "Updates": {"Count": 3, "LastId": "KB1"},
                "Security": {"UAC": 1},
            },
        )
        tab = self._make_tab()
        query = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = query
        tab._render_section(InventorySection.OVERVIEW, first)
        self.assertIn("Windows 11", _section_texts(tab, InventorySection.OVERVIEW))

        tab._service.invalidate_section(InventorySection.OVERVIEW)
        new_query = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = new_query
        tab._on_collect_ok(
            SectionResult(section=InventorySection.OVERVIEW, payload=second, query=new_query)
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("Windows 10 Pro", texts)
        self.assertNotIn("Windows 11", texts)
        self.assertIn(r"HOSTA\novo", texts)
        self.assertIn("10.0.0.8", texts)
        self.assertFalse(_has_loading(tab, InventorySection.OVERVIEW))
        self.assertFalse(_has_spinner(tab, InventorySection.OVERVIEW))

    def test_host_mismatch_does_not_render_foreign_data(self) -> None:
        tab = self._make_tab()
        query = tab._service.begin_query("ETSEADM-CAP10", InventorySection.OVERVIEW)
        tab._active_query = query
        payload = OverviewData(
            hostname="ETSEADM-CAP10",
            collected_host="OUTRO-HOST",
            requested_host="ETSEADM-CAP10",
            host_mismatch=True,
            status=QueryStatus.ERROR,
            completeness=CollectionState.ERROR.value,
            error="O host retornado (OUTRO-HOST) não corresponde ao host solicitado (ETSEADM-CAP10).",
            os_name="Microsoft Windows 11 Pro",
            memory_summary="7.87 GB",
        )
        tab._on_collect_ok(
            SectionResult(section=InventorySection.OVERVIEW, payload=payload, query=query)
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("não corresponde", texts)
        self.assertNotIn("7.87 GB", texts)

    def test_remote_error_renders_error_without_spinner(self) -> None:
        self.host_edit.setText("HOSTA")
        tab = self._make_tab()
        query = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = query
        tab._set_loading(True, "Coletando...")
        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.OVERVIEW,
                payload=OverviewData(
                    status=QueryStatus.ERROR,
                    completeness=CollectionState.ERROR.value,
                    error="Falha remota de HOSTA",
                ),
                query=query,
            )
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("Falha remota de HOSTA", texts)
        self.assertFalse(_has_spinner(tab, InventorySection.OVERVIEW))
        self.assertFalse(_has_loading(tab, InventorySection.OVERVIEW))

    def test_unavailable_fields_are_labeled(self) -> None:
        tab = self._make_tab()
        tab._render_section(
            InventorySection.OVERVIEW,
            OverviewData(
                hostname="HOSTA",
                os_name=UNAVAILABLE,
                memory_summary=UNAVAILABLE,
                completeness=CollectionState.PARTIAL.value,
                status=QueryStatus.OK,
            ),
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("Não disponível", texts)
        self.assertIn("Coleta parcial", texts)
        self.assertNotIn("Ver seção", texts)

    def test_stale_host_result_is_not_mixed(self) -> None:
        self.host_edit.setText("HOSTA")
        tab = self._make_tab()
        first = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = first
        tab._render_section(InventorySection.OVERVIEW, self._full_overview("HOSTA"))
        self.host_edit.setText("HOSTB")
        self.app.processEvents()
        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.OVERVIEW,
                payload=self._full_overview("HOSTA"),
                query=first,
            )
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertNotIn("tce_admin", texts)
        self.assertNotIn("192.168.4.119", texts)


class OverviewScriptContractTests(unittest.TestCase):
    def test_overview_script_queries_required_cim_classes(self) -> None:
        from remoteops.utils.inventory.collectors import _SCRIPT_OVERVIEW_ENRICH

        script = _SCRIPT_OVERVIEW_ENRICH
        self.assertIn("Win32_ComputerSystem", script)
        self.assertIn("TotalPhysicalMemory", script)
        self.assertIn("UserName", script)
        self.assertIn("Win32_OperatingSystem", script)
        self.assertIn("Caption", script)
        self.assertIn("BuildNumber", script)
        self.assertIn("OSArchitecture", script)
        self.assertIn("Win32_Processor", script)
        self.assertIn("NumberOfLogicalProcessors", script)
        self.assertIn("MaxClockSpeed", script)
        self.assertIn("Win32_LogicalDisk", script)
        self.assertIn("Win32_NetworkAdapterConfiguration", script)
        self.assertIn("DefaultIPGateway", script)
        self.assertIn("Write-RemoteOpsJson", script)


if __name__ == "__main__":
    unittest.main()
