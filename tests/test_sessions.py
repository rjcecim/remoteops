"""Sessões WTS/quser: logins numéricos e fallback quando a API omite o usuário."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from remoteops.utils.hostsearch import (
    EMPTY_CELL,
    format_active_session_users,
    lookup_active_session_users,
    lookup_targets,
)
from remoteops.utils.sessions import (
    RemoteSession,
    _has_interactive_user,
    _merge_session_usernames,
    list_remote_sessions,
    parse_query_session_output,
    parse_quser_output,
)

QWINSTA_NUMERIC = """
 SESSIONNAME       USERNAME                 ID  STATE   TYPE        DEVICE
 services                                    0  Disc
 console           1234567                   1  Ativo
 rdp-tcp                                 65536  Listen
 rdp-tcp#3         1234567                   2  Active
"""

QUSER_NUMERIC = """
 USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME
>1234567               rdp-tcp#3           2  Active          .  17/09/2026 08:12
"""


class QuerySessionParseTests(unittest.TestCase):
    def test_numeric_username_is_not_session_id(self) -> None:
        sessions = parse_query_session_output(QWINSTA_NUMERIC)
        by_id = {item.session_id: item for item in sessions}
        self.assertEqual(by_id[1].username, "1234567")
        self.assertEqual(by_id[1].name, "console")
        self.assertEqual(by_id[1].state.casefold(), "ativo")
        self.assertEqual(by_id[2].username, "1234567")
        self.assertEqual(by_id[2].session_id, 2)
        self.assertEqual(by_id[0].username, "")

    def test_quser_numeric_login(self) -> None:
        sessions = parse_quser_output(QUSER_NUMERIC)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].username, "1234567")
        self.assertEqual(sessions[0].session_id, 2)
        self.assertEqual(sessions[0].name, "rdp-tcp#3")
        self.assertEqual(sessions[0].state.casefold(), "active")

    def test_ativo_counts_as_active_user(self) -> None:
        sessions = parse_query_session_output(QWINSTA_NUMERIC)
        self.assertEqual(format_active_session_users(sessions), "1234567")


class SessionFallbackTests(unittest.TestCase):
    def test_wts_without_username_is_not_complete(self) -> None:
        wts_only = [
            RemoteSession(0, "Services", "", "Desconectada", ""),
            RemoteSession(1, "console", "", "Ativa", ""),
        ]
        self.assertFalse(_has_interactive_user(wts_only))
        filled = _merge_session_usernames(
            wts_only,
            [RemoteSession(1, "console", "1234567", "Ativo", "")],
        )
        self.assertEqual(filled[1].username, "1234567")
        self.assertEqual(format_active_session_users(filled), "1234567")

    def test_list_remote_sessions_falls_back_to_quser(self) -> None:
        wts_only = [
            RemoteSession(0, "Services", "", "Desconectada", ""),
            RemoteSession(2, "rdp-tcp#3", "", "Ativa", ""),
        ]
        quser = [RemoteSession(2, "rdp-tcp#3", "1234567", "Active", "")]

        class _Auth:
            conflict = False
            connected = True
            error = ""

        with patch("remoteops.utils.sessions.connect_ipc", return_value=_Auth()), patch(
            "remoteops.utils.sessions.release_ipc"
        ), patch(
            "remoteops.utils.sessions._enumerate_wts", return_value=(wts_only, "")
        ), patch(
            "remoteops.utils.sessions._query_session_cli", return_value=(quser, "")
        ):
            sessions, error = list_remote_sessions("ETPRES-ACRP01", user="u", password="p")
        self.assertEqual(error, "")
        self.assertEqual(format_active_session_users(sessions), "1234567")

    def test_lookup_tries_hostname_before_ip(self) -> None:
        calls: list[str] = []

        def _list(host, **_kwargs):
            calls.append(host)
            if host == "ETPRES-ACRP01":
                return (
                    [RemoteSession(2, "rdp-tcp#3", "1234567", "Active", "TCE-PA")],
                    "",
                )
            return ([], "falha")

        with patch("remoteops.utils.hostsearch.list_remote_sessions", side_effect=_list):
            text = lookup_active_session_users(
                "192.168.31.97",
                hostname="ETPRES-ACRP01",
            )
        self.assertEqual(text, r"TCE-PA\1234567")
        self.assertEqual(calls[0], "ETPRES-ACRP01")
        self.assertEqual(lookup_targets("192.168.31.97", "ETPRES-ACRP01"), [
            "ETPRES-ACRP01",
            "192.168.31.97",
        ])

    def test_lookup_ip_only_when_hostname_missing(self) -> None:
        self.assertEqual(lookup_targets("192.168.31.97", "192.168.31.97"), ["192.168.31.97"])
        with patch(
            "remoteops.utils.hostsearch.list_remote_sessions",
            return_value=([], "falha"),
        ):
            self.assertEqual(lookup_active_session_users("192.168.31.97"), EMPTY_CELL)
