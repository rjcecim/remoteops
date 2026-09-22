"""Aba Sistema: overlay CIM para SO e memória, sem inventar valores."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QLabel

from remoteops.ui.inventory.widgets import SystemPanel
from remoteops.utils.inventory.collectors import collect_system, overlay_system_cim
from remoteops.utils.inventory.models import QueryStatus
from remoteops.utils.psinfo import format_system_display, parse_psinfo_output

CAP10_BYTES = 8_455_716_864


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _psinfo_cap10() -> str:
    return """
System information for \\\\ETSEADM-CAP10:

Uptime:                    4 days 0 hours 0 minutes 0 seconds
Kernel version:            Windows 10 Pro, Multiprocessor Free
Kernel build number:       26100
Product type:              Professional
Product version:           6.3
Processors:                6
Processor type:            Intel(R) Core(TM) i5-8600 CPU @ 3.10GHz
Processor speed:           3.1 GHz
Physical memory:           32474 MB
System root:               C:\\WINDOWS
"""


def _cim_win11(name: str = "ETSEADM-CAP10") -> dict:
    return {
        "ComputerSystem": {
            "Name": name,
            "TotalPhysicalMemory": CAP10_BYTES,
        },
        "OperatingSystem": {
            "Caption": "Microsoft Windows 11 Pro",
            "Version": "10.0.26100",
            "BuildNumber": "26100",
            "OSArchitecture": "64-bit",
        },
    }


class OverlaySystemCimTests(unittest.TestCase):
    def test_windows_11_caption_replaces_psinfo_product_name(self) -> None:
        parsed = parse_psinfo_output(_psinfo_cap10(), host="ETSEADM-CAP10")
        system = dict(parsed.system)
        self.assertTrue(overlay_system_cim(system, _cim_win11(), "ETSEADM-CAP10"))
        self.assertEqual(system["OS caption"], "Microsoft Windows 11 Pro")
        self.assertEqual(system["OS version"], "10.0.26100")
        self.assertEqual(system["OS build"], "26100")
        self.assertEqual(system["OS architecture"], "64 bits")
        self.assertNotIn("32474", system["Physical memory"])
        self.assertIn("GB", system["Physical memory"])

    def test_windows_10_caption_is_kept(self) -> None:
        enrich = _cim_win11("HOST10")
        enrich["OperatingSystem"]["Caption"] = "Microsoft Windows 10 Pro"
        enrich["OperatingSystem"]["Version"] = "10.0.19045"
        enrich["OperatingSystem"]["BuildNumber"] = "19045"
        system: dict[str, str] = {}
        overlay_system_cim(system, enrich, "HOST10")
        self.assertEqual(system["OS caption"], "Microsoft Windows 10 Pro")
        self.assertNotIn("Windows 11", system["OS caption"])

    def test_missing_memory_is_not_zero(self) -> None:
        system = {"Physical memory": "32474 MB"}
        overlay_system_cim(
            system,
            {
                "ComputerSystem": {"Name": "HOSTA", "TotalPhysicalMemory": None},
                "OperatingSystem": {"Caption": "Microsoft Windows 11 Pro"},
            },
            "HOSTA",
        )
        self.assertEqual(system["Physical memory"], "32474 MB")
        self.assertNotEqual(system.get("Physical memory"), "0")
        self.assertNotEqual(system.get("Physical memory"), "0 MB")

    def test_host_mismatch_does_not_overlay(self) -> None:
        system = {"Physical memory": "32474 MB", "Kernel version": "Windows 10 Pro, Multiprocessor Free"}
        applied = overlay_system_cim(system, _cim_win11("OUTRO-HOST"), "ETSEADM-CAP10")
        self.assertFalse(applied)
        self.assertNotIn("OS caption", system)
        self.assertEqual(system["Physical memory"], "32474 MB")

    def test_empty_enrich_leaves_psinfo(self) -> None:
        system = {"Physical memory": "32474 MB"}
        self.assertFalse(overlay_system_cim(system, None, "HOSTA"))
        self.assertEqual(system["Physical memory"], "32474 MB")


class CollectSystemTests(unittest.TestCase):
    def test_collect_prefers_cim_os_and_memory(self) -> None:
        with patch(
            "remoteops.utils.inventory.collectors.collect_psinfo_raw",
            return_value=(_psinfo_cap10(), ""),
        ), patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(_cim_win11(), ""),
        ), patch(
            "remoteops.utils.inventory.collectors.patch_system_uptime",
        ):
            data = collect_system("ETSEADM-CAP10")
        self.assertEqual(data.status, QueryStatus.OK)
        labels = {label: value for _group, label, value in data.rows}
        self.assertEqual(labels["Sistema operacional"], "Microsoft Windows 11 Pro")
        self.assertEqual(labels["Versão"], "10.0.26100")
        self.assertEqual(labels["Build"], "26100")
        self.assertEqual(labels["Arquitetura"], "64 bits")
        self.assertNotIn("32474", labels["Memória física"])
        self.assertIn("GB", labels["Memória física"])
        self.assertIn("Win32_OperatingSystem", data.source_note)

    def test_remote_error_without_sources(self) -> None:
        with patch(
            "remoteops.utils.inventory.collectors.collect_psinfo_raw",
            return_value=("", "timeout"),
        ), patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(None, "PsExec falhou"),
        ):
            data = collect_system("HOSTA")
        self.assertEqual(data.status, QueryStatus.ERROR)
        self.assertTrue(data.error)

    def test_cim_only_when_psinfo_fails(self) -> None:
        with patch(
            "remoteops.utils.inventory.collectors.collect_psinfo_raw",
            return_value=("", "PsInfo sem dados."),
        ), patch(
            "remoteops.utils.inventory.collectors.run_remote_powershell",
            return_value=(_cim_win11(), ""),
        ):
            data = collect_system("ETSEADM-CAP10")
        labels = {label: value for _group, label, value in data.rows}
        self.assertEqual(data.status, QueryStatus.OK)
        self.assertEqual(labels["Sistema operacional"], "Microsoft Windows 11 Pro")
        self.assertIn("GB", labels["Memória física"])


class SystemPanelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_app()

    def test_hero_uses_cim_caption_not_psinfo_kernel_string(self) -> None:
        parsed = parse_psinfo_output(_psinfo_cap10(), host="ETSEADM-CAP10")
        system = dict(parsed.system)
        overlay_system_cim(system, _cim_win11(), "ETSEADM-CAP10")
        rows = format_system_display(system, host="ETSEADM-CAP10")
        panel = SystemPanel(rows, source_note="Fonte: Win32_OperatingSystem")
        texts = " ".join(lbl.text() for lbl in panel.findChildren(QLabel) if lbl.text())
        self.assertIn("Microsoft Windows 11 Pro", texts)
        self.assertIn("10.0.26100", texts)
        self.assertIn("64 bits", texts)
        self.assertNotIn("32474", texts)
        self.assertNotIn("Windows 10 Pro, Multiprocessor Free", texts)
        panel.deleteLater()
        self.app.processEvents()

    def test_script_queries_os_and_memory(self) -> None:
        from remoteops.utils.inventory.collectors import _SCRIPT_SYSTEM_ENRICH

        self.assertIn("Win32_OperatingSystem", _SCRIPT_SYSTEM_ENRICH)
        self.assertIn("Caption", _SCRIPT_SYSTEM_ENRICH)
        self.assertIn("TotalPhysicalMemory", _SCRIPT_SYSTEM_ENRICH)
        self.assertIn("Write-RemoteOpsJson", _SCRIPT_SYSTEM_ENRICH)


if __name__ == "__main__":
    unittest.main()
