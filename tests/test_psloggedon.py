"""Testes de PsLoggedOn — sem rede e sem executável remoto."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.utils.psloggedon import (
    build_psloggedon_argv,
    classify_psloggedon_error,
    is_psloggedon_usage_text,
    parse_psloggedon_output,
    psloggedon_available,
    resolve_psloggedon_exe,
    split_account,
)


_LOCAL_AND_SHARE = """
PsLoggedon v1.35 - See who's logged on
Copyright (C) 2000-2016 Mark Russinovich
Sysinternals - www.sysinternals.com

Users logged on locally:
     31/08/2026 11:45:30    	TCE-PA\\0101093
     3/24/2020 2:28:50 PM     DOMAIN\\usuario02

Users logged on via resource shares:
     3/24/2020 5:24:23 PM     DOMAIN\\admin
"""

_EMPTY_SHARE = """
Users logged on locally:
     11/07/2012 19:05:37        WILLIAM-PC\\william

No one is logged on via resource shares.
"""

_NO_ONE = """
No one is logged on locally.
No one is logged on via resource shares.
"""

_USAGE = """
Usage: C:\\PSTools\\PsLoggedon64.exe [-l] [-x] [\\\\computername]
    or C:\\PSTools\\PsLoggedon64.exe [username]
-l     Show only local logons
-x     Don't show logon times
-nobanner Do not display the startup banner and copyright message.
"""


class TestPsLoggedOnResolve(unittest.TestCase):
    def test_prefers_64bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsLoggedon64.exe"), "wb").close()
            open(os.path.join(folder, "PsLoggedon.exe"), "wb").close()
            resolved = resolve_psloggedon_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsLoggedon64.exe")
            self.assertTrue(psloggedon_available(folder))

    def test_fallback_32bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsLoggedon.exe"), "wb").close()
            resolved = resolve_psloggedon_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsLoggedon.exe")

    def test_missing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(psloggedon_available(folder))


class TestPsLoggedOnArgv(unittest.TestCase):
    def test_basic_remote(self) -> None:
        argv = build_psloggedon_argv(
            r"C:\PSTools\PsLoggedon64.exe", "PC001"
        )
        self.assertEqual(
            argv,
            [
                r"C:\PSTools\PsLoggedon64.exe",
                "-accepteula",
                "-nobanner",
                "\\\\PC001",
            ],
        )

    def test_local_only_hide_times(self) -> None:
        argv = build_psloggedon_argv(
            "PsLoggedon64.exe",
            "pc01",
            local_only=True,
            hide_times=True,
        )
        self.assertIn("-l", argv)
        self.assertIn("-x", argv)
        self.assertEqual(argv[-1], "\\\\pc01")

    def test_empty_host(self) -> None:
        self.assertEqual(build_psloggedon_argv("exe", ""), [])


class TestPsLoggedOnParse(unittest.TestCase):
    def test_local_and_network(self) -> None:
        rows = parse_psloggedon_output(_LOCAL_AND_SHARE)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].display_user, "TCE-PA\\0101093")
        self.assertEqual(rows[0].logon_type, "Local")
        self.assertEqual(rows[0].logon_time, "31/08/2026 11:45:30")
        self.assertEqual(rows[1].username, "usuario02")
        self.assertEqual(rows[1].logon_time, "24/03/2020 14:28:50")
        self.assertEqual(rows[2].logon_type, "Rede")
        self.assertEqual(rows[2].domain, "DOMAIN")
        self.assertEqual(rows[2].logon_time, "24/03/2020 17:24:23")

    def test_empty_share_section(self) -> None:
        rows = parse_psloggedon_output(_EMPTY_SHARE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].logon_type, "Local")
        self.assertEqual(rows[0].logon_time, "11/07/2012 19:05:37")

    def test_no_one(self) -> None:
        self.assertEqual(parse_psloggedon_output(_NO_ONE), [])

    def test_usage(self) -> None:
        self.assertTrue(is_psloggedon_usage_text(_USAGE))
        self.assertEqual(classify_psloggedon_error(_USAGE, 1), "usage")

    def test_access_denied(self) -> None:
        self.assertEqual(
            classify_psloggedon_error("Access is denied.", 1),
            "access_denied",
        )

    def test_split_account(self) -> None:
        self.assertEqual(split_account(r"DOMAIN\user"), ("DOMAIN", "user"))
        self.assertEqual(split_account("localuser"), ("", "localuser"))


if __name__ == "__main__":
    unittest.main()
