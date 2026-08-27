"""Testes unitários do módulo Serviços (PsService)."""

from __future__ import annotations

import unittest

from remoteops.utils.redaction import redact_command_text
from remoteops.utils.servicos import (
    SETCONFIG_AUTO,
    SETCONFIG_DEMAND,
    SETCONFIG_DISABLED,
    build_psservice_argv,
    classify_psservice_error,
    friendly_error_message,
    merge_query_and_config,
    parse_psservice_config,
    parse_psservice_depend,
    parse_psservice_find,
    parse_psservice_query,
    parse_psservice_security,
    translate_start_type,
    translate_state,
)


QUERY_SAMPLE = """
SERVICE_NAME: Spooler
DISPLAY_NAME: Spooler de Impressão
Esse serviço coloca em spool os trabalhos de impressão.
\tTYPE\t\t  : 110 WIN32_OWN_PROCESS INTERACTIVE_PROCESS
\tSTATE\t\t  : 4  RUNNING
\t\t\t       (STOPPABLE,NOT_PAUSABLE,IGNORES_SHUTDOWN)
\tWIN32_EXIT_CODE\t  : 0  (0x0)
\tSERVICE_EXIT_CODE : 0  (0x0)
\tCHECKPOINT\t  : 0x0
\tWAIT_HINT\t  : 0 ms

SERVICE_NAME: BITS
DISPLAY_NAME: Background Intelligent Transfer Service
\tTYPE\t\t  : 20 WIN32_SHARE_PROCESS
\tSTATE\t\t  : 1  STOPPED
\t\t\t       (NOT_STOPPABLE,NOT_PAUSABLE,IGNORES_SHUTDOWN)
\tWIN32_EXIT_CODE\t  : 1077 (0x435)
\tSERVICE_EXIT_CODE : 0  (0x0)
\tCHECKPOINT\t  : 0x0
\tWAIT_HINT\t  : 0 ms
"""

CONFIG_SAMPLE = """
SERVICE_NAME: Spooler
DISPLAY_NAME: Spooler de Impressão
\tTYPE\t\t  : 110 WIN32_OWN_PROCESS INTERACTIVE_PROCESS
\tSTART_TYPE\t  : 2  AUTO_START
\tERROR_CONTROL\t  : 1  NORMAL
\tBINARY_PATH_NAME  : C:\\WINDOWS\\System32\\spoolsv.exe
\tLOAD_ORDER_GROUP  : SpoolerGroup
\tTAG\t\t  : 0
\tDEPENDENCIES\t  : RPCSS
\t\t\t  : http
\tSERVICE_START_NAME: LocalSystem

SERVICE_NAME: BITS
DISPLAY_NAME: Background Intelligent Transfer Service
\tTYPE\t\t  : 20 WIN32_SHARE_PROCESS
\tSTART_TYPE\t  : 3  DEMAND_START
\tERROR_CONTROL\t  : 1  NORMAL
\tBINARY_PATH_NAME  : C:\\WINDOWS\\System32\\svchost.exe -k netsvcs
\tLOAD_ORDER_GROUP  :
\tTAG\t\t  : 0
\tDEPENDENCIES\t  : RpcSs
\tSERVICE_START_NAME: LocalSystem
"""

DEPEND_EMPTY = "Spooler has no dependent services.\n"

DEPEND_SAMPLE = """
SERVICE_NAME: WinRM
DISPLAY_NAME: Windows Remote Management (WS-Management)
\tTYPE\t\t  : 20 WIN32_SHARE_PROCESS
\tSTATE\t\t  : 1  STOPPED
\t\t\t       (NOT_STOPPABLE,NOT_PAUSABLE,IGNORES_SHUTDOWN)
\tWIN32_EXIT_CODE\t  : 1077 (0x435)
\tSERVICE_EXIT_CODE : 0  (0x0)
\tCHECKPOINT\t  : 0x0
\tWAIT_HINT\t  : 0 ms
"""

SECURITY_SAMPLE = """
SERVICE_NAME: Spooler
DISPLAY_NAME: Spooler de Impressão
\tACCOUNT: LocalSystem
\tSECURITY:
\t[ALLOW] AUTORIDADE NT\\Usuários autenticados
\t        Query status
\t        Query Config
\t[ALLOW] BUILTIN\\Administradores
\t        All
"""

FIND_SAMPLE = """
ETSEGP01
ETSEGP03
\\\\ETSEGP08
ETSEGP12 some detail
"""


class ServicosParserTests(unittest.TestCase):
    def test_parse_query(self) -> None:
        rows = parse_psservice_query(QUERY_SAMPLE)
        self.assertEqual(len(rows), 2)
        spooler = rows[0]
        self.assertEqual(spooler.service_name, "Spooler")
        self.assertEqual(spooler.state, "RUNNING")
        self.assertEqual(spooler.state_code, 4)
        self.assertIn("STOPPABLE", spooler.controls_accepted)
        self.assertEqual(rows[1].state, "STOPPED")

    def test_parse_config(self) -> None:
        rows = parse_psservice_config(CONFIG_SAMPLE)
        self.assertEqual(len(rows), 2)
        spooler = rows[0]
        self.assertEqual(spooler.start_type, "AUTO_START")
        self.assertEqual(spooler.account, "LocalSystem")
        self.assertEqual(spooler.binary_path, r"C:\WINDOWS\System32\spoolsv.exe")
        self.assertEqual(spooler.dependencies, ("RPCSS", "http"))
        self.assertEqual(rows[1].start_type, "DEMAND_START")

    def test_merge_query_and_config(self) -> None:
        merged = merge_query_and_config(
            parse_psservice_query(QUERY_SAMPLE),
            parse_psservice_config(CONFIG_SAMPLE),
            host="ETSEGP03",
        )
        by_name = {r.service_name: r for r in merged}
        spooler = by_name["Spooler"]
        self.assertEqual(spooler.state, "RUNNING")
        self.assertEqual(spooler.start_type, "AUTO_START")
        self.assertEqual(spooler.account, "LocalSystem")
        self.assertEqual(spooler.host, "ETSEGP03")
        self.assertEqual(spooler.setconfig_token, SETCONFIG_AUTO)

    def test_parse_depend_empty_and_list(self) -> None:
        rows, empty = parse_psservice_depend(DEPEND_EMPTY)
        self.assertEqual(rows, [])
        self.assertTrue(empty)
        rows2, empty2 = parse_psservice_depend(DEPEND_SAMPLE)
        self.assertFalse(empty2)
        self.assertEqual(rows2[0].service_name, "WinRM")

    def test_parse_security(self) -> None:
        info = parse_psservice_security(SECURITY_SAMPLE)
        self.assertEqual(info.service_name, "Spooler")
        self.assertEqual(info.account, "LocalSystem")
        self.assertGreaterEqual(len(info.entries), 2)
        self.assertTrue(info.entries[0].startswith("[ALLOW]"))

    def test_parse_find(self) -> None:
        hits = parse_psservice_find(FIND_SAMPLE)
        names = [h.computer for h in hits]
        self.assertEqual(names, ["ETSEGP01", "ETSEGP03", "ETSEGP08", "ETSEGP12"])

    def test_translate_known_and_unknown_state(self) -> None:
        self.assertEqual(translate_state("RUNNING"), "Executando")
        self.assertEqual(translate_state("STOPPED"), "Parado")
        self.assertEqual(translate_state("WEIRD_STATE"), "WEIRD_STATE")

    def test_translate_start_types(self) -> None:
        self.assertEqual(translate_start_type("AUTO_START"), "Automático")
        self.assertEqual(translate_start_type("DEMAND_START"), "Manual")
        self.assertEqual(translate_start_type("DISABLED"), "Desabilitado")
        self.assertEqual(translate_start_type("BOOT_START"), "Inicialização (boot)")


class ServicosArgvTests(unittest.TestCase):
    def test_query_argv_with_auth(self) -> None:
        argv = build_psservice_argv(
            r"C:\PSTools\PsService64.exe",
            "query",
            host="ETSEGP03",
            user=r"DOM\user",
            password="s3cret!",
        )
        self.assertEqual(argv[0], r"C:\PSTools\PsService64.exe")
        self.assertIn("-accepteula", argv)
        self.assertIn("-nobanner", argv)
        self.assertIn(r"\\ETSEGP03", argv)
        self.assertIn("-u", argv)
        self.assertIn(r"DOM\user", argv)
        self.assertIn("-p", argv)
        self.assertIn("s3cret!", argv)
        self.assertEqual(argv[-1], "query")

    def test_setconfig_tokens(self) -> None:
        for token in (SETCONFIG_AUTO, SETCONFIG_DEMAND, SETCONFIG_DISABLED):
            argv = build_psservice_argv(
                "PsService64.exe",
                "setconfig",
                host="H",
                service_name="Spooler",
                extra=[token],
            )
            self.assertEqual(argv[-3:], ["setconfig", "Spooler", token])

    def test_find_argv_without_host(self) -> None:
        argv = build_psservice_argv(
            "PsService64.exe",
            "find",
            host="SHOULD_IGNORE",
            service_name="RustDesk",
            user="u",
            password="p",
        )
        self.assertNotIn(r"\\SHOULD_IGNORE", argv)
        self.assertNotIn("-u", argv)
        self.assertNotIn("-p", argv)
        self.assertEqual(argv[-2:], ["find", "RustDesk"])

    def test_password_redaction(self) -> None:
        password = "SuperSecret#123"
        argv = build_psservice_argv(
            "PsService64.exe",
            "stop",
            host="HOST",
            service_name="Spooler",
            user="u",
            password=password,
        )
        joined = " ".join(argv)
        self.assertIn(password, joined)
        safe = redact_command_text(joined, passwords=[password])
        self.assertNotIn(password, safe)
        self.assertIn("********", safe)


class ServicosErrorTests(unittest.TestCase):
    def test_classify_and_friendly(self) -> None:
        self.assertEqual(
            classify_psservice_error("Access is denied."), "acesso_negado"
        )
        self.assertIn(
            "Acesso negado",
            friendly_error_message("acesso_negado"),
        )

    def test_host_mismatch_generation(self) -> None:
        """Resultados de outro host não devem ser mesclados como se fossem do atual."""
        a = merge_query_and_config(
            parse_psservice_query(QUERY_SAMPLE),
            parse_psservice_config(CONFIG_SAMPLE),
            host="HOST_A",
        )
        b = merge_query_and_config(
            parse_psservice_query(QUERY_SAMPLE),
            parse_psservice_config(CONFIG_SAMPLE),
            host="HOST_B",
        )
        self.assertTrue(all(r.host == "HOST_A" for r in a))
        self.assertTrue(all(r.host == "HOST_B" for r in b))
        self.assertNotEqual(a[0].host, b[0].host)


if __name__ == "__main__":
    unittest.main()
