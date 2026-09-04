"""Consulta remota via PsExec/PowerShell: JSON e argv do .exe windowed."""

from __future__ import annotations

import json
import unittest

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.powershell_options import decode_encoded_command
from remoteops.utils.inventory.remote_exec import (
    _shorten_ps_error,
    build_remote_powershell_argv,
    extract_b64_json,
    parse_json_output,
    wrap_remote_ps_script,
)
from remoteops.utils.local_accounts import (
    LOCAL_ACCOUNTS_QUERY_SCRIPT,
    parse_local_accounts_payload,
)


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

    def test_concatenated_root_objects_are_merged(self) -> None:
        raw = '{"Name":"Convidado"}{"Name":"tce_admin"}{"Name":"DefaultAccount"}'
        data, err = parse_json_output(raw)
        self.assertEqual(err, "")
        self.assertEqual(
            [row["Name"] for row in data],
            ["Convidado", "tce_admin", "DefaultAccount"],
        )


class LocalAccountsPayloadTests(unittest.TestCase):
    def test_numeric_keys_from_powershell_hashtable(self) -> None:
        data = {
            "0": {"Name": "Convidado", "SID": "S-1-5-21-1-2-3-501", "Domain": "ETSETIN-CAU11"},
            "1": {"Name": "tce_admin", "SID": "S-1-5-21-1-2-3-500", "Domain": "ETSETIN-CAU11"},
        }
        accounts, err = parse_local_accounts_payload(data, host="ETSETIN-CAU11")
        self.assertEqual(err, "")
        self.assertEqual([a.name for a in accounts], ["Convidado", "tce_admin"])

    def test_sid_object_from_powershell(self) -> None:
        data = [
            {
                "Name": "tce_admin",
                "SID": {"Value": "S-1-5-21-1-2-3-500"},
                "Domain": "ETSETIN-CAU01",
            }
        ]
        accounts, err = parse_local_accounts_payload(data, host="ETSETIN-CAU01")
        self.assertEqual(err, "")
        self.assertEqual(accounts[0].sid, "S-1-5-21-1-2-3-500")
        self.assertEqual(accounts[0].rid, 500)

    def test_script_uses_inputobject(self) -> None:
        self.assertIn("ConvertTo-Json -InputObject", LOCAL_ACCOUNTS_QUERY_SCRIPT)
        self.assertIn("Get-LocalUser", LOCAL_ACCOUNTS_QUERY_SCRIPT)
        self.assertIn("Write-RemoteOpsJson", LOCAL_ACCOUNTS_QUERY_SCRIPT)
        self.assertNotIn("[Console]::Out.Write", LOCAL_ACCOUNTS_QUERY_SCRIPT)


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
        self.assertIn("Get-LocalUser", text or "")
        self.assertIn("UTF8Encoding", text or "")

    def test_wrap_with_result_path_emits_file_and_b64(self) -> None:
        wrapped = wrap_remote_ps_script("'[]'", result_path=r"C:\Windows\Temp\ro.json")
        self.assertIn("Write-RemoteOpsJson", wrapped)
        self.assertIn("WriteAllText", wrapped)
        self.assertIn("__REMOTEOPS_B64__", wrapped)
        self.assertIn(r"C:\Windows\Temp\ro.json", wrapped)
        self.assertNotIn("$__roPipe = . {", wrapped)

    def test_extract_b64_json(self) -> None:
        payload = '[{"Name":"tce_admin"},{"Name":"Convidado"}]'
        import base64

        marked = "__REMOTEOPS_B64__" + base64.b64encode(payload.encode("utf-8")).decode("ascii")
        decoded = extract_b64_json("noise\n" + marked + "\n")
        self.assertEqual(decoded, payload)
        data, err = parse_json_output(marked)
        self.assertEqual(err, "")
        self.assertEqual([row["Name"] for row in data], ["tce_admin", "Convidado"])


class PsExecNoiseTests(unittest.TestCase):
    def test_connecting_line_is_not_the_user_error(self) -> None:
        msg = _shorten_ps_error("Connecting to ETSETIN-CAU11...\r\nAccess is denied.")
        self.assertIn("Acesso negado", msg)
        self.assertNotIn("Connecting", msg)

    def test_only_connecting_line_falls_back(self) -> None:
        msg = _shorten_ps_error("Connecting to ETSETIN-CAU11...")
        self.assertEqual(msg, "Não foi possível consultar.")


class ConsoleCodecTests(unittest.TestCase):
    def test_utf16le_json_without_bom(self) -> None:
        payload = json.dumps([{"Name": "tce_admin"}]).encode("utf-16le")
        text = decode_console_bytes(payload)
        data = json.loads(text)
        self.assertEqual(data[0]["Name"], "tce_admin")


if __name__ == "__main__":
    unittest.main()
