"""ConPTY: sem reexecução após CreateProcess, cancelamento remoto e artefatos."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from remoteops.winget.constants import CREATEPROCESS_CMDLINE_MAX, PSEXEC_ACTION_TIMEOUT_S
from remoteops.winget.powershell_script import build_bootstrap_script, build_remote_script
from remoteops.winget.psexec_args import build_psexec_args
from remoteops.winget.remote import _annotate_stop, _payload_or_raise, _request_remote_stop
from remoteops.winget.result_file import (
    build_remote_paths,
    delete_remote_artifact,
    signal_remote_cancel,
)
from remoteops.winget.win_error import ResolvedExitCode


def _script(**kwargs) -> str:
    params = dict(
        action="install",
        ids=["Foo.Bar"],
        query="",
        result_path=r"C:\Windows\Temp\WINGETRMABC.json",
        log_path=r"C:\Windows\Temp\WINGETRMABC.log",
        cancel_path=r"C:\Windows\Temp\WINGETRMABC.cancel",
        timeout_s=PSEXEC_ACTION_TIMEOUT_S,
    )
    params.update(kwargs)
    return build_remote_script(**params)


class TestGeneratedScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = _script()

    def test_uses_getlines_snapshot(self) -> None:
        self.assertIn("GetLines()", self.script)
        self.assertNotIn("$runner.Lines", self.script)

    def test_no_fallback_after_createprocess(self) -> None:
        self.assertIn("ProcessStarted", self.script)
        self.assertIn("sem reexecução", self.script)
        started_idx = self.script.find("falha após iniciar o WinGet (sem reexecução)")
        fallback_idx = self.script.find("usando modo padrão")
        self.assertGreater(started_idx, 0)
        self.assertGreater(fallback_idx, started_idx)

    def test_no_infinite_wait_in_runner(self) -> None:
        self.assertNotIn("WaitForSingleObject(pi.hProcess, 0xFFFFFFFF)", self.script)

    def test_try_finally_and_inherit_restore(self) -> None:
        self.assertIn("inheritCleared", self.script)
        self.assertIn("RestoreStdHandleInherit", self.script)
        self.assertIn("finally {", self.script)

    def test_cancel_and_timeout_placeholders(self) -> None:
        self.assertIn(r"C:\Windows\Temp\WINGETRMABC.cancel", self.script)
        self.assertIn(f"$script:WingetTimeoutS = {PSEXEC_ACTION_TIMEOUT_S}", self.script)
        self.assertIn("CancelRequested", self.script)
        self.assertIn("Test-RemoteCancel", self.script)

    def test_pipe_fallback_does_not_use_clr_thread(self) -> None:
        self.assertNotIn("System.Threading.Thread", self.script)
        self.assertIn("BeginRead", self.script)

    def test_comments_stripped_from_payload(self) -> None:
        self.assertNotIn("# ConPTY:", self.script)
        self.assertNotIn("NÃO usar $LASTEXITCODE", self.script)
        self.assertNotIn("Fecha o PTY para gerar EOF", self.script)

    def test_list_omits_conpty_runner(self) -> None:
        script = _script(action="list", ids=[])
        self.assertNotIn("CreatePseudoConsole", script)
        self.assertNotIn("Add-Type -TypeDefinition", script)
        self.assertIn("$script:ConPtyOk = $false", script)


class TestCommandLineBudget(unittest.TestCase):
    def _cmdline_len(self, action: str) -> int:
        script = _script(action=action)
        args = build_psexec_args(
            psexec_path=r"C:\PSTools\PsExec64.exe",
            host="ETSECEX-3CCG34",
            username="u",
            password="p",
            svc_name="WINGETRM261EBA",
            ps_command=build_bootstrap_script(script),
        )
        return len(subprocess.list2cmdline(args))

    def test_list_and_install_fit_createprocess_limit(self) -> None:
        self.assertLess(self._cmdline_len("list"), CREATEPROCESS_CMDLINE_MAX)
        self.assertLess(self._cmdline_len("install"), CREATEPROCESS_CMDLINE_MAX)


class TestRemoteArtifacts(unittest.TestCase):
    def test_build_remote_paths_includes_cancel(self) -> None:
        art = build_remote_paths("HOST01", "WINGETRMABC123")
        self.assertEqual(art.json_path, r"C:\Windows\Temp\WINGETRMABC123.json")
        self.assertEqual(art.log_path, r"C:\Windows\Temp\WINGETRMABC123.log")
        self.assertEqual(art.cancel_path, r"C:\Windows\Temp\WINGETRMABC123.cancel")
        self.assertEqual(art.cancel_admin, r"\\HOST01\ADMIN$\Temp\WINGETRMABC123.cancel")
        self.assertEqual(art.cancel_c, r"\\HOST01\C$\Windows\Temp\WINGETRMABC123.cancel")

    def test_signal_remote_cancel_writes_and_can_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p1 = str(Path(tmp) / "a.cancel")
            p2 = str(Path(tmp) / "b.cancel")
            written = signal_remote_cancel(p1, p2, "")
            self.assertEqual(written, [p1, p2])
            self.assertEqual(Path(p1).read_text(encoding="utf-8"), "1")
            self.assertEqual(Path(p2).read_text(encoding="utf-8"), "1")
            delete_remote_artifact(p1, p2)
            self.assertFalse(Path(p1).exists())
            self.assertFalse(Path(p2).exists())


class _FakeProc:
    def __init__(self, *, exit_immediately: bool = False) -> None:
        self.killed = False
        self._done = exit_immediately

    def poll(self):
        return 0 if self._done or self.killed else None

    def kill(self) -> None:
        self.killed = True


class TestRequestRemoteStop(unittest.TestCase):
    def test_writes_cancel_and_kills_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cancel = str(Path(tmp) / "x.cancel")
            proc = _FakeProc()
            logs: list[str] = []
            _request_remote_stop(
                proc,  # type: ignore[arg-type]
                log_cb=logs.append,
                message="sinal",
                cancel_unc=(cancel,),
                grace_s=0.3,
            )
            self.assertTrue(Path(cancel).exists())
            self.assertTrue(proc.killed)
            self.assertTrue(any("PsExec local" in x for x in logs))

    def test_does_not_kill_if_host_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cancel = str(Path(tmp) / "x.cancel")
            proc = _FakeProc(exit_immediately=True)
            _request_remote_stop(
                proc,  # type: ignore[arg-type]
                log_cb=None,
                message="sinal",
                cancel_unc=(cancel,),
                grace_s=2.0,
            )
            self.assertFalse(proc.killed)
            self.assertTrue(Path(cancel).exists())


class TestPayloadAfterStop(unittest.TestCase):
    def test_timeout_prefers_json_results(self) -> None:
        payload = {
            "Ok": True,
            "Action": "install",
            "Results": [{"Id": "Foo.Bar", "ExitCode": 0, "Output": "ok"}],
        }
        out = _payload_or_raise(
            action="install",
            ids=["Foo.Bar"],
            stdout="",
            stderr="",
            file_json=json.dumps(payload),
            exit_code=1,
            timed_out=True,
            resolved_exit=ResolvedExitCode(1, "err", "unknown"),
            cancelled=False,
        )
        self.assertFalse(out["Ok"])
        self.assertTrue(out["TimedOut"])
        self.assertEqual(out["Results"][0]["Id"], "Foo.Bar")

    def test_cancel_without_json_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _payload_or_raise(
                action="install",
                ids=["Foo.Bar"],
                stdout="",
                stderr="",
                file_json=None,
                exit_code=1,
                timed_out=False,
                resolved_exit=ResolvedExitCode(1, "err", "unknown"),
                cancelled=True,
            )
        self.assertEqual(str(ctx.exception), "Operação cancelada pelo usuário.")

    def test_annotate_stop_sets_flags(self) -> None:
        payload = _annotate_stop({"Ok": True, "Meta": {}}, cancelled=True, timed_out=False)
        self.assertFalse(payload["Ok"])
        self.assertTrue(payload["Cancelled"])
        self.assertTrue(payload["Meta"]["Cancelled"])
        self.assertEqual(payload["Error"], "Operação cancelada pelo usuário.")


if __name__ == "__main__":
    unittest.main()
