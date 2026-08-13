"""Parser da tabela textual do winget: IDs com dígito (7zip.7zip) e Store."""

from __future__ import annotations

import unittest

from remoteops.ui.winget.parsers.winget_text import (
    _looks_like_pkg_id,
    parse_winget_list,
    parse_winget_search,
    parse_winget_upgrade,
)

_SEARCH_7ZIP = """
Name                            Id                           Version      Match      Source
-------------------------------------------------------------------------------------------
7-Zip                           7zip.7zip                    24.09                   winget
7-Zip Pre-release               7zip.7zip.PreRelease         25.00                   winget
7-Zip ZS                        mcmilk.7zip-zstd             24.09                   winget
7-Zip                           XP8K0HKJFRXB78               Unknown                 msstore
Google Chrome                   Google.Chrome                128.0.6613.85           winget
""".strip()

_SEARCH_EXACT_ID = """
Name  Id         Version Source
-------------------------------
7-Zip 7zip.7zip  24.09   winget
""".strip()

_LIST_7ZIP = """
Name    Id         Version  Available  Source
---------------------------------------------
7-Zip   7zip.7zip  24.08    24.09      winget
""".strip()

_UPGRADE_7ZIP = """
Name    Id         Version  Available  Source
---------------------------------------------
7-Zip   7zip.7zip  24.08    24.09      winget
2 upgrades available.
""".strip()


class TestLooksLikePkgId(unittest.TestCase):
    def test_numeric_publisher_and_package(self) -> None:
        self.assertTrue(_looks_like_pkg_id("7zip.7zip"))
        self.assertTrue(_looks_like_pkg_id("7zip.7zip.PreRelease"))
        self.assertTrue(_looks_like_pkg_id("mcmilk.7zip-zstd"))
        self.assertTrue(_looks_like_pkg_id("Google.Chrome"))

    def test_rejects_bare_versions(self) -> None:
        self.assertFalse(_looks_like_pkg_id("24.09"))
        self.assertFalse(_looks_like_pkg_id("1.2.3"))
        self.assertFalse(_looks_like_pkg_id("128.0.6613.85"))

    def test_store_ids(self) -> None:
        self.assertTrue(_looks_like_pkg_id("XP8K0HKJFRXB78"))
        self.assertTrue(_looks_like_pkg_id("9N8G7FCLQFXR"))
        self.assertFalse(_looks_like_pkg_id("Unknown"))
        self.assertFalse(_looks_like_pkg_id("winget"))


class TestParseWingetSearch(unittest.TestCase):
    def test_keeps_packages_with_digits_in_id(self) -> None:
        rows = parse_winget_search(_SEARCH_7ZIP.splitlines())
        ids = [r["Id"] for r in rows]
        self.assertEqual(
            ids,
            [
                "7zip.7zip",
                "7zip.7zip.PreRelease",
                "mcmilk.7zip-zstd",
                "XP8K0HKJFRXB78",
                "Google.Chrome",
            ],
        )
        self.assertEqual(rows[0]["Name"], "7-Zip")
        self.assertEqual(rows[0]["Version"], "24.09")
        self.assertEqual(rows[0]["Source"], "winget")
        self.assertEqual(rows[3]["Source"], "msstore")

    def test_exact_id_query_table(self) -> None:
        rows = parse_winget_search(_SEARCH_EXACT_ID.splitlines())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Id"], "7zip.7zip")
        self.assertEqual(rows[0]["Name"], "7-Zip")


class TestParseWingetListAndUpgrade(unittest.TestCase):
    def test_list_keeps_7zip(self) -> None:
        rows = parse_winget_list(_LIST_7ZIP.splitlines())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Id"], "7zip.7zip")
        self.assertEqual(rows[0]["Version"], "24.08")
        self.assertEqual(rows[0]["Available"], "24.09")

    def test_upgrade_keeps_7zip(self) -> None:
        rows = parse_winget_upgrade(_UPGRADE_7ZIP.splitlines())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Id"], "7zip.7zip")
        self.assertEqual(rows[0]["Available"], "24.09")


if __name__ == "__main__":
    unittest.main()
