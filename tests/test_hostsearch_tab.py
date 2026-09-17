"""Pesquisa de Host: aba independente, filtro ao vivo, usuário ativo e olho → PsExec."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QToolButton

from remoteops.ui.tabs.hostsearch import COL_HOSTNAME, COL_IP, COL_USER, HostSearchTab
from remoteops.ui.tabs.psexec import PsExecTab
from remoteops.ui.widgets.selector import FileSelectorWidget
from remoteops.utils.hostsearch import (
    EMPTY_CELL,
    display_hostname,
    format_active_session_users,
    lookup_active_session_users,
    psexec_target,
    require_active_network_range_message,
    row_matches_filter,
)
from remoteops.utils.sessions import RemoteSession


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _eye_button(tab: HostSearchTab, row: int) -> QToolButton:
    cell = tab.table.cellWidget(row, 3)
    btn = cell.findChild(QToolButton) if cell is not None else None
    if btn is None:
        raise AssertionError(f"olho ausente na linha {row}")
    return btn


class HostSearchHelpersTests(unittest.TestCase):
    def test_hostname_dash_when_equal_to_ip(self) -> None:
        self.assertEqual(display_hostname("10.0.0.8", "10.0.0.8"), EMPTY_CELL)
        self.assertEqual(display_hostname("10.0.0.8", "10.0.0.8 "), EMPTY_CELL)
        self.assertEqual(display_hostname("10.0.0.8", ""), EMPTY_CELL)

    def test_hostname_shown_when_distinct(self) -> None:
        self.assertEqual(display_hostname("10.0.0.8", "PC-LAB"), "PC-LAB")

    def test_psexec_prefers_hostname_else_ip(self) -> None:
        self.assertEqual(psexec_target("10.0.0.8", "PC-LAB"), "PC-LAB")
        self.assertEqual(psexec_target("10.0.0.8", "10.0.0.8"), "10.0.0.8")
        self.assertEqual(psexec_target("10.0.0.8", ""), "10.0.0.8")

    def test_filter_matches_ip_hostname_or_user_without_scan(self) -> None:
        self.assertTrue(row_matches_filter("10.0.0.8", "PC-LAB", r"ACME\bob", "lab"))
        self.assertTrue(row_matches_filter("10.0.0.8", "PC-LAB", r"ACME\bob", "10.0.0"))
        self.assertTrue(row_matches_filter("10.0.0.8", "PC-LAB", r"ACME\bob", "bob"))
        self.assertFalse(row_matches_filter("10.0.0.8", "PC-LAB", r"ACME\bob", "alice"))
        self.assertTrue(row_matches_filter("10.0.0.8", "10.0.0.8", EMPTY_CELL, ""))

    def test_active_users_domain_backslash_user(self) -> None:
        sessions = [
            RemoteSession(1, "rdp-tcp", "bob", "Ativa", "ACME"),
            RemoteSession(2, "console", "idle", "Desconectada", "ACME"),
            RemoteSession(3, "services", "", "Listen", ""),
        ]
        self.assertEqual(format_active_session_users(sessions), r"ACME\bob")

    def test_ativo_numeric_login_is_listed(self) -> None:
        sessions = [
            RemoteSession(2, "rdp-tcp#3", "1234567", "Ativo", "TCE-PA"),
        ]
        self.assertEqual(format_active_session_users(sessions), r"TCE-PA\1234567")
        self.assertEqual(format_active_session_users([]), EMPTY_CELL)
        disconnected = [RemoteSession(4, "rdp-tcp", "bob", "Desconectada", "ACME")]
        self.assertEqual(format_active_session_users(disconnected), EMPTY_CELL)

    def test_lookup_failure_is_dash(self) -> None:
        with patch(
            "remoteops.utils.hostsearch.list_remote_sessions",
            return_value=([], "falha WTS"),
        ):
            self.assertEqual(lookup_active_session_users("10.0.0.8", "u", "p"), EMPTY_CELL)

    def test_range_required_no_hosts_json_fallback(self) -> None:
        self.assertIsNone(require_active_network_range_message("network"))
        self.assertIn("hosts.json", require_active_network_range_message("json") or "")
        self.assertEqual(
            require_active_network_range_message("invalid", "IP inválido."),
            "IP inválido.",
        )


class HostSearchScanTests(unittest.TestCase):
    def test_scan_hits_list_each_ip_without_icmp(self) -> None:
        from remoteops.utils.network_scan import scan_windows_host_hits

        def probe(ip, should_cancel=None):
            if ip.endswith(".2"):
                return "PC-02"
            if ip.endswith(".3"):
                return ip
            return None

        with patch("remoteops.utils.network_scan.probe_windows_host", side_effect=probe):
            with patch("remoteops.utils.ping.ping_host_status") as ping:
                hits = scan_windows_host_hits(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        ping.assert_not_called()
        by_ip = dict(hits)
        self.assertEqual(by_ip["10.0.0.2"], "PC-02")
        self.assertEqual(by_ip["10.0.0.3"], "10.0.0.3")
        self.assertNotIn("10.0.0.1", by_ip)


class HostSearchTabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _ensure_app()
        self.tab = HostSearchTab()

    def tearDown(self) -> None:
        self.tab.shutdown(wait_ms=500)
        self.tab.deleteLater()
        self.app.processEvents()

    def test_tab_columns_and_hostname_dash(self) -> None:
        self.assertEqual(
            [self.tab.table.horizontalHeaderItem(i).text() for i in range(3)],
            ["IP", "Hostname", "Usuário ativo"],
        )
        self.tab.add_discovered_host("192.168.1.10", "192.168.1.10", queue_user_lookup=False)
        self.tab.add_discovered_host("192.168.1.11", "HOST-11", queue_user_lookup=False)
        self.assertEqual(self.tab.table.item(0, COL_IP).text(), "192.168.1.10")
        self.assertEqual(self.tab.table.item(0, COL_HOSTNAME).text(), EMPTY_CELL)
        self.assertEqual(self.tab.table.item(0, COL_USER).text(), EMPTY_CELL)
        self.assertEqual(self.tab.table.item(1, COL_HOSTNAME).text(), "HOST-11")
        self.assertIsNotNone(_eye_button(self.tab, 0))

    def test_filter_hides_rows_without_new_scan(self) -> None:
        self.tab.add_discovered_host("10.0.0.1", "ALPHA", queue_user_lookup=False)
        self.tab.add_discovered_host("10.0.0.2", "BETA", queue_user_lookup=False)
        self.tab.set_active_user("10.0.0.2", r"ACME\carol")
        started = self.tab._scan_started_count
        self.tab.filter_edit.setText("carol")
        self.assertEqual(self.tab._scan_started_count, started)
        self.assertTrue(self.tab.table.isRowHidden(0))
        self.assertFalse(self.tab.table.isRowHidden(1))
        self.tab.filter_edit.setText("10.0.0.1")
        self.assertEqual(self.tab._scan_started_count, started)
        self.assertFalse(self.tab.table.isRowHidden(0))
        self.assertTrue(self.tab.table.isRowHidden(1))
        self.tab.filter_edit.setText("alpha")
        self.assertFalse(self.tab.table.isRowHidden(0))
        self.assertTrue(self.tab.table.isRowHidden(1))

    def test_user_column_updates_without_blocking_list(self) -> None:
        self.tab.add_discovered_host("10.9.9.9", "NINE", queue_user_lookup=False)
        self.assertEqual(self.tab.table.item(0, COL_USER).text(), EMPTY_CELL)
        self.tab.set_active_user("10.9.9.9", r"DOM\jane")
        self.assertEqual(self.tab.table.item(0, COL_USER).text(), r"DOM\jane")
        self.assertEqual(self.tab.table.item(0, COL_IP).text(), "10.9.9.9")

    def test_user_lookup_runs_after_hit_not_in_probe(self) -> None:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        self.tab._search_generation = 7
        self.tab._session_pool = ThreadPoolExecutor(max_workers=1)
        gate = threading.Event()

        def _lookup(host, user="", password="", hostname=""):
            gate.wait(timeout=2)
            return r"ACME\bob"

        with patch(
            "remoteops.ui.tabs.hostsearch.lookup_active_session_users",
            side_effect=_lookup,
        ) as lookup:
            loop = QEventLoop()
            QTimer.singleShot(3000, loop.quit)
            self.tab._user_bridge.userReady.connect(lambda *_a: loop.quit())
            self.tab.add_discovered_host("10.0.0.8", "HOST8", queue_user_lookup=True)
            self.assertEqual(self.tab.table.item(0, COL_IP).text(), "10.0.0.8")
            self.assertEqual(self.tab.table.item(0, COL_USER).text(), EMPTY_CELL)
            gate.set()
            loop.exec()
            self.app.processEvents()
            lookup.assert_called()
            self.assertEqual(lookup.call_args.args[0], "10.0.0.8")
            self.assertEqual(lookup.call_args.kwargs.get("hostname"), "HOST8")
        self.assertEqual(self.tab.table.item(0, COL_USER).text(), r"ACME\bob")

    def test_start_search_requires_ip_range_not_hosts_json(self) -> None:
        with patch(
            "remoteops.ui.tabs.hostsearch.network_range_search_mode",
            return_value=("json", None, 0),
        ), patch.object(QMessageBox, "warning", return_value=0) as warn, patch(
            "remoteops.utils.hosts.load_hosts_file"
        ) as load:
            self.tab.start_search()
        load.assert_not_called()
        warn.assert_called()
        self.assertEqual(self.tab._scan_started_count, 0)
        self.assertIsNone(self.tab._scan_worker)

    def test_eye_emits_hostname_or_ip(self) -> None:
        received: list[str] = []
        self.tab.openHostRequested.connect(received.append)
        self.tab.add_discovered_host("10.1.2.3", "PC-03", queue_user_lookup=False)
        self.tab.add_discovered_host("10.1.2.4", "10.1.2.4", queue_user_lookup=False)
        _eye_button(self.tab, 0).click()
        _eye_button(self.tab, 1).click()
        self.assertEqual(received, ["PC-03", "10.1.2.4"])


class HostSearchHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _ensure_app()

    def test_host_button_beside_lupa_without_replacing_it(self) -> None:
        selector = FileSelectorWidget()
        self.assertEqual(selector.search_button.text(), "\uE721")
        self.assertIn("aplicativos", selector.search_button.toolTip().casefold())
        self.assertEqual(selector.host_search_button.text(), "\uE968")
        self.assertEqual(selector.host_search_button.toolTip(), "Pesquisar hosts")
        header = selector._header_widget.layout()
        search_idx = header.indexOf(selector.search_button)
        host_idx = header.indexOf(selector.host_search_button)
        self.assertEqual(host_idx, search_idx + 1)
        selector.deleteLater()


class HostSearchEyeToPsExecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _ensure_app()

    def test_eye_fills_psexec_switches_tab_and_focuses(self) -> None:
        from remoteops.ui.main_window import MainWindow

        win = MainWindow()
        win.show()
        self.app.processEvents()
        try:
            win.open_appsearch_tab()
            win.open_hostsearch_tab()
            self.assertIsNotNone(win.appsearch_tab)
            self.assertIsNotNone(win.hostsearch_tab)
            self.assertNotEqual(
                win.tabs.indexOf(win.appsearch_tab),
                win.tabs.indexOf(win.hostsearch_tab),
            )
            self.assertEqual(win.tabs.tabText(win.tabs.indexOf(win.hostsearch_tab)), "Pesquisa de Host")
            tab = win.hostsearch_tab
            tab.add_discovered_host("172.16.0.9", "HOST-9", queue_user_lookup=False)
            _eye_button(tab, 0).click()
            self.app.processEvents()
            self.assertIs(win.tabs.currentWidget(), win.psexec_tab)
            self.assertEqual(win.psexec_tab.host_edit.text(), "HOST-9")
            QApplication.setActiveWindow(win)
            self.app.processEvents()
            self.assertTrue(win.psexec_tab.host_edit.hasFocus())

            tab.add_discovered_host("172.16.0.10", "172.16.0.10", queue_user_lookup=False)
            win.open_hostsearch_tab()
            ip_row = next(
                r
                for r in range(tab.table.rowCount())
                if tab.table.item(r, COL_IP).text() == "172.16.0.10"
            )
            _eye_button(tab, ip_row).click()
            self.app.processEvents()
            self.assertIs(win.tabs.currentWidget(), win.psexec_tab)
            self.assertEqual(win.psexec_tab.host_edit.text(), "172.16.0.10")
            self.assertIsNotNone(win.appsearch_tab)
        finally:
            win.close()
            win.deleteLater()
            self.app.processEvents()

    def test_eye_with_standalone_psexec_tab(self) -> None:
        host_tab = HostSearchTab()
        psexec = PsExecTab()
        psexec.show()
        host_tab.show()
        self.app.processEvents()
        try:
            host_tab.openHostRequested.connect(
                lambda host: (
                    psexec.host_edit.setText(host),
                    psexec.host_edit.setFocus(),
                )
            )
            host_tab.add_discovered_host("8.8.8.8", "DNS-HOST", queue_user_lookup=False)
            _eye_button(host_tab, 0).click()
            self.app.processEvents()
            self.assertEqual(psexec.host_edit.text(), "DNS-HOST")
        finally:
            host_tab.shutdown(wait_ms=200)
            host_tab.deleteLater()
            psexec.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
