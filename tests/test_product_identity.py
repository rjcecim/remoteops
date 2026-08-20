"""Testes da identificação do EXE e da comparação numérica de versão."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from remoteops.utils.product_identity import (
    ExeMetadata,
    compare_versions,
    identify_product,
    match_identity_app,
    valid_numeric_version,
)
from remoteops.utils.psinfo import InstalledApp


def _app(name: str, version: str = "", arch: str = "64") -> InstalledApp:
    return InstalledApp(
        display_name=name,
        version=version,
        publisher="",
        display_line=f"{name} {version}".strip(),
        product_code="",
        uninstall_string="",
        quiet_uninstall_string="",
        is_msi=False,
        arch=arch,
    )


class VersionCompareTests(unittest.TestCase):
    def test_numeric_4_10_greater_than_4_9(self) -> None:
        self.assertEqual(compare_versions("4.10.0", "4.9.0"), 1)
        self.assertEqual(compare_versions("4.9.0", "4.10.0"), -1)

    def test_equal_with_padding(self) -> None:
        self.assertEqual(compare_versions("2.3", "2.3.0"), 0)

    def test_invalid_is_none(self) -> None:
        self.assertIsNone(compare_versions("abc", "1.0"))
        self.assertEqual(valid_numeric_version("N/A"), "")
        self.assertEqual(valid_numeric_version("2.3.0"), "2.3.0")


class ExeVersionTests(unittest.TestCase):
    def test_product_version_preferred(self) -> None:
        meta = ExeMetadata(
            path="setup.exe",
            product_version="2.3.0",
            file_version="1.0.0",
        )
        self.assertEqual(meta.installer_version, "2.3.0")

    def test_file_version_fallback(self) -> None:
        meta = ExeMetadata(
            path="setup.exe",
            product_version="",
            file_version="2.3.0",
        )
        self.assertEqual(meta.installer_version, "2.3.0")

    def test_invalid_product_version_falls_back_to_file(self) -> None:
        meta = ExeMetadata(
            path="setup.exe",
            product_version="N/A",
            file_version="2.3.0",
        )
        self.assertEqual(meta.installer_version, "2.3.0")

    def test_no_numeric_version(self) -> None:
        meta = ExeMetadata(path="setup.exe", product_version="N/A", file_version="")
        self.assertEqual(meta.installer_version, "")


class IdentifyProductTests(unittest.TestCase):
    def setUp(self) -> None:
        self._catalog = patch(
            "remoteops.utils.product_identity.find_catalog_entry",
            return_value=None,
        )
        self._catalog.start()
        self.addCleanup(self._catalog.stop)

    def test_product_name_before_filename(self) -> None:
        meta = ExeMetadata(
            path=r"C:\tmp\produto-x64-230.exe",
            product_name="Produto Exemplo",
            file_stem="produto-x64-230",
        )
        identity = identify_product(meta.path, meta)
        self.assertEqual(identity.needles[0], "Produto Exemplo")
        self.assertTrue(
            any("produto-x64-230" in n.casefold() for n in identity.filename_needles)
            or any(n.casefold() == "produto" for n in identity.filename_needles)
        )
        found = match_identity_app([_app("Produto Exemplo", "2.1.0")], identity)
        self.assertIsNotNone(found)
        self.assertEqual(found.display_name, "Produto Exemplo")

    def test_file_description_when_no_product_name(self) -> None:
        meta = ExeMetadata(
            path=r"C:\tmp\setup.exe",
            file_description="Produto Exemplo",
            file_stem="setup",
        )
        identity = identify_product(meta.path, meta)
        self.assertEqual(identity.needles[0], "Produto Exemplo")
        found = match_identity_app([_app("Produto Exemplo", "1.0")], identity)
        self.assertIsNotNone(found)

    def test_filename_fallback(self) -> None:
        meta = ExeMetadata(
            path=r"C:\tmp\produto-x64-230.exe",
            file_stem="produto-x64-230",
        )
        identity = identify_product(meta.path, meta)
        self.assertEqual(identity.needles, ())
        self.assertTrue(identity.filename_needles)
        found = match_identity_app([_app("Produto Exemplo", "2.1.0")], identity)
        self.assertIsNotNone(found)
        self.assertEqual(found.display_name, "Produto Exemplo")

    def test_does_not_pick_unrelated_app_from_generic_token(self) -> None:
        meta = ExeMetadata(
            path=r"C:\tmp\produto-x64-230.exe",
            product_name="Produto Exemplo",
            file_stem="produto-x64-230",
        )
        identity = identify_product(meta.path, meta)
        apps = [
            _app("SuperProduto Extra", "9.9.9"),
            _app("Produto Exemplo", "2.1.0"),
        ]
        found = match_identity_app(apps, identity)
        self.assertIsNotNone(found)
        self.assertEqual(found.display_name, "Produto Exemplo")

    def test_highest_version_among_matches(self) -> None:
        meta = ExeMetadata(
            path="setup.exe",
            product_name="Produto Exemplo",
            file_stem="setup",
        )
        identity = identify_product(meta.path, meta)
        found = match_identity_app(
            [
                _app("Produto Exemplo", "2.1.0"),
                _app("Produto Exemplo", "2.3.0"),
                _app("Produto Exemplo", "2.2.0"),
            ],
            identity,
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.version, "2.3.0")


if __name__ == "__main__":
    unittest.main()
