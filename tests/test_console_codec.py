"""Testes do codec de console canónico."""

from __future__ import annotations

import unittest

from remoteops.core.console_codec import decode_best_effort, decode_console_bytes


class ConsoleCodecTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(decode_console_bytes(b""), "")

    def test_utf8(self) -> None:
        self.assertEqual(decode_console_bytes("olá".encode("utf-8")), "olá")

    def test_utf8_bom(self) -> None:
        raw = b"\xef\xbb\xbf" + "teste".encode("utf-8")
        self.assertEqual(decode_console_bytes(raw), "teste")

    def test_utf16_le_bom(self) -> None:
        raw = "host".encode("utf-16")  # inclui BOM LE no Windows
        self.assertEqual(decode_console_bytes(raw), "host")

    def test_utf16le_without_bom_heuristic(self) -> None:
        # CLIXML-like: muitos NULs intercalados
        raw = "ABCDEFGH".encode("utf-16le")
        self.assertEqual(decode_console_bytes(raw), "ABCDEFGH")

    def test_alias(self) -> None:
        self.assertIs(decode_best_effort, decode_console_bytes)


if __name__ == "__main__":
    unittest.main()
