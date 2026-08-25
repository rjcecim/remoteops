"""Testes das regras de impressoras de rede (UNC, PrintUI, escopo, segurança)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from remoteops.services.ops import CredentialContext
from remoteops.services.printers import (
    CommandCapture,
    InstallPrinterRequest,
    PrinterService,
)
from remoteops.utils.printer_settings import (
    DEFAULT_PRINT_LIST_TIMEOUT_S,
    DEFAULT_PRINT_SERVER,
    MAX_PRINT_LIST_TIMEOUT_S,
    MIN_PRINT_LIST_TIMEOUT_S,
    PRINT_SERVER_PLACEHOLDER,
    PRINT_SERVER_REQUIRED_MSG,
    get_print_server,
    is_print_server_configured,
    normalize_print_list_timeout,
    parse_print_server_input,
    require_print_server,
)
from remoteops.utils.printers import (
    FORBIDDEN_SECURITY_SNIPPETS,
    SCOPE_ALL,
    SCOPE_USER,
    NetworkPrinter,
    build_list_printers_script,
    can_install_printer,
    can_proceed_to_connect,
    classify_printer_error,
    collected_printer_scripts,
    describe_printui_fields,
    effective_set_default,
    elevated_psexec_flags,
    install_block_reason,
    local_list_printers_argv,
    logical_printui_preview,
    parse_printers_json,
    print_server_unc,
    printer_matches_query,
    printer_unc,
    printui_argv,
    printui_command_line,
    read_printers_catalog_file,
    reject_psexec_interactive_identity,
    should_connect_active_user,
    should_prepare_driver,
)
from remoteops.utils.sessions import RemoteSession

TEST_SERVER = "printserver"
TEST_PRINTER_UNC = r"\\printserver\HP_FIN_03"

ACTIVE = RemoteSession(
    session_id=2, name="console", username=r"TCE\joao.silva", state="Ativa"
)
DISCONNECTED = RemoteSession(
    session_id=3, name="rdp-tcp#1", username=r"TCE\maria", state="Desconectada"
)
PRINTER = NetworkPrinter(
    name="HP LaserJet no 3º Andar",
    share_name="HP_FIN_03",
    driver_name="HP Universal Printing PCL 6",
    location="Financeiro",
    comment="colorida",
)
UNSHARED = NetworkPrinter(
    name="HP Local",
    share_name="",
    driver_name="HP Universal Printing PCL 6",
)


class UncTests(unittest.TestCase):
    def test_share_with_spaces(self):
        self.assertEqual(
            printer_unc("HP Financeiro", TEST_SERVER),
            r"\\printserver\HP Financeiro",
        )

    def test_uses_share_name_not_display_name(self):
        unc = printer_unc(PRINTER.share_name, TEST_SERVER)
        self.assertEqual(unc, TEST_PRINTER_UNC)
        self.assertNotIn("LaserJet", unc)
        self.assertNotIn("3º", unc)

    def test_empty_share_name_raises(self):
        with self.assertRaises(ValueError):
            printer_unc("  ", TEST_SERVER)
        with self.assertRaises(ValueError):
            printer_unc(UNSHARED.share_name, TEST_SERVER)

    def test_empty_server_raises(self):
        with self.assertRaises(ValueError):
            print_server_unc("")
        with self.assertRaises(ValueError):
            printer_unc(PRINTER.share_name, "")
        self.assertEqual(print_server_unc(TEST_SERVER), r"\\printserver")


class PrintUiTests(unittest.TestCase):
    def test_user_connection(self):
        argv = printui_argv("in", TEST_PRINTER_UNC)
        self.assertEqual(argv[0], "rundll32.exe")
        self.assertEqual(argv[1], "printui.dll,PrintUIEntry")
        self.assertIn("/in", argv)
        self.assertIn(r"/n\\printserver\HP_FIN_03", argv)
        self.assertIn("/q", argv)
        self.assertNotIn("/ga", argv)

    def test_all_users(self):
        argv = printui_argv("ga", TEST_PRINTER_UNC)
        self.assertIn("/ga", argv)
        self.assertIn(r"/n\\printserver\HP_FIN_03", argv)
        self.assertIn("/q", argv)
        self.assertNotIn("/in", argv)

    def test_default_printer(self):
        argv = printui_argv("y", TEST_PRINTER_UNC)
        self.assertIn("/y", argv)
        self.assertIn(r"/n\\printserver\HP_FIN_03", argv)
        self.assertIn("/q", argv)

    def test_preview_user_and_all(self):
        user = logical_printui_preview(
            scope=SCOPE_USER, unc=TEST_PRINTER_UNC, set_default=True
        )
        self.assertIn("/in", user)
        self.assertIn("/y", user)
        self.assertIn("/q", user)
        all_users = logical_printui_preview(
            scope=SCOPE_ALL, unc=TEST_PRINTER_UNC, set_default=True
        )
        self.assertIn("/ga", all_users)
        self.assertNotIn("/y", all_users)
        self.assertIn(printui_command_line("ga", TEST_PRINTER_UNC), all_users)


class ScopeTests(unittest.TestCase):
    def test_user_requires_active_session(self):
        self.assertTrue(
            can_install_printer(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_USER,
                session=ACTIVE,
            )
        )
        self.assertFalse(
            can_install_printer(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_USER,
                session=DISCONNECTED,
            )
        )
        self.assertFalse(
            can_install_printer(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_USER,
                session=None,
            )
        )
        self.assertIn(
            "sessão interativa",
            install_block_reason(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_USER,
                session=None,
            ).casefold(),
        )

    def test_all_users_does_not_require_session(self):
        self.assertTrue(
            can_install_printer(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_ALL,
                session=None,
            )
        )
        self.assertFalse(
            can_install_printer(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_ALL,
                session=None,
                server_configured=False,
            )
        )
        self.assertIn(
            "configurações",
            install_block_reason(
                host_online=True,
                printer=PRINTER,
                busy=False,
                scope=SCOPE_ALL,
                session=None,
                server_configured=False,
            ).casefold(),
        )
        self.assertTrue(should_connect_active_user(SCOPE_ALL, True))
        self.assertFalse(should_connect_active_user(SCOPE_ALL, False))
        self.assertFalse(should_connect_active_user(SCOPE_USER, True))

    def test_default_is_not_global(self):
        self.assertTrue(effective_set_default(SCOPE_USER, True))
        self.assertFalse(effective_set_default(SCOPE_ALL, True))
        fields = describe_printui_fields(
            scope=SCOPE_ALL,
            unc=TEST_PRINTER_UNC,
            set_default=True,
            has_active_user=True,
        )
        self.assertFalse(any(value == "/y" for _title, value in fields))

    def test_empty_share_disables_install(self):
        self.assertFalse(
            can_install_printer(
                host_online=True,
                printer=UNSHARED,
                busy=False,
                scope=SCOPE_ALL,
                session=None,
            )
        )
        self.assertEqual(
            install_block_reason(
                host_online=True,
                printer=UNSHARED,
                busy=False,
                scope=SCOPE_ALL,
                session=None,
            ),
            "Não compartilhada",
        )


class DriverPipelineTests(unittest.TestCase):
    def setUp(self):
        self.svc = PrinterService()
        self.creds = CredentialContext(user=r"TCE\admin", password="secret-password")
        self.request = InstallPrinterRequest(
            host="ETSEGP03",
            printer=PRINTER,
            scope=SCOPE_USER,
            session=ACTIVE,
        )
        self._server_patch = patch(
            "remoteops.services.printers.require_print_server",
            return_value=TEST_SERVER,
        )
        self._server_patch.start()

    def tearDown(self):
        self._server_patch.stop()
        self.creds.clear()

    def _user_ok(self):
        return CommandCapture(ok=True, payload={"ok": True, "connected": True})

    def test_present_driver_skips_prepare(self):
        with patch.object(
            self.svc,
            "check_driver",
            return_value=CommandCapture(ok=True, payload={"present": True}),
        ):
            with patch.object(self.svc, "prepare_driver") as prepare:
                with patch.object(self.svc, "connect_user", return_value=self._user_ok()):
                    with patch.object(self.svc, "cleanup_operation"):
                        result = self.svc.install(self.request, self.creds)
        prepare.assert_not_called()
        self.assertTrue(result.ok)
        self.assertFalse(result.driver_prepared)
        self.assertFalse(should_prepare_driver(True))

    def test_missing_driver_runs_prepare(self):
        with patch.object(
            self.svc,
            "check_driver",
            return_value=CommandCapture(ok=True, payload={"present": False}),
        ):
            with patch.object(
                self.svc,
                "prepare_driver",
                return_value=CommandCapture(ok=True, payload={"present": True}),
            ) as prepare:
                with patch.object(self.svc, "connect_user", return_value=self._user_ok()) as connect:
                    with patch.object(self.svc, "cleanup_operation"):
                        result = self.svc.install(self.request, self.creds)
        prepare.assert_called_once()
        connect.assert_called_once()
        self.assertTrue(result.ok)
        self.assertTrue(result.driver_prepared)
        self.assertTrue(should_prepare_driver(False))

    def test_prepare_failure_blocks_user_connect(self):
        with patch.object(
            self.svc,
            "check_driver",
            return_value=CommandCapture(ok=True, payload={"present": False}),
        ):
            with patch.object(
                self.svc,
                "prepare_driver",
                return_value=CommandCapture(
                    ok=False, error="Driver não pôde ser preparado."
                ),
            ):
                with patch.object(self.svc, "connect_user") as connect:
                    with patch.object(self.svc, "cleanup_operation"):
                        result = self.svc.install(self.request, self.creds)
        connect.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Driver não pôde ser preparado.")
        self.assertFalse(can_proceed_to_connect(driver_ready=False))

    def test_all_users_applies_ga_and_in_when_active(self):
        request = InstallPrinterRequest(
            host="ETSEGP03",
            printer=PRINTER,
            scope=SCOPE_ALL,
            session=ACTIVE,
        )
        with patch.object(
            self.svc,
            "check_driver",
            return_value=CommandCapture(ok=True, payload={"present": True}),
        ):
            with patch.object(
                self.svc,
                "connect_computer",
                return_value=CommandCapture(ok=True, payload={"ok": True}),
            ) as ga:
                with patch.object(
                    self.svc, "connect_user", return_value=self._user_ok()
                ) as user:
                    with patch.object(self.svc, "cleanup_operation"):
                        result = self.svc.install(request, self.creds)
        ga.assert_called_once()
        user.assert_called_once()
        self.assertFalse(user.call_args.kwargs.get("set_default"))
        self.assertTrue(result.ok)
        self.assertTrue(result.computer_connected)
        self.assertTrue(result.user_connected)

    def test_all_users_without_session_only_ga(self):
        request = InstallPrinterRequest(
            host="ETSEGP03",
            printer=PRINTER,
            scope=SCOPE_ALL,
            session=None,
        )
        with patch.object(
            self.svc,
            "check_driver",
            return_value=CommandCapture(ok=True, payload={"present": True}),
        ):
            with patch.object(
                self.svc,
                "connect_computer",
                return_value=CommandCapture(ok=True, payload={"ok": True}),
            ):
                with patch.object(self.svc, "connect_user") as user:
                    with patch.object(self.svc, "cleanup_operation"):
                        result = self.svc.install(request, self.creds)
        user.assert_not_called()
        self.assertTrue(result.ok)

    def test_logs_and_result_redact_password(self):
        logs: list[str] = []
        with patch.object(
            self.svc,
            "check_driver",
            return_value=CommandCapture(ok=True, payload={"present": True}),
        ):
            with patch.object(self.svc, "connect_user", return_value=self._user_ok()):
                with patch.object(self.svc, "cleanup_operation"):
                    result = self.svc.install(
                        self.request,
                        self.creds,
                        progress=logs.append,
                        passwords=["secret-password"],
                    )
        blob = "\n".join(logs) + result.message
        self.assertNotIn("secret-password", blob)
        preview = logical_printui_preview(
            scope=SCOPE_USER, unc=TEST_PRINTER_UNC, set_default=True
        )
        self.assertNotIn("secret-password", preview)
        self.assertNotIn("-p", preview)


class JsonAndFilterTests(unittest.TestCase):
    def test_zero_one_many_and_accents(self):
        self.assertEqual(parse_printers_json("[]"), [])
        one = parse_printers_json(
            '{"Name":"HP 3º andar","ShareName":"HP_FIN_03","DriverName":"HP",'
            '"PortName":"IP_1","Location":"Financeiro","Comment":"colorida",'
            '"Published":true}'
        )
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0].name, "HP 3º andar")
        many = parse_printers_json(
            '[{"Name":"A","ShareName":"A1","DriverName":"D","PortName":"P",'
            '"Location":"L","Comment":"C","Published":false},'
            '{"Name":"B","ShareName":"B1","DriverName":"D","PortName":"P",'
            '"Location":"L","Comment":"C","Published":false}]'
        )
        self.assertEqual(len(many), 2)

    def test_filter_fields(self):
        self.assertTrue(printer_matches_query(PRINTER, "HP"))
        self.assertTrue(printer_matches_query(PRINTER, "financeiro"))
        self.assertTrue(printer_matches_query(PRINTER, "colorida"))
        self.assertTrue(printer_matches_query(PRINTER, "HP_FIN_03"))
        self.assertFalse(printer_matches_query(PRINTER, "brother"))


class DiscoveryAndSecurityTests(unittest.TestCase):
    _CATALOG = r"C:\Temp\RemoteOps_Printers_test.json"

    def test_list_argv_requires_server_and_temp_json_path(self):
        with self.assertRaises(ValueError):
            local_list_printers_argv()
        with self.assertRaises(ValueError):
            local_list_printers_argv(TEST_SERVER)
        with self.assertRaises(ValueError):
            local_list_printers_argv("", self._CATALOG)
        with self.assertRaises(ValueError):
            build_list_printers_script()
        with self.assertRaises(ValueError):
            build_list_printers_script(TEST_SERVER)

    def test_list_argv_is_local_powershell(self):
        argv = local_list_printers_argv(TEST_SERVER, self._CATALOG)
        joined = " ".join(argv).lower()
        self.assertEqual(argv[0], "powershell.exe")
        self.assertIn("-encodedcommand", joined)
        self.assertIn("-executionpolicy", joined)
        self.assertIn("bypass", joined)
        self.assertNotIn("psexec", joined)
        script = build_list_printers_script(TEST_SERVER, self._CATALOG)
        self.assertIn("Get-Printer", script)
        self.assertIn(TEST_SERVER, script)
        self.assertNotIn("orfeu", script.casefold())
        self.assertIn("WriteAllText", script)
        self.assertIn("RemoteOps_Printers_test.json", script)
        self.assertIn('"ok":true', script)
        self.assertNotIn("Get-CimInstance", script)
        self.assertNotIn("WinRM", script)

    def test_list_script_does_not_dump_catalog_to_stdout(self):
        script = build_list_printers_script(TEST_SERVER, self._CATALOG)
        self.assertIn("[System.IO.File]::WriteAllText", script)
        self.assertNotIn("Write-Output $json", script)
        self.assertNotIn("Write-Output ([string]$json)", script)

    def test_read_printers_catalog_file(self):
        handle, path = tempfile.mkstemp(prefix="RemoteOps_Printers_", suffix=".json")
        os.close(handle)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    '[{"Name":"IRVDI01_VSS","ShareName":"IRVDI01_VSS",'
                    '"DriverName":"Samsung M408x Series","PortName":"P",'
                    '"Location":"","Comment":"","Published":false}]'
                )
            printers = read_printers_catalog_file(path)
            self.assertEqual(len(printers), 1)
            self.assertEqual(printers[0].name, "IRVDI01_VSS")
            self.assertEqual(printers[0].share_name, "IRVDI01_VSS")
        finally:
            os.remove(path)

    def test_list_server_printers_reads_and_deletes_temp_json(self):
        created: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args, **kwargs):
            handle, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return handle, path

        payload = (
            '[{"Name":"IRVDI01_VSS","ShareName":"IRVDI01_VSS",'
            '"DriverName":"Samsung M408x Series","PortName":"TCP/IP-192.168.1.201",'
            '"Location":"","Comment":"","Published":false}]'
        )

        def fake_run(*_args, **_kwargs):
            with open(created[0], "w", encoding="utf-8") as fh:
                fh.write(payload)
            return CommandCapture(
                ok=True, stdout='{"ok":true,"count":1}', exit_code=0
            )

        service = PrinterService()
        with patch(
            "remoteops.services.printers.tempfile.mkstemp",
            side_effect=tracking_mkstemp,
        ):
            with patch.object(service, "_run_argv", side_effect=fake_run):
                printers, error = service.list_server_printers(TEST_SERVER)
        self.assertEqual(error, "")
        self.assertEqual(len(printers), 1)
        self.assertEqual(printers[0].name, "IRVDI01_VSS")
        self.assertTrue(created)
        self.assertFalse(os.path.isfile(created[0]))

    def test_list_server_printers_empty_temp_file(self):
        service = PrinterService()

        def fake_run(*_args, **_kwargs):
            return CommandCapture(
                ok=True, stdout='{"ok":true,"count":0}', exit_code=0
            )

        with patch.object(service, "_run_argv", side_effect=fake_run):
            printers, error = service.list_server_printers(TEST_SERVER)
        self.assertEqual(printers, [])
        self.assertIn("temporário", error)

    def test_classify_does_not_echo_truncated_json(self):
        dump = '[{"Name":"IRVDI01_VSS","ShareName":"IRVDI01_VSS","DriverName":'
        msg = classify_printer_error(dump)
        self.assertNotIn("IRVDI01", msg)
        self.assertIn("impress", msg.casefold())

    def test_list_timeout_is_not_user_task_timeout(self):
        msg = classify_printer_error(
            "timeout consultando o servidor de impressão", server=TEST_SERVER
        )
        self.assertIn(print_server_unc(TEST_SERVER), msg)
        self.assertNotIn("tarefa", msg.casefold())
        empty = classify_printer_error("timeout consultando o servidor de impressão")
        self.assertIn("servidor de impressão", empty.casefold())
        self.assertNotIn("\\\\", empty)
        task = classify_printer_error("timeout aguardando tarefa do usuário")
        self.assertIn("tarefa", task.casefold())

    def test_scripts_do_not_weaken_security(self):
        blob = "\n".join(collected_printer_scripts()).casefold()
        for snippet in FORBIDDEN_SECURITY_SNIPPETS:
            self.assertNotIn(snippet.casefold(), blob)
        self.assertIn("interactivetoken", blob.replace(" ", ""))
        self.assertIn("unregister-scheduledtask", blob)
        self.assertIn("runlevel limited", blob)

    def test_psexec_interactive_identity_is_rejected(self):
        flags = elevated_psexec_flags(as_system=True)
        self.assertIn("-s", flags)
        self.assertNotIn("-i", flags)
        reject_psexec_interactive_identity(flags)
        with self.assertRaises(ValueError):
            reject_psexec_interactive_identity(["-accepteula", "-i", "2"])

    def test_list_server_printers_requires_configured_server(self):
        service = PrinterService()
        with patch(
            "remoteops.services.printers.require_print_server",
            side_effect=ValueError(PRINT_SERVER_REQUIRED_MSG),
        ):
            printers, error = service.list_server_printers()
        self.assertEqual(printers, [])
        self.assertIn("configurações", error.casefold())

    def test_install_requires_configured_server(self):
        service = PrinterService()
        creds = CredentialContext(user=r"TCE\admin", password="")
        request = InstallPrinterRequest(
            host="ETSEGP03",
            printer=PRINTER,
            scope=SCOPE_ALL,
        )
        with patch(
            "remoteops.services.printers.require_print_server",
            side_effect=ValueError(PRINT_SERVER_REQUIRED_MSG),
        ):
            result = service.install(request, creds)
        creds.clear()
        self.assertFalse(result.ok)
        self.assertIn("configurações", result.message.casefold())

    def test_scripts_do_not_embed_hardcoded_server(self):
        blob = "\n".join(collected_printer_scripts()).casefold()
        self.assertNotIn("orfeu", blob)
        self.assertIn("printserver", blob)


class PrinterSettingsTests(unittest.TestCase):
    def setUp(self):
        import remoteops.utils.printer_settings as mod

        self.mod = mod
        mod._runtime_server = None
        mod._runtime_timeout = None

    def tearDown(self):
        self.mod._runtime_server = None
        self.mod._runtime_timeout = None

    def test_default_server_is_empty(self):
        self.assertEqual(DEFAULT_PRINT_SERVER, "")
        with patch.object(self.mod, "load_setting", return_value=""):
            self.assertEqual(get_print_server(), "")
            self.assertFalse(is_print_server_configured())
            with self.assertRaises(ValueError) as ctx:
                require_print_server()
            self.assertIn("configurações", str(ctx.exception).casefold())

    def test_placeholder_is_not_saved_by_default(self):
        self.assertEqual(PRINT_SERVER_PLACEHOLDER, r"\\servidor-impressao")
        self.assertNotEqual(
            PRINT_SERVER_PLACEHOLDER.strip("\\"), DEFAULT_PRINT_SERVER
        )
        self.assertEqual(parse_print_server_input(""), "")
        self.assertEqual(parse_print_server_input("   "), "")

    def test_parse_uses_host_from_unc(self):
        self.assertEqual(
            parse_print_server_input(r"\\printserver\HP_FIN_03"),
            "printserver",
        )
        with self.assertRaises(ValueError):
            parse_print_server_input("bad host!")

    def test_timeout_clamp(self):
        self.assertEqual(normalize_print_list_timeout(10), MIN_PRINT_LIST_TIMEOUT_S)
        self.assertEqual(normalize_print_list_timeout(999), MAX_PRINT_LIST_TIMEOUT_S)
        self.assertEqual(
            normalize_print_list_timeout("nope"), DEFAULT_PRINT_LIST_TIMEOUT_S
        )


if __name__ == "__main__":
    unittest.main()
