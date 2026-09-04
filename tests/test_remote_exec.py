"""Consulta remota via PsExec/PowerShell: JSON e argv do .exe windowed."""

from __future__ import annotations

import json
import unittest

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.powershell_options import decode_encoded_command
from remoteops.utils.inventory.remote_exec import (
    build_remote_powershell_argv,
    parse_json_output,
    wrap_remote_ps_script,
)
from remoteops.utils.local_accounts import LOCAL_ACCOUNTS_QUERY_SCRIPT


class ParseJsonOutputTests(unittest.TestCase):
    def test_plain_array(self) -> None:
        data, err = parse_json_output('[{"Name":"tce_admin"}]')
        self.assertEqual(err, "")
        self.assertEqual(data[0]["Name"], "tce_admin")

    def test_prefix_garbage_then_json(self) -> None:
        data, err = parse_json_output('Connecting...\r\n[{"Name":"x"}]')
        self.assertEqual(err, "")
        self.assertEqual(data[0]["Name"], "x")

    def test_nul_interleaved_utf16_misdecode(self) -> None:
        raw = "\x00".join(list('[{"Name":"a"}]'))
        data, err = parse_json_output(raw)
        self.assertEqual(err, "")
        self.assertEqual(data[0]["Name"], "a")

    def test_invalid_includes_preview(self) -> None:
        data, err = parse_json_output("The handle is invalid.")
        self.assertIsNone(data)
        self.assertIn("Resposta não é JSON válido", err)
        self.assertIn("handle is invalid", err.lower())


class WrapAndArgvTests(unittest.TestCase):
    def test_wrap_sets_utf8_and_silences_progress(self) -> None:
        wrapped = wrap_remote_ps_script(" '[]' ")
        self.assertIn("UTF8Encoding", wrapped)
        self.assertIn("SilentlyContinue", wrapped)
        self.assertIn("'[]'", wrapped)

    def test_argv_uses_encoded_command_not_dash_command(self) -> None:
        argv = build_remote_powershell_argv(
            "ETSETIN-CAU11",
            LOCAL_ACCOUNTS_QUERY_SCRIPT,
            pstools_dir=r"C:\PSTools",
        )
        self.assertIn("-EncodedCommand", argv)
        self.assertNotIn("-Command", argv)
        blob = argv[argv.index("-EncodedCommand") + 1]
        text, err = decode_encoded_command(blob)
        self.assertIsNone(err)
        self.assertIn("LocalAccount=True", text or "")
        self.assertIn("UTF8Encoding", text or "")


class ConsoleCodecTests(unittest.TestCase):
    def test_utf16le_json_without_bom(self) -> None:
        payload = json.dumps([{"Name": "tce_admin"}]).encode("utf-16le")
        text = decode_console_bytes(payload)
        data = json.loads(text)
        self.assertEqual(data[0]["Name"], "tce_admin")


if __name__ == "__main__":
    unittest.main()
