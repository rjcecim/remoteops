"""Consulta remota via PsExec/PowerShell: JSON e argv do .exe windowed."""

from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from remoteops.core.console_codec import decode_console_bytes
from remoteops.core.powershell_options import decode_encoded_command
from remoteops.utils.inventory.remote_exec import (
    _B64_MARK,
    _shorten_ps_error,
    build_remote_powershell_argv,
    extract_b64_json,
    parse_json_output,
    run_remote_powershell,
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

    def test_bom_object(self) -> None:
        data, err = parse_json_output('\ufeff{"Name":"Intel(R) UHD Graphics 630"}')
        self.assertEqual(err, "")
        self.assertEqual(data["Name"], "Intel(R) UHD Graphics 630")

    def test_empty_array(self) -> None:
        data, err = parse_json_output("[]")
        self.assertEqual(err, "")
        self.assertEqual(data, [])

    def test_array_with_one_item(self) -> None:
        data, err = parse_json_output('[{"Name":"Ethernet 9"}]')
        self.assertEqual(err, "")
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["Name"], "Ethernet 9")

    def test_array_with_several_items(self) -> None:
        raw = json.dumps([{"Name": f"Ethernet {i}"} for i in range(5)], separators=(",", ":"))
        data, err = parse_json_output(raw)
        self.assertEqual(err, "")
        self.assertEqual([row["Name"] for row in data], [f"Ethernet {i}" for i in range(5)])

    def test_large_object_json(self) -> None:
        payload = {
            "Processors": [
                {"Name": f"Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz #{i}", "Note": "N" * 120}
                for i in range(80)
            ]
        }
        raw = json.dumps(payload, separators=(",", ":"))
        self.assertGreater(len(raw), 8000)
        data, err = parse_json_output(raw)
        self.assertEqual(err, "")
        self.assertEqual(len(data["Processors"]), 80)
        self.assertIn("i5-10500T", data["Processors"][0]["Name"])

    def test_large_array_json(self) -> None:
        payload = [
            {
                "Name": f"Ethernet {i}",
                "Description": "Fortinet Virtual Ethernet Adapter " + ("X" * 80),
            }
            for i in range(80)
        ]
        raw = json.dumps(payload, separators=(",", ":"))
        self.assertGreater(len(raw), 8000)
        data, err = parse_json_output(raw)
        self.assertEqual(err, "")
        self.assertEqual(len(data), 80)
        self.assertEqual(data[0]["Name"], "Ethernet 0")

    def test_truncated_hardware_is_not_valid_json(self) -> None:
        raw = '{"Processors":[{"Name":"Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz",'
        data, err = parse_json_output(raw)
        self.assertIsNone(data)
        self.assertIn("Resposta não é JSON válido", err)
        self.assertIn("Processors", err)

    def test_truncated_network_is_not_valid_json(self) -> None:
        raw = '[{"Name":"Ethernet 9","Description":"Fortinet Virtual Ethernet Adapter'
        data, err = parse_json_output(raw)
        self.assertIsNone(data)
        self.assertIn("Resposta não é JSON válido", err)

    def test_truncated_video_is_not_valid_json(self) -> None:
        raw = '{"Name":"Intel(R) UHD Graphics 630","AdapterCompatibility":"Intel Corporation",'
        data, err = parse_json_output(raw)
        self.assertIsNone(data)
        self.assertIn("Resposta não é JSON válido", err)

    def test_truncated_object_does_not_return_nested_fragment(self) -> None:
        raw = (
            '{"Processors":[{"Name":"Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz",'
            '"Manufacturer":"Intel"}],"BaseBoard":{"Manufacturer":"Dell"'
        )
        data, err = parse_json_output(raw)
        self.assertIsNone(data)
        self.assertIn("Resposta não é JSON válido", err)

    def test_psexec_error_is_not_treated_as_json(self) -> None:
        for raw in (
            "The handle is invalid.",
            "Access is denied.",
            "PsExec could not start powershell.exe:\r\nThe system cannot find the file specified.",
            "Connecting to HOST...\r\nError establishing communication with PsExec service.",
        ):
            with self.subTest(raw=raw.splitlines()[0]):
                data, err = parse_json_output(raw)
                self.assertIsNone(data)
                self.assertTrue(err)
                self.assertNotIsInstance(data, (dict, list))


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


def _proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class RunRemotePowershellPriorityTests(unittest.TestCase):
    def _run(self, *, file_json, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        with patch(
            "remoteops.utils.inventory.remote_exec.run_captured",
            return_value=_proc(stdout=stdout, stderr=stderr, returncode=returncode),
        ), patch(
            "remoteops.utils.inventory.remote_exec.read_remote_result_file",
            return_value=file_json,
        ):
            return run_remote_powershell("HOST1", "$json = '{}'", pstools_dir=r"C:\PSTools")

    def test_prefers_complete_remote_file_over_truncated_stdout(self) -> None:
        complete = json.dumps(
            {
                "Processors": [
                    {"Name": f"Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz #{i}", "Note": "N" * 40}
                    for i in range(40)
                ]
            },
            separators=(",", ":"),
        )
        truncated = complete[:80]
        data, err = self._run(file_json=complete, stdout=truncated.encode("utf-8"))
        self.assertEqual(err, "")
        self.assertEqual(len(data["Processors"]), 40)
        self.assertIn("i5-10500T", data["Processors"][0]["Name"])

    def test_file_with_bom(self) -> None:
        payload = '{"Name":"Intel(R) UHD Graphics 630"}'
        data, err = self._run(file_json="\ufeff" + payload, stdout=b"")
        self.assertEqual(err, "")
        self.assertEqual(data["Name"], "Intel(R) UHD Graphics 630")

    def test_file_with_nul(self) -> None:
        payload = '{"Name":"Intel(R) UHD Graphics 630"}'
        nuls = "\x00".join(list(payload))
        data, err = self._run(file_json=nuls, stdout=b"")
        self.assertEqual(err, "")
        self.assertEqual(data["Name"], "Intel(R) UHD Graphics 630")

    def test_prefers_file_over_b64_and_stdout(self) -> None:
        b64 = _B64_MARK + base64.b64encode(b'{"source":"b64"}').decode("ascii")
        stdout = (b64 + '\n{"source":"stdout"}').encode("utf-8")
        data, err = self._run(file_json='{"source":"file"}', stdout=stdout)
        self.assertEqual(err, "")
        self.assertEqual(data["source"], "file")

    def test_falls_back_to_base64_when_file_missing(self) -> None:
        payload = json.dumps(
            [{"Name": f"Ethernet {i}", "Description": "Fortinet " + ("Z" * 30)} for i in range(25)],
            separators=(",", ":"),
        )
        marked = _B64_MARK + base64.b64encode(payload.encode("utf-8")).decode("ascii")
        data, err = self._run(file_json=None, stdout=("noise\n" + marked).encode("utf-8"))
        self.assertEqual(err, "")
        self.assertEqual(len(data), 25)
        self.assertEqual(data[0]["Name"], "Ethernet 0")

    def test_falls_back_to_stdout_when_file_and_b64_missing(self) -> None:
        payload = '[{"Name":"Ethernet 9"}]'
        data, err = self._run(file_json=None, stdout=payload.encode("utf-8"))
        self.assertEqual(err, "")
        self.assertEqual(data[0]["Name"], "Ethernet 9")

    def test_truncated_stdout_without_file_is_error(self) -> None:
        truncated = '{"Processors":[{"Name":"Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz",'
        data, err = self._run(file_json=None, stdout=truncated.encode("utf-8"))
        self.assertIsNone(data)
        self.assertIn("Resposta não é JSON válido", err)

    def test_psexec_error_is_not_json(self) -> None:
        data, err = self._run(
            file_json=None,
            stdout=b"",
            stderr=b"Connecting to HOST1...\r\nAccess is denied.\r\n",
            returncode=1,
        )
        self.assertIsNone(data)
        self.assertIn("Acesso negado", err)
        self.assertNotIn("JSON", err)

    def test_concatenated_json_from_file(self) -> None:
        raw = '{"Name":"Convidado"}{"Name":"tce_admin"}'
        data, err = self._run(file_json=raw, stdout=b"")
        self.assertEqual(err, "")
        self.assertEqual([row["Name"] for row in data], ["Convidado", "tce_admin"])


class ConsoleCodecTests(unittest.TestCase):
    def test_utf16le_json_without_bom(self) -> None:
        payload = json.dumps([{"Name": "tce_admin"}]).encode("utf-16le")
        text = decode_console_bytes(payload)
        data = json.loads(text)
        self.assertEqual(data[0]["Name"], "tce_admin")


if __name__ == "__main__":
    unittest.main()
