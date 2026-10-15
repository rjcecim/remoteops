"""Validação de destino Robocopy e switches /R /W."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.core.robocopy_dest import (
    DEFAULT_RETRIES,
    DEFAULT_WAIT_SECONDS,
    build_robocopy_switch_argv,
    coerce_retries,
    coerce_wait_seconds,
    file_belongs_to_folder,
    normalize_robocopy_dest,
    remote_local_path,
    remote_unc_path,
    validate_robocopy_dest,
)


class DestValidationTests(unittest.TestCase):
    def test_default_forms(self) -> None:
        for raw in ("temp", "C:\\temp", "C:/temp", "  C:\\temp  "):
            canonical, err = validate_robocopy_dest(raw)
            self.assertEqual(err, "", msg=raw)
            self.assertEqual(canonical, "temp")

    def test_nested_scripts(self) -> None:
        canonical, err = validate_robocopy_dest(r"C:\temp\scripts")
        self.assertEqual(err, "")
        self.assertEqual(canonical, r"temp\scripts")

    def test_spaces_and_unicode(self) -> None:
        canonical, err = validate_robocopy_dest(r"C:\temp\lote msi")
        self.assertEqual(err, "")
        self.assertEqual(canonical, r"temp\lote msi")
        canonical, err = validate_robocopy_dest(r"C:\temp\pacotes")
        self.assertEqual(err, "")
        accented, err = validate_robocopy_dest(r"C:\temp\instalação")
        self.assertEqual(err, "")
        self.assertIn("instalação", accented)

    def test_empty_rejected(self) -> None:
        _, err = validate_robocopy_dest("")
        self.assertTrue(err)

    def test_unc_rejected(self) -> None:
        _, err = validate_robocopy_dest(r"\\PC01\C$\temp")
        self.assertIn("UNC", err)

    def test_other_drive_rejected(self) -> None:
        _, err = validate_robocopy_dest(r"D:\temp")
        self.assertIn("C:", err)

    def test_dot_segments_rejected(self) -> None:
        _, err = validate_robocopy_dest(r"C:\temp\..\Windows")
        self.assertTrue(err)
        _, err = validate_robocopy_dest(r"temp\.")
        self.assertTrue(err)

    def test_invalid_chars_rejected(self) -> None:
        _, err = validate_robocopy_dest(r"C:\temp\a|b")
        self.assertIn("inválido", err.lower())

    def test_reserved_names_rejected(self) -> None:
        _, err = validate_robocopy_dest(r"C:\temp\CON")
        self.assertIn("reservado", err.lower())
        _, err = validate_robocopy_dest(r"C:\temp\nul.txt")
        self.assertIn("reservado", err.lower())

    def test_c_dollar_rejected(self) -> None:
        _, err = validate_robocopy_dest(r"C$\temp")
        self.assertTrue(err)

    def test_normalize_preserves_dots_for_validation(self) -> None:
        self.assertIn("..", normalize_robocopy_dest(r"temp\..\x"))

    def test_unc_and_local_paths(self) -> None:
        self.assertEqual(remote_local_path("temp", "setup.msi"), r"C:\temp\setup.msi")
        self.assertEqual(
            remote_unc_path("PC01", "temp", "pasta"),
            r"\\PC01\C$\temp\pasta",
        )


class SwitchArgvTests(unittest.TestCase):
    def test_defaults_when_switches_none(self) -> None:
        argv = build_robocopy_switch_argv(None)
        for flag in ("/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP"):
            self.assertIn(flag, argv)
        self.assertIn(f"/R:{DEFAULT_RETRIES}", argv)
        self.assertIn(f"/W:{DEFAULT_WAIT_SECONDS}", argv)

    def test_empty_switches_still_emit_r_w(self) -> None:
        argv = build_robocopy_switch_argv("")
        self.assertEqual(argv, [f"/R:{DEFAULT_RETRIES}", f"/W:{DEFAULT_WAIT_SECONDS}"])

    def test_strips_duplicate_r_w_from_switches(self) -> None:
        argv = build_robocopy_switch_argv("/NFL /R:99 /W:99", retries=2, wait_seconds=3)
        self.assertEqual(argv.count("/R:2"), 1)
        self.assertEqual(argv.count("/W:3"), 1)
        self.assertNotIn("/R:99", argv)

    def test_blocks_destructive_and_link_switches(self) -> None:
        argv = build_robocopy_switch_argv("/NFL /MIR /PURGE /SL /SJ", retries=1, wait_seconds=1)
        joined = " ".join(argv).upper()
        for bad in ("/MIR", "/PURGE", "/SL", "/SJ"):
            self.assertNotIn(bad, joined)
        self.assertIn("/NFL", argv)

    def test_coerce_rejects_unknown(self) -> None:
        self.assertEqual(coerce_retries(99), DEFAULT_RETRIES)
        self.assertEqual(coerce_wait_seconds(-1), DEFAULT_WAIT_SECONDS)
        self.assertEqual(coerce_retries(5), 5)
        self.assertEqual(coerce_wait_seconds(10), 10)


class FolderBelongingTests(unittest.TestCase):
    def test_file_inside_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "sub")
            os.makedirs(nested)
            path = os.path.join(nested, "a.exe")
            with open(path, "wb") as fh:
                fh.write(b"x")
            self.assertTrue(file_belongs_to_folder(path, tmp))

    def test_file_outside_folder(self) -> None:
        with tempfile.TemporaryDirectory() as a:
            with tempfile.TemporaryDirectory() as b:
                path = os.path.join(a, "a.exe")
                with open(path, "wb") as fh:
                    fh.write(b"x")
                self.assertFalse(file_belongs_to_folder(path, b))


if __name__ == "__main__":
    unittest.main()
