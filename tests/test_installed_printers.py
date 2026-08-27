"""Testes da consulta de impressoras instaladas — sem host real."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from remoteops.services.printers import PrinterService
from remoteops.utils.installed_printers import (
    MSG_AMBIGUOUS_SID,
    MSG_CANCELLED,
    MSG_IDENTIFYING,
    MSG_INVALID_HOST,
    MSG_MULTI_SESSION,
    MSG_NO_ACTIVE_SESSION,
    MSG_OFFLINE,
    MSG_PARTIAL_USER,
    MSG_TIMEOUT,
    SOURCE_COMPUTER_REG,
    SOURCE_SPOOLER,
    SOURCE_USER_REG,
    TYPE_COMPUTER_NET,
    TYPE_LOCAL,
    TYPE_TCPIP,
    TYPE_USB,
    TYPE_USER_NET,
    TYPE_VIRTUAL,
    InstalledPrintersPayload,
    apply_default_flag,
    assemble_installed_printers,
    assert_safe_computer_name,
    build_list_computer_printers_script,
    classify_computer_printer_error,
    classify_printer_type,
    identity_key,
    installed_query_readiness,
    is_user_sid_key,
    merge_printer_pair,
    normalize_unc,
    parse_connection_key,
    parse_device_value,
    printer_is_default,
    printer_to_mapping,
    resolve_user_sid,
    result_belongs_to_current,
    session_account,
    split_account,
    unc_from_friendly_name,
)
from remoteops.utils.ipc_auth import (
    ERROR_ALREADY_ASSIGNED,
    ERROR_SESSION_CREDENTIAL_CONFLICT,
    ERROR_SUCCESS,
    interpret_ipc_result,
)
from remoteops.utils.printers import NetworkPrinter
from remoteops.utils.remote_printers_query import (
    hanging_registry_worker,
    query_remote_printers_registry,
)
from remoteops.utils.sessions import RemoteSession


def _np(**kwargs) -> NetworkPrinter:
    return NetworkPrinter(**kwargs)


class TestConnectionKeysAndUnc(unittest.TestCase):
    def test_parse_connection_key_comma_form(self) -> None:
        parsed = parse_connection_key(",,PRINT01,Financeiro")
        self.assertEqual(parsed["server"], "PRINT01")
        self.assertEqual(parsed["share"], "Financeiro")
        self.assertEqual(parsed["unc"], r"\\PRINT01\Financeiro")

    def test_normalize_unc(self) -> None:
        self.assertEqual(normalize_unc(r"\\PRINT01\Financeiro"), r"\\PRINT01\Financeiro")
        self.assertEqual(normalize_unc("//PRINT01/Financeiro"), r"\\PRINT01\Financeiro")
        self.assertEqual(normalize_unc(r"\\\\PRINT01\\Financeiro"), r"\\PRINT01\Financeiro")
        self.assertEqual(normalize_unc(""), "")

    def test_friendly_name_to_unc(self) -> None:
        self.assertEqual(
            unc_from_friendly_name("Financeiro em PRINT01"),
            r"\\PRINT01\Financeiro",
        )
        self.assertEqual(
            unc_from_friendly_name("Financeiro on PRINT01"),
            r"\\PRINT01\Financeiro",
        )


class TestSidResolution(unittest.TestCase):
    def test_is_user_sid_key(self) -> None:
        self.assertTrue(is_user_sid_key("S-1-5-21-1-2-3-1001"))
        self.assertFalse(is_user_sid_key("S-1-5-21-1-2-3-1001_Classes"))
        self.assertFalse(is_user_sid_key("S-1-5-18"))
        self.assertFalse(is_user_sid_key(".DEFAULT"))

    def test_match_username_and_userdomain(self) -> None:
        entries = [
            {
                "sid": "S-1-5-21-1-2-3-1001",
                "USERNAME": "jsilva",
                "USERDOMAIN": "CORP",
                "USERDNSDOMAIN": "corp.example.com",
                "SESSIONNAME": "Console",
            },
            {
                "sid": "S-1-5-21-1-2-3-1002",
                "USERNAME": "admin",
                "USERDOMAIN": "CORP",
                "USERDNSDOMAIN": "corp.example.com",
                "SESSIONNAME": "RDP-Tcp#4",
            },
        ]
        sid, err = resolve_user_sid(
            entries, username="jsilva", domain="CORP", session_name="Console"
        )
        self.assertEqual(sid, "S-1-5-21-1-2-3-1001")
        self.assertEqual(err, "")

    def test_dns_domain_matches_netbios(self) -> None:
        entries = [
            {
                "sid": "S-1-5-21-1-2-3-1001",
                "USERNAME": "jsilva",
                "USERDOMAIN": "CORP",
                "USERDNSDOMAIN": "corp.tce.go.gov.br",
                "SESSIONNAME": "Console",
            }
        ]
        sid, err = resolve_user_sid(
            entries, username=r"CORP\jsilva", domain="", session_name=""
        )
        self.assertEqual(sid, "S-1-5-21-1-2-3-1001")
        self.assertEqual(err, "")

    def test_ambiguous_sid(self) -> None:
        entries = [
            {
                "sid": "S-1-5-21-1-2-3-1001",
                "USERNAME": "jsilva",
                "USERDOMAIN": "CORP",
                "USERDNSDOMAIN": "",
                "SESSIONNAME": "Console",
            },
            {
                "sid": "S-1-5-21-9-9-9-2002",
                "USERNAME": "jsilva",
                "USERDOMAIN": "CORP",
                "USERDNSDOMAIN": "",
                "SESSIONNAME": "",
            },
        ]
        sid, err = resolve_user_sid(entries, username="jsilva", domain="CORP")
        self.assertEqual(sid, "")
        self.assertEqual(err, MSG_AMBIGUOUS_SID)

    def test_does_not_pick_first_unrelated_sid(self) -> None:
        entries = [
            {
                "sid": "S-1-5-21-1-2-3-1001",
                "USERNAME": "outro",
                "USERDOMAIN": "CORP",
                "USERDNSDOMAIN": "",
                "SESSIONNAME": "Console",
            }
        ]
        sid, err = resolve_user_sid(entries, username="jsilva", domain="CORP")
        self.assertEqual(sid, "")
        self.assertTrue(err)


class TestDefaultPrinter(unittest.TestCase):
    def test_parse_device_value(self) -> None:
        self.assertEqual(
            parse_device_value(r"\\PRINT01\Financeiro,winspool,Ne00:"),
            r"\\PRINT01\Financeiro",
        )
        self.assertEqual(parse_device_value("HP Laser,winspool,LPT1:"), "HP Laser")
        self.assertEqual(parse_device_value(""), "")

    def test_identify_default(self) -> None:
        printer = _np(name=r"\\PRINT01\Financeiro", share_name="Financeiro")
        self.assertTrue(printer_is_default(printer, r"\\PRINT01\Financeiro"))
        self.assertTrue(
            printer_is_default(
                _np(name="Financeiro em PRINT01", share_name="Financeiro"),
                r"\\PRINT01\Financeiro",
            )
        )
        self.assertFalse(printer_is_default(_np(name="HP Local"), r"\\PRINT01\Financeiro"))

    def test_apply_default_flag(self) -> None:
        rows = apply_default_flag(
            [
                _np(name=r"\\PRINT01\Financeiro", share_name="Financeiro"),
                _np(name="HP Local", is_default=False),
            ],
            r"\\PRINT01\Financeiro",
        )
        self.assertTrue(rows[0].is_default)
        self.assertFalse(rows[1].is_default)


class TestDedupAndMerge(unittest.TestCase):
    def test_dedup_spooler_in_and_ga(self) -> None:
        spooler = [
            _np(
                name="Financeiro em PRINT01",
                share_name="Financeiro",
                driver_name="Driver Universal",
                port_name=r"\\PRINT01\Financeiro",
                printer_type="Connection",
            )
        ]
        merged = assemble_installed_printers(
            spooler=spooler,
            user_connections=[{"leaf": ",,PRINT01,Financeiro"}],
            computer_connections=[{"leaf": ",,PRINT01,Financeiro"}],
            default_name=r"\\PRINT01\Financeiro",
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].name, r"\\PRINT01\Financeiro")
        self.assertEqual(merged[0].driver_name, "Driver Universal")
        self.assertEqual(merged[0].printer_type, TYPE_USER_NET)
        self.assertTrue(merged[0].is_default)

    def test_fill_empty_fields_on_merge(self) -> None:
        left = _np(name=r"\\PRINT01\Financeiro", driver_name="", location="")
        right = _np(
            name="Financeiro",
            driver_name="HP Universal",
            location="Andar 2",
            comment="fila",
        )
        merged = merge_printer_pair(left, right)
        self.assertEqual(merged.driver_name, "HP Universal")
        self.assertEqual(merged.location, "Andar 2")
        self.assertEqual(merged.comment, "fila")
        self.assertEqual(merged.name, r"\\PRINT01\Financeiro")

    def test_identity_key_case_insensitive(self) -> None:
        a = identity_key(_np(name=r"\\Print01\Financeiro"))
        b = identity_key(_np(name=r"\\PRINT01\FINANCEIRO"))
        self.assertEqual(a, b)


class TestTypeClassification(unittest.TestCase):
    def test_types(self) -> None:
        self.assertEqual(
            classify_printer_type(source=SOURCE_USER_REG, name=r"\\S\Q"),
            TYPE_USER_NET,
        )
        self.assertEqual(
            classify_printer_type(source=SOURCE_COMPUTER_REG, name=r"\\S\Q"),
            TYPE_COMPUTER_NET,
        )
        self.assertEqual(
            classify_printer_type(source=SOURCE_SPOOLER, port_name="USB001"),
            TYPE_USB,
        )
        self.assertEqual(
            classify_printer_type(source=SOURCE_SPOOLER, port_name="IP_10.0.0.8"),
            TYPE_TCPIP,
        )
        self.assertEqual(
            classify_printer_type(
                source=SOURCE_SPOOLER, name="Microsoft Print to PDF", port_name="PORTPROMPT:"
            ),
            TYPE_VIRTUAL,
        )
        self.assertEqual(
            classify_printer_type(source=SOURCE_SPOOLER, port_name="LPT1:", name="HP"),
            TYPE_LOCAL,
        )


class TestPayloadAndReadiness(unittest.TestCase):
    def test_success_payload(self) -> None:
        payload = InstalledPrintersPayload(
            ok=True,
            partial=False,
            host="PC001",
            username=r"CORP\jsilva",
            session_id=2,
            printers=[
                _np(
                    name=r"\\PRINT01\Financeiro",
                    share_name="Financeiro",
                    driver_name="Driver Universal",
                    port_name=r"\\PRINT01\Financeiro",
                    location="Financeiro",
                    printer_type=TYPE_USER_NET,
                    is_default=True,
                )
            ],
        )
        data = payload.to_dict()
        self.assertTrue(data["ok"])
        self.assertFalse(data["partial"])
        self.assertEqual(data["host"], "PC001")
        self.assertEqual(data["username"], r"CORP\jsilva")
        self.assertEqual(data["session_id"], 2)
        self.assertEqual(data["printers"][0]["Name"], r"\\PRINT01\Financeiro")
        self.assertEqual(data["printers"][0]["Type"], TYPE_USER_NET)
        self.assertTrue(data["printers"][0]["Default"])
        self.assertEqual(data["error"], "")

    def test_partial_payload(self) -> None:
        payload = InstalledPrintersPayload(
            ok=True,
            partial=True,
            host="PC001",
            username=r"CORP\jsilva",
            printers=[_np(name="HP Local", printer_type=TYPE_LOCAL)],
            warnings=[MSG_PARTIAL_USER],
        )
        data = payload.to_dict()
        self.assertTrue(data["ok"])
        self.assertTrue(data["partial"])
        self.assertIn(MSG_PARTIAL_USER, data["warnings"])

    def test_readable_errors(self) -> None:
        payload = InstalledPrintersPayload(ok=False, host="PC001", error=MSG_TIMEOUT)
        self.assertFalse(payload.ok)
        self.assertEqual(payload.error, MSG_TIMEOUT)
        self.assertNotEqual(payload.error, "Falha na consulta")
        self.assertEqual(
            classify_computer_printer_error("RPC server is unavailable", 1, host="PC001"),
            "O spooler remoto não respondeu.",
        )
        self.assertIn("Acesso negado", classify_computer_printer_error("Access is denied", 5))

    def test_no_active_session(self) -> None:
        msg, session = installed_query_readiness(
            host="PC001",
            online=True,
            sessions=[
                RemoteSession(session_id=1, username="jsilva", state="Desconectada", domain="CORP")
            ],
            selected_session=None,
            sessions_loading=False,
        )
        self.assertEqual(msg, MSG_NO_ACTIVE_SESSION)
        self.assertIsNone(session)

    def test_multiple_active_sessions_require_choice(self) -> None:
        a = RemoteSession(session_id=2, username="ana", state="Ativa", domain="CORP")
        b = RemoteSession(session_id=3, username="bruno", state="Active", domain="CORP")
        msg, session = installed_query_readiness(
            host="PC001",
            online=True,
            sessions=[a, b],
            selected_session=None,
            sessions_loading=False,
        )
        self.assertEqual(msg, MSG_MULTI_SESSION)
        self.assertIsNone(session)
        msg, session = installed_query_readiness(
            host="PC001",
            online=True,
            sessions=[a, b],
            selected_session=b,
            sessions_loading=False,
        )
        self.assertEqual(msg, "")
        self.assertEqual(session.session_id, 3)

    def test_identifying_and_offline(self) -> None:
        msg, session = installed_query_readiness(
            host="PC001",
            online=True,
            sessions=[],
            selected_session=None,
            sessions_loading=True,
        )
        self.assertEqual(msg, MSG_IDENTIFYING)
        self.assertIsNone(session)
        msg, session = installed_query_readiness(
            host="PC001",
            online=False,
            sessions=[],
            selected_session=None,
            sessions_loading=False,
        )
        self.assertEqual(msg, MSG_OFFLINE)
        self.assertIsNone(session)

    def test_stale_result_from_other_host(self) -> None:
        self.assertFalse(
            result_belongs_to_current(
                result_host="PC001",
                current_host="PC002",
                result_generation=1,
                current_generation=1,
            )
        )
        self.assertFalse(
            result_belongs_to_current(
                result_host="PC001",
                current_host="PC001",
                result_generation=1,
                current_generation=2,
            )
        )
        self.assertTrue(
            result_belongs_to_current(
                result_host="PC001",
                current_host="PC001",
                result_generation=4,
                current_generation=4,
            )
        )

    def test_session_account_uses_wts_domain_only(self) -> None:
        session = RemoteSession(
            session_id=2, username="jsilva", state="Ativa", domain="CORP"
        )
        self.assertEqual(session_account(session), r"CORP\jsilva")
        self.assertEqual(split_account(r"CORP\jsilva"), ("CORP", "jsilva"))


class TestPowershellBuilder(unittest.TestCase):
    def test_builder_contains_get_printer_computername(self) -> None:
        script = build_list_computer_printers_script("PC001", r"C:\Temp\out.json")
        self.assertIn("Get-Printer -ComputerName", script)
        self.assertIn("'PC001'", script)
        self.assertIn("Type", script)
        self.assertIn("Default", script)

    def test_hostname_validation_and_escaping(self) -> None:
        self.assertEqual(assert_safe_computer_name("PC001"), "PC001")
        self.assertEqual(assert_safe_computer_name("10.0.0.8"), "10.0.0.8")
        with self.assertRaises(ValueError):
            assert_safe_computer_name("PC001;whoami")
        with self.assertRaises(ValueError):
            assert_safe_computer_name("bad'host")
        with self.assertRaises(ValueError):
            build_list_computer_printers_script("PC001$(calc)", r"C:\Temp\out.json")
        script = build_list_computer_printers_script("PC-01.corp.local", r"C:\Temp\out.json")
        self.assertIn("'PC-01.corp.local'", script)


class TestHostnameAssertMessage(unittest.TestCase):
    def test_empty_host_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            assert_safe_computer_name("")
        self.assertEqual(str(ctx.exception), MSG_INVALID_HOST)


class TestIpcCodes(unittest.TestCase):
    def test_interpret_ipc_result(self) -> None:
        ok = interpret_ipc_result(ERROR_SUCCESS, "PC001")
        self.assertTrue(ok.created)
        self.assertTrue(ok.connected)
        preexisting = interpret_ipc_result(ERROR_ALREADY_ASSIGNED, "PC001")
        self.assertTrue(preexisting.preexisting)
        self.assertTrue(preexisting.connected)
        self.assertFalse(preexisting.created)
        conflict = interpret_ipc_result(ERROR_SESSION_CREDENTIAL_CONFLICT, "PC001")
        self.assertTrue(conflict.conflict)
        self.assertFalse(conflict.connected)
        self.assertIn("IPC$", conflict.error)


class TestTimeoutAndCancel(unittest.TestCase):
    def test_timeout_kills_hanging_child(self) -> None:
        payload = query_remote_printers_registry(
            "PC001",
            timeout=0.6,
            worker_target=hanging_registry_worker,
            extra_request={"sleep_s": 20},
        )
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error_kind"), "timed_out")
        self.assertEqual(payload.get("error"), MSG_TIMEOUT)

    def test_cancel_before_start(self) -> None:
        payload = query_remote_printers_registry(
            "PC001",
            timeout=5,
            should_cancel=lambda: True,
            worker_target=hanging_registry_worker,
        )
        self.assertEqual(payload.get("error_kind"), "cancelled")
        self.assertEqual(payload.get("error"), MSG_CANCELLED)

    def test_cancel_during_hang(self) -> None:
        state = {"n": 0}

        def should_cancel() -> bool:
            state["n"] += 1
            return state["n"] > 2

        payload = query_remote_printers_registry(
            "PC001",
            timeout=8,
            should_cancel=should_cancel,
            worker_target=hanging_registry_worker,
            extra_request={"sleep_s": 20},
        )
        self.assertEqual(payload.get("error_kind"), "cancelled")
        self.assertEqual(payload.get("error"), MSG_CANCELLED)


class TestQueryPathHasNoPsExec(unittest.TestCase):
    def test_service_listing_source_avoids_psexec(self) -> None:
        src = inspect.getsource(PrinterService.list_host_installed_printers)
        self.assertNotIn("run_remote_script", src)
        self.assertNotIn("build_psexec_argv", src)
        self.assertNotIn("_query_session_psexec", src)
        helper = inspect.getsource(PrinterService._list_computer_printers)
        self.assertNotIn("run_remote_script", helper)
        self.assertNotIn("build_psexec_argv", helper)
        self.assertNotIn("psexec", helper.casefold())

    def test_query_modules_have_no_psexec(self) -> None:
        import remoteops.utils.installed_printers as inst
        import remoteops.utils.printers as printers_mod
        import remoteops.utils.remote_printers_query as query
        import remoteops.utils.remote_printers_registry as registry

        for mod in (inst, query, registry):
            src = inspect.getsource(mod)
            self.assertNotIn("run_remote_script", src)
            self.assertNotIn("build_psexec_argv", src)
            self.assertNotIn("_query_session_psexec", src)
        self.assertFalse(hasattr(printers_mod, "build_list_host_installed_printers_script"))
        self.assertFalse(hasattr(printers_mod, "parse_user_printers_payload"))
        self.assertFalse(hasattr(PrinterService, "list_host_user_printers"))

    def test_mapping_roundtrip(self) -> None:
        printer = _np(name=r"\\S\Q", printer_type=TYPE_USER_NET, is_default=True)
        data = printer_to_mapping(printer)
        self.assertEqual(data["Type"], TYPE_USER_NET)
        self.assertTrue(data["Default"])


class TestSessionsNoPsExecFallback(unittest.TestCase):
    @patch("remoteops.utils.sessions._query_session_psexec")
    @patch("remoteops.utils.sessions._query_session_cli", return_value=([], "cli"))
    @patch(
        "remoteops.utils.sessions._enumerate_wts",
        return_value=([], "Não foi possível consultar as sessões pela API WTS."),
    )
    @patch("remoteops.utils.sessions.connect_ipc")
    @patch("remoteops.utils.sessions.release_ipc")
    def test_default_does_not_call_psexec(
        self, _release, connect, _wts, _cli, psexec
    ) -> None:
        from remoteops.utils.ipc_auth import IpcAuthResult
        from remoteops.utils.sessions import list_remote_sessions

        connect.return_value = IpcAuthResult(host="PC001", skipped=True)
        sessions, error = list_remote_sessions("PC001")
        self.assertEqual(sessions, [])
        self.assertIn("WTS", error)
        psexec.assert_not_called()

    @patch("remoteops.utils.sessions._query_session_psexec")
    @patch("remoteops.utils.sessions._query_session_cli", return_value=([], "cli"))
    @patch("remoteops.utils.sessions._enumerate_wts", return_value=([], "wts"))
    @patch("remoteops.utils.sessions.connect_ipc")
    @patch("remoteops.utils.sessions.release_ipc")
    def test_explicit_opt_in_may_call_psexec(
        self, _release, connect, _wts, _cli, psexec
    ) -> None:
        from remoteops.utils.ipc_auth import IpcAuthResult
        from remoteops.utils.sessions import list_remote_sessions

        connect.return_value = IpcAuthResult(host="PC001", skipped=True)
        psexec.return_value = []
        list_remote_sessions("PC001", allow_psexec_fallback=True)
        psexec.assert_called_once()

    def test_list_remote_sessions_signature_defaults_safe(self) -> None:
        from remoteops.utils.sessions import list_remote_sessions

        src = inspect.getsource(list_remote_sessions)
        self.assertIn("allow_psexec_fallback: bool = False", src)


if __name__ == "__main__":
    unittest.main()
