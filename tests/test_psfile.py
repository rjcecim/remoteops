"""Testes de PsFile — sem rede e sem executável remoto."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.utils.psfile import (
    build_psfile_argv,
    classify_psfile_error,
    is_psfile_usage_text,
    parse_psfile_close_result,
    parse_psfile_output,
    psfile_available,
    resolve_psfile_exe,
)


_SAMPLE = """
PsFile v1.04 - Lists files and directories opened remotely
Copyright (C) 2001-2023 Mark Russinovich
Sysinternals

Files opened remotely on PC001:
[124] C:\\Dados\\Relatorio.xlsx
  User:   DOMAIN\\usuario02
  Locks:  0
  Access: Read

[2214593372] D:\\PATH TO\\FILE.PDF
User: USERNAME
Locks: 2
Access: Read Write
"""

_EMPTY = """
Files opened remotely on PC001:

No files opened remotely on PC001.
"""

_USAGE = """
Usage: C:\\PSTools\\psfile64.exe [\\\\RemoteComputer [-u Username [-p Password]]] [[Id | path] [-c]]
     -u        Specifies optional user name for login to
               remote computer.
     -p        Specifies password for user name.
     Id        Id of file to print information for or close.
     Path      Full or partial path of files to match.
     -c        Closes file identified by file Id.
     -nobanner Do not display the startup banner and copyright message.
"""

_CLOSED = "Closed file C:\\Dados\\Relatorio.xlsx on PC001."


class TestPsFileResolve(unittest.TestCase):
    def test_prefers_64bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "psfile64.exe"), "wb").close()
            open(os.path.join(folder, "psfile.exe"), "wb").close()
            resolved = resolve_psfile_exe(folder)
            self.assertEqual(os.path.basename(resolved).lower(), "psfile64.exe")
            self.assertTrue(psfile_available(folder))

    def test_missing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(psfile_available(folder))


class TestPsFileArgv(unittest.TestCase):
    def test_list(self) -> None:
        argv = build_psfile_argv(
            r"C:\PSTools\psfile64.exe",
            "PC001",
            user="DOMAIN\\admin",
            password="secret",
        )
        self.assertEqual(
            argv,
            [
                r"C:\PSTools\psfile64.exe",
                "-accepteula",
                "-nobanner",
                "\\\\PC001",
                "-u",
                "DOMAIN\\admin",
                "-p",
                "secret",
            ],
        )

    def test_close_by_id(self) -> None:
        argv = build_psfile_argv(
            "psfile64.exe",
            "PC001",
            file_id="124",
            close=True,
        )
        self.assertEqual(argv[-2:], ["124", "-c"])

    def test_close_requires_selector(self) -> None:
        self.assertEqual(
            build_psfile_argv("psfile64.exe", "PC001", close=True),
            [],
        )


class TestPsFileParse(unittest.TestCase):
    def test_ids_user_path_locks(self) -> None:
        rows = parse_psfile_output(_SAMPLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].id, "124")
        self.assertEqual(rows[0].username, "DOMAIN\\usuario02")
        self.assertEqual(rows[0].path, r"C:\Dados\Relatorio.xlsx")
        self.assertEqual(rows[0].locks, 0)
        self.assertEqual(rows[0].permissions, "Read")
        self.assertEqual(rows[1].id, "2214593372")
        self.assertEqual(rows[1].locks, 2)
        self.assertIn("FILE.PDF", rows[1].path)

    def test_empty(self) -> None:
        self.assertEqual(parse_psfile_output(_EMPTY), [])

    def test_usage(self) -> None:
        self.assertTrue(is_psfile_usage_text(_USAGE))
        self.assertEqual(classify_psfile_error(_USAGE, 1), "usage")

    def test_close_ok(self) -> None:
        ok, path = parse_psfile_close_result(_CLOSED)
        self.assertTrue(ok)
        self.assertIn("Relatorio.xlsx", path)

    def test_close_already_gone(self) -> None:
        ok, msg = parse_psfile_close_result(
            "No files opened remotely on PC001."
        )
        self.assertFalse(ok)
        self.assertIn("fechado", msg.casefold())


if __name__ == "__main__":
    unittest.main()
