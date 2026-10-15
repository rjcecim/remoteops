"""Pipeline de saída: UTF-8, ANSI, CR, redação fragmentada e linhas ConPTY."""

from __future__ import annotations

import unittest

from remoteops.core.console_stream import (
    ConPtyTextProcessor,
    IncrementalUtf8Decoder,
    StreamingAnsiFilter,
    StreamRedactor,
    emit_conpty_text,
    strip_ansi,
)
from remoteops.utils.redaction import REDACTED


class StripAnsiTests(unittest.TestCase):
    def test_preserves_carriage_return(self) -> None:
        text = strip_ansi("a\rb\n")
        self.assertIn("\r", text)
        self.assertEqual(text, "a\rb\n")

    def test_normalizes_crlf(self) -> None:
        self.assertEqual(strip_ansi("a\r\nb"), "a\nb")

    def test_removes_csi_color(self) -> None:
        self.assertEqual(strip_ansi("\x1b[32mOK\x1b[0m"), "OK")

    def test_normalize_cr_opt_in(self) -> None:
        self.assertEqual(strip_ansi("a\rb", normalize_cr=True), "a\nb")


class IncrementalUtf8Tests(unittest.TestCase):
    def test_multibyte_split_across_chunks(self) -> None:
        # "ç" em UTF-8 = C3 A7
        dec = IncrementalUtf8Decoder()
        self.assertEqual(dec.feed(b"\xc3"), "")
        self.assertEqual(dec.feed(b"\xa7"), "ç")
        self.assertEqual(dec.finish(), "")

    def test_emoji_split(self) -> None:
        raw = "🙂".encode("utf-8")
        dec = IncrementalUtf8Decoder()
        mid = len(raw) // 2
        self.assertEqual(dec.feed(raw[:mid]), "")
        self.assertEqual(dec.feed(raw[mid:]), "🙂")


class StreamingAnsiTests(unittest.TestCase):
    def test_csi_split_across_feeds(self) -> None:
        filt = StreamingAnsiFilter()
        self.assertEqual(filt.feed("\x1b[32"), "")
        self.assertEqual(filt.feed("mOK\x1b[0m"), "OK")
        self.assertEqual(filt.finish(), "")

    def test_preserves_cr_for_progress(self) -> None:
        filt = StreamingAnsiFilter()
        out = filt.feed("10%\r20%\n")
        self.assertEqual(out, "10%\r20%\n")


class StreamRedactorTests(unittest.TestCase):
    def test_password_split_across_chunks(self) -> None:
        secret = "SuperSecret99"
        red = StreamRedactor([secret])
        part1 = red.feed("echo " + secret[:6])
        part2 = red.feed(secret[6:] + " done")
        full = part1 + part2 + red.finish()
        self.assertNotIn(secret, full)
        self.assertIn(REDACTED, full)
        self.assertIn("echo ", full)
        self.assertIn(" done", full)

    def test_complete_password_in_one_chunk(self) -> None:
        secret = "abc123"
        red = StreamRedactor([secret])
        out = red.feed(f"x {secret} y") + red.finish()
        self.assertEqual(out, f"x {REDACTED} y")

    def test_no_false_hold_without_prefix(self) -> None:
        red = StreamRedactor(["password"])
        out = red.feed("hello\n") + red.finish()
        self.assertEqual(out, "hello\n")


class ConPtyTextProcessorTests(unittest.TestCase):
    def test_utf8_and_ansi_pipeline(self) -> None:
        proc = ConPtyTextProcessor()
        # "ção" colorido; byte de "ç" dividido entre chunks.
        self.assertEqual(proc.feed_bytes(b"\x1b[31m\xc3"), "")
        self.assertEqual(proc.feed_bytes(b"\xa7ao\x1b[0m\n"), "\u00e7ao\n")


class EmitConptyTextTests(unittest.TestCase):
    def test_cr_keeps_last_segment(self) -> None:
        lines: list[str] = []
        partials: list[str] = []
        carry: list[str] = []
        emit_conpty_text(
            "10%\r50%\r100%\n",
            on_line=lines.append,
            carry=carry,
            on_partial=partials.append,
        )
        self.assertEqual(lines, ["100%"])
        self.assertEqual(partials[-1], "")

    def test_partial_prompt(self) -> None:
        lines: list[str] = []
        partials: list[str] = []
        carry: list[str] = []
        emit_conpty_text(
            "C:\\>",
            on_line=lines.append,
            carry=carry,
            on_partial=partials.append,
        )
        self.assertEqual(lines, [])
        self.assertEqual(partials[-1], "C:\\>")
        self.assertEqual(carry, ["C:\\>"])

    def test_progress_then_line(self) -> None:
        lines: list[str] = []
        carry: list[str] = []
        emit_conpty_text("a\rb\nfinal\n", on_line=lines.append, carry=carry)
        self.assertEqual(lines, ["b", "final"])


if __name__ == "__main__":
    unittest.main()
