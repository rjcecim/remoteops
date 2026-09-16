"""Interface do inventário: resultados obsoletos não alteram a solicitação atual."""

from __future__ import annotations

import sys
import threading
import unittest
from typing import List, Optional

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget

from remoteops.ui.inventory.nav import all_sections
from remoteops.ui.inventory.widgets import VideoPanel
from remoteops.ui.tabs.inventario import InventarioTab, _InventoryWorker
from remoteops.utils.inventory.models import (
    InventorySection,
    MonitorInfo,
    OverviewData,
    QueryContext,
    QueryStatus,
    SectionResult,
    SystemData,
    VideoAdapter,
    VideoData,
)
from remoteops.utils.inventory.service import InventoryService


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _overview(host: str) -> OverviewData:
    return OverviewData(
        hostname=host,
        manufacturer=f"MFR-{host}",
        model=f"MDL-{host}",
        os_summary=f"OS-{host}",
        status=QueryStatus.OK,
    )


def _section_texts(tab: InventarioTab, section: InventorySection) -> str:
    page = tab._section_pages[section]
    return " ".join(lbl.text() for lbl in page.findChildren(QLabel) if lbl.text())


def _has_idle(tab: InventarioTab, section: InventorySection) -> bool:
    page = tab._section_pages[section]
    return page.findChild(QWidget, "inventoryIdleState") is not None


class _CollectorGate:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.payloads: list[object] = []

    def add(self, payload: object) -> None:
        self.payloads.append(payload)

    def collect(self, host: str, **_kwargs: object) -> object:
        if not self.payloads:
            raise AssertionError(f"coletor sem payload para {host}")
        payload = self.payloads.pop(0)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("coletor não foi liberado")
        return payload


class _LogSink:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def append_log(self, msg: str) -> None:
        self.lines.append(msg)


def _wait_thread_finished(app: QApplication, worker: _InventoryWorker, timeout_ms: int = 5000) -> None:
    if not worker.isRunning():
        app.processEvents()
        return
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    app.processEvents()


class InventarioTabIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_app()

    def setUp(self) -> None:
        self.host_edit = QLineEdit("HOSTA")
        self.log = _LogSink()

    def tearDown(self) -> None:
        tab = getattr(self, "tab", None)
        if tab is not None:
            tab.shutdown(wait_ms=2000)
            tab.deleteLater()
        self.app.processEvents()

    def _make_tab(self, service: Optional[InventoryService] = None) -> InventarioTab:
        self.tab = InventarioTab(
            host_source=self.host_edit,
            log_output=self.log,
            service=service,
        )
        return self.tab

    def test_old_result_after_host_change_is_ignored(self) -> None:
        gate = _CollectorGate()
        gate.add(_overview("HOSTA"))
        service = InventoryService(collectors={InventorySection.OVERVIEW: gate.collect})
        tab = self._make_tab(service)
        tab._load_section(InventorySection.OVERVIEW)
        self.assertTrue(gate.started.wait(timeout=5))
        old_worker = tab._worker
        self.assertIsNotNone(old_worker)

        self.host_edit.setText("HOSTB")
        self.app.processEvents()

        gate.release.set()
        _wait_thread_finished(self.app, old_worker)

        self.assertIsNone(service.cache.get(InventorySection.OVERVIEW))
        self.assertEqual(service.cache.host, "hostb")
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertNotIn("MFR-HOSTA", texts)
        self.assertNotIn("MDL-HOSTA", texts)
        self.assertTrue(_has_idle(tab, InventorySection.OVERVIEW))
        self.assertIn("HOSTB", tab._host_chip.toolTip())

    def test_a_b_a_old_result_does_not_replace_current(self) -> None:
        tab = self._make_tab()
        first = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = first
        self.host_edit.setText("HOSTB")
        self.app.processEvents()
        self.host_edit.setText("HOSTA")
        self.app.processEvents()
        second = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = second
        tab._render_section(InventorySection.OVERVIEW, _overview("NEW-A"))

        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.OVERVIEW,
                payload=_overview("OLD-A"),
                query=first,
            )
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("NEW-A", texts)
        self.assertNotIn("OLD-A", texts)
        self.assertNotIn("MFR-OLD-A", texts)

    def test_older_refresh_does_not_overwrite_newer_ui(self) -> None:
        tab = self._make_tab()
        first = tab._service.begin_query("HOSTA", InventorySection.SYSTEM)
        second = tab._service.begin_query("HOSTA", InventorySection.SYSTEM)
        tab._active_query = second
        tab._render_section(
            InventorySection.SYSTEM,
            SystemData(rows=[("Campo", "NOVO", "")], status=QueryStatus.OK),
        )
        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.SYSTEM,
                payload=SystemData(rows=[("Campo", "ANTIGO", "")], status=QueryStatus.OK),
                query=first,
            )
        )
        texts = _section_texts(tab, InventorySection.SYSTEM)
        self.assertIn("NOVO", texts)
        self.assertNotIn("ANTIGO", texts)

    def test_late_error_does_not_replace_current_state(self) -> None:
        tab = self._make_tab()
        stale = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        current = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = current
        tab._set_loading(True, "Coletando atual...")
        tab._render_section(InventorySection.OVERVIEW, _overview("ATUAL"))
        status_before = tab._status_lbl.text()

        tab._on_collect_err(
            SectionResult(
                section=InventorySection.OVERVIEW,
                status=QueryStatus.ERROR,
                error="falha antiga de HOSTA",
                query=stale,
            )
        )
        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertIn("ATUAL", texts)
        self.assertNotIn("falha antiga de HOSTA", texts)
        self.assertNotIn("Não foi possível consultar", texts)
        self.assertTrue(all("falha antiga" not in line for line in self.log.lines))
        self.assertEqual(tab._active_query, current)
        self.assertEqual(tab._status_lbl.text(), status_before)

    def test_late_finished_preserves_current_worker(self) -> None:
        tab = self._make_tab()
        first = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        old = _InventoryWorker(tab._service, InventorySection.OVERVIEW, "HOSTA", first)
        tab._worker = old
        tab._active_query = first
        tab._set_loading(True, "Coletando antigo...")

        second = tab._service.begin_query("HOSTA", InventorySection.HARDWARE)
        new = _InventoryWorker(tab._service, InventorySection.HARDWARE, "HOSTA", second)
        tab._retire_worker()
        tab._worker = new
        tab._active_query = second
        tab._set_loading(True, "Coletando atual...")

        self.assertIn(old, tab._stale_workers)
        tab._on_worker_finished(old)
        self.app.processEvents()

        self.assertIs(tab._worker, new)
        self.assertNotIn(old, tab._stale_workers)
        self.assertEqual(tab._active_query, second)
        self.assertFalse(tab.refresh_btn.isEnabled())
        self.assertIn("Coletando atual", tab._status_lbl.text())

    def test_displayed_data_is_cleared_on_host_change(self) -> None:
        tab = self._make_tab()
        tab._render_section(InventorySection.OVERVIEW, _overview("HOSTA"))
        self.assertIn("MFR-HOSTA", _section_texts(tab, InventorySection.OVERVIEW))

        self.host_edit.setText("HOSTB")
        self.app.processEvents()

        texts = _section_texts(tab, InventorySection.OVERVIEW)
        self.assertNotIn("MFR-HOSTA", texts)
        self.assertNotIn("MDL-HOSTA", texts)
        self.assertTrue(_has_idle(tab, InventorySection.OVERVIEW))
        self.assertIn("HOSTB", tab._host_chip.toolTip())
        self.assertTrue(tab.refresh_btn.isEnabled())

    def test_successful_query_renders_and_reuses_cache(self) -> None:
        calls = {"n": 0}

        def collect(host: str, **_kwargs: object) -> OverviewData:
            calls["n"] += 1
            return _overview(host)

        service = InventoryService(collectors={InventorySection.OVERVIEW: collect})
        tab = self._make_tab(service)
        query = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        result = tab._service.collect_section(
            InventorySection.OVERVIEW, "HOSTA", query=query
        )
        tab._active_query = query
        tab._on_collect_ok(result)
        self.assertIn("MFR-HOSTA", _section_texts(tab, InventorySection.OVERVIEW))

        tab._current_section = InventorySection.SYSTEM
        tab._on_section_selected(InventorySection.OVERVIEW)
        self.assertEqual(calls["n"], 1)
        self.assertIn("MFR-HOSTA", _section_texts(tab, InventorySection.OVERVIEW))

    def test_manual_refresh_starts_new_request(self) -> None:
        gate = _CollectorGate()
        gate.add(_overview("HOSTA"))
        service = InventoryService(collectors={InventorySection.OVERVIEW: gate.collect})
        tab = self._make_tab(service)
        first = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = first
        tab.refresh_current()
        self.app.processEvents()
        self.assertIsNotNone(tab._active_query)
        self.assertNotEqual(tab._active_query.request_id, first.request_id)
        self.assertEqual(tab._active_query.host, "hosta")
        self.assertTrue(gate.started.wait(timeout=5))
        gate.release.set()
        if tab._worker is not None:
            _wait_thread_finished(self.app, tab._worker)

    def test_section_navigation_does_not_apply_other_section_result(self) -> None:
        tab = self._make_tab()
        overview_query = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        hardware_query = tab._service.begin_query("HOSTA", InventorySection.HARDWARE)
        tab._active_query = hardware_query
        tab._current_section = InventorySection.HARDWARE
        tab._reset_section_page(InventorySection.OVERVIEW)

        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.OVERVIEW,
                payload=_overview("HOSTA"),
                query=overview_query,
            )
        )
        self.assertTrue(_has_idle(tab, InventorySection.OVERVIEW))
        self.assertNotIn("MFR-HOSTA", _section_texts(tab, InventorySection.OVERVIEW))

    def test_host_change_without_active_query_clears_pages(self) -> None:
        tab = self._make_tab()
        tab._render_section(InventorySection.SYSTEM, SystemData(rows=[("SO", "WinA", "")]))
        self.host_edit.setText("HOSTB")
        self.app.processEvents()
        self.assertNotIn("WinA", _section_texts(tab, InventorySection.SYSTEM))
        self.assertTrue(_has_idle(tab, InventorySection.SYSTEM))
        self.assertIsNone(tab._service.cache.get(InventorySection.SYSTEM))

    def test_closed_tab_ignores_previous_instance_result(self) -> None:
        tab = self._make_tab()
        query = tab._service.begin_query("HOSTA", InventorySection.OVERVIEW)
        tab._active_query = query
        tab.shutdown(wait_ms=100)
        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.OVERVIEW,
                payload=_overview("HOSTA"),
                query=query,
            )
        )
        self.assertTrue(tab._closed)
        self.assertIsNone(tab._active_query)

        reopened = InventarioTab(host_source=self.host_edit, service=InventoryService())
        self.addCleanup(lambda: reopened.shutdown(wait_ms=1000))
        for section in all_sections():
            self.assertNotIn("MFR-HOSTA", _section_texts(reopened, section))

    def test_query_context_mismatch_by_generation_is_rejected(self) -> None:
        tab = self._make_tab()
        current = QueryContext("hosta", InventorySection.OVERVIEW, 2, 2)
        stale = QueryContext("hosta", InventorySection.OVERVIEW, 1, 1)
        tab._active_query = current
        tab._on_collect_ok(
            SectionResult(
                section=InventorySection.OVERVIEW,
                payload=_overview("STALE"),
                query=stale,
            )
        )
        self.assertNotIn("STALE", _section_texts(tab, InventorySection.OVERVIEW))


class VideoPanelMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _ensure_app()

    def test_renders_manufacturer_model_and_serial(self) -> None:
        panel = VideoPanel(
            VideoData(
                adapters=[VideoAdapter(name="Intel(R) UHD Graphics 630")],
                monitors=[
                    MonitorInfo(manufacturer="HPN", model="HP P24a G4", serial="BRC22707B4"),
                    MonitorInfo(manufacturer="HPN", model="HP E24mv G4", serial="CNC2271M97"),
                ],
            )
        )
        text = " ".join(lbl.text() for lbl in panel.findChildren(QLabel) if lbl.text())
        self.assertIn("HP P24a G4", text)
        self.assertIn("BRC22707B4", text)
        self.assertIn("HP E24mv G4", text)
        self.assertIn("CNC2271M97", text)
        self.assertIn("HPN", text)
        self.assertIn("Monitores", text)
        self.assertIn("2 monitores", text)
        panel.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
