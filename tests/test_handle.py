"""Testes de Handle / PsExec+Handle — sem rede e sem executável remoto."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.utils.handle import (
    build_handle_local_argv,
    build_remote_handle_argv,
    classify_handle_error,
    handle_available,
    is_handle_usage_text,
    parse_handle_close_result,
    parse_handle_csv,
    parse_handle_output,
    parse_handle_text,
    resolve_handle_exe,
)


_CSV_WITH_USER = """Process\tPID\tUser\tHandle\tType\tShare Flags\tName\tAccess
EXCEL.EXE\t5824\tFile\tDOMAIN\\usuario\t0x0000021C\tC:\\Dados\\Base.xlsx
sihost.exe\t6676\tFile\tTCE-PA\\0101093\t0x0000005C\tC:\\Windows\\System32 
"""

_CSV_NO_USER = """Process\tPID\tType\tHandle\tName
explorer.exe\t14504\tFile\t0x000001C0\tC:\\Program Files\\App with spaces\\file.dat
"""

_TEXT_SEARCH = """
EXCEL.EXE       pid: 5824   type: File          DOMAIN\\usuario    21C: C:\\Dados\\Relatorio.xlsx
"""

_NO_MATCH = "No matching handles found."

_USAGE = """
usage: handle [[-a [-l]] [-v|-vt] [-u] | [-c <handle> [-y]] | [-s]] [-p <process>|<pid>] [name] [-nobanner]
  -v         CSV output with comma delimiter.
  -vt        CSV output with tab delimiter.
  -nobanner  Do not display the startup banner and copyright message.
"""


class TestHandleResolve(unittest.TestCase):
    def test_prefers_64bit_in_handle_dir(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "handle64.exe"), "wb").close()
            open(os.path.join(folder, "handle.exe"), "wb").close()
            resolved = resolve_handle_exe(folder)
            self.assertEqual(os.path.basename(resolved).lower(), "handle64.exe")
            self.assertTrue(handle_available(folder))

    def test_missing_binary_points_to_expected_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            resolved = resolve_handle_exe(folder)
            self.assertTrue(
                resolved.endswith("handle64.exe") or resolved.endswith("Handle64.exe")
            )
            self.assertFalse(handle_available(folder))

    def test_prefers_64_falls_back_to_32(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "handle.exe"), "wb").close()
            resolved = resolve_handle_exe(folder)
            self.assertEqual(os.path.basename(resolved).lower(), "handle.exe")
            self.assertTrue(handle_available(folder))


class TestHandleArgv(unittest.TestCase):
    def test_local_search(self) -> None:
        argv = build_handle_local_argv(
            r"C:\Handle\handle64.exe",
            search="Base.xlsx",
        )
        self.assertEqual(
            argv,
            [
                r"C:\Handle\handle64.exe",
                "-accepteula",
                "-nobanner",
                "-u",
                "-vt",
                "Base.xlsx",
            ],
        )

    def test_local_requires_search_or_list_all(self) -> None:
        self.assertEqual(
            build_handle_local_argv(r"C:\Handle\handle64.exe"),
            [],
        )
        argv = build_handle_local_argv(
            r"C:\Handle\handle64.exe", list_all=True
        )
        self.assertIn("-vt", argv)
        self.assertNotIn("Base.xlsx", argv)

    def test_local_close(self) -> None:
        argv = build_handle_local_argv(
            r"C:\Handle\handle64.exe",
            close_handle="0x21C",
            close_pid=5824,
        )
        self.assertEqual(
            argv[-5:],
            ["-c", "21C", "-p", "5824", "-y"],
        )

    def test_remote_psexec_copy(self) -> None:
        argv = build_remote_handle_argv(
            r"C:\Handle\handle64.exe",
            "PC001",
            psexec_exe=r"C:\PSTools\PsExec64.exe",
            search="Base.xlsx",
            user="DOMAIN\\admin",
            password="secret",
        )
        self.assertEqual(argv[0], r"C:\PSTools\PsExec64.exe")
        self.assertEqual(argv[1], "\\\\PC001")
        self.assertIn("-u", argv)
        self.assertIn("secret", argv)
        self.assertIn("-c", argv)
        self.assertIn("-f", argv)
        self.assertIn("-h", argv)
        self.assertIn(r"C:\Handle\handle64.exe", argv)
        self.assertIn("Base.xlsx", argv)
        # -c do Handle não deve aparecer na pesquisa
        handle_idx = argv.index(r"C:\Handle\handle64.exe")
        handle_part = argv[handle_idx:]
        self.assertNotIn("-c", handle_part[1:])


class TestHandleParse(unittest.TestCase):
    def test_csv_with_user_buggy_header(self) -> None:
        rows = parse_handle_csv(_CSV_WITH_USER)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].process_name, "EXCEL.EXE")
        self.assertEqual(rows[0].pid, 5824)
        self.assertEqual(rows[0].object_type, "File")
        self.assertEqual(rows[0].user, r"DOMAIN\usuario")
        self.assertEqual(rows[0].handle, "21C")
        self.assertEqual(rows[0].path, r"C:\Dados\Base.xlsx")

    def test_csv_no_user(self) -> None:
        rows = parse_handle_csv(_CSV_NO_USER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pid, 14504)
        self.assertIn("spaces", rows[0].path)
        self.assertEqual(rows[0].handle, "1C0")
        self.assertEqual(rows[0].user, "")

    def test_text_search(self) -> None:
        rows = parse_handle_text(_TEXT_SEARCH)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].handle, "21C")
        self.assertIn("Relatorio.xlsx", rows[0].path)

    def test_no_match(self) -> None:
        self.assertEqual(parse_handle_output(_NO_MATCH), [])
        self.assertEqual(classify_handle_error(_NO_MATCH, 1), "ok")
        self.assertEqual(classify_handle_error(_NO_MATCH, 0), "ok")

    def test_usage(self) -> None:
        self.assertTrue(is_handle_usage_text(_USAGE))
        self.assertEqual(classify_handle_error(_USAGE, 1), "usage")

    def test_close_result(self) -> None:
        ok, _ = parse_handle_close_result("Handle closed.")
        self.assertTrue(ok)

    def test_embedded_newline_does_not_raise(self) -> None:
        """Saída completa via PsExec pode misturar CR/LF e quebrar csv.reader."""
        blob = (
            "Process\tPID\tUser\tHandle\tType\tShare Flags\tName\tAccess\n"
            "EXCEL.EXE\t5824\tFile\tDOMAIN\\usuario\t0x0000021C\tC:\\Dados\\Base.xlsx\n"
            "weird.exe\t99\tFile\tDOMAIN\\u\t0x00000010\tC:\\path\n"
            "with\n"  # newline solto no meio do fluxo
            "sihost.exe\t6676\tFile\tTCE-PA\\0101093\t0x0000005C\tC:\\Windows\\System32\n"
        )
        rows = parse_handle_output(blob)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0].handle, "21C")


if __name__ == "__main__":
    unittest.main()
