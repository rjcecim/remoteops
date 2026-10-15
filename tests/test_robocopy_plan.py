"""Plano Robocopy→PsExec, argv e códigos de saída."""

from __future__ import annotations

import os
import tempfile
import unittest

from remoteops.core.builder import CommandBuilder
from remoteops.core.models import classify_robocopy_exit, is_robocopy_success
from remoteops.core.run_attempt import plan_blocking_errors


def _touch(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class RobocopyCommandBuildTests(unittest.TestCase):
    def _builder(self, path: str, *, folder: str | None = None) -> CommandBuilder:
        builder = CommandBuilder()
        if folder:
            builder.set_file_selection(
                {"mode": "folder", "file": path, "folder": folder}
            )
        else:
            builder.set_file_selection({"mode": "file", "file": path, "folder": None})
        builder.set_psexec_params(
            {"host": "PC01", "psexec_path": "", "remote_cmd": ""}
        )
        builder.set_robocopy_params(
            {
                "dest": "C:\\temp",
                "switches": "/NFL /NDL /NJH /NJS /NC /NS /NP",
                "retries": 2,
                "wait": 3,
            }
        )
        return builder

    def test_file_argv_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _touch(os.path.join(tmp, "setup.msi"))
            spec = self._builder(path).build_robocopy_spec()
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.argv[0].lower(), "robocopy")
        self.assertEqual(spec.argv[1], os.path.dirname(path))
        self.assertEqual(spec.argv[2], r"\\PC01\C$\temp")
        self.assertEqual(spec.argv[3], "setup.msi")
        self.assertIn("/R:2", spec.argv)
        self.assertIn("/W:3", spec.argv)
        self.assertNotIn("/MIR", " ".join(spec.argv).upper())
        self.assertEqual(spec.metadata.get("remote_path"), r"C:\temp\setup.msi")

    def test_folder_uses_e_and_remote_relpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "pkg")
            path = _touch(os.path.join(folder, "bin", "app.exe"))
            spec = self._builder(path, folder=folder).build_robocopy_spec()
        assert spec is not None
        self.assertIn("/E", spec.argv)
        self.assertTrue(any(arg.endswith(r"\temp\pkg") for arg in spec.argv))
        self.assertEqual(
            spec.metadata.get("remote_path"),
            r"C:\temp\pkg\bin\app.exe",
        )

    def test_file_outside_folder_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as a:
            with tempfile.TemporaryDirectory() as b:
                _touch(os.path.join(a, "ok.msi"))
                outside = _touch(os.path.join(b, "other.msi"))
                builder = self._builder(outside, folder=a)
                builder.set_file_selection(
                    {"mode": "folder", "file": outside, "folder": a}
                )
                plan = builder.build_execution_plan()
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan_blocking_errors(plan))
        self.assertEqual((plan[0].metadata or {}).get("kind"), "robocopy")

    def test_missing_source_blocks_psexec(self) -> None:
        missing = os.path.join(tempfile.gettempdir(), "remoteops-missing-xyz.msi")
        if os.path.exists(missing):
            os.remove(missing)
        builder = self._builder(missing)
        plan = builder.build_execution_plan()
        self.assertEqual(len(plan), 1)
        errors = plan_blocking_errors(plan)
        self.assertTrue(errors)
        self.assertFalse(any((s.metadata or {}).get("kind") == "psexec" for s in plan))

    def test_invalid_dest_blocks_psexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _touch(os.path.join(tmp, "setup.msi"))
            builder = self._builder(path)
            builder.set_robocopy_params(
                {"dest": r"D:\temp", "switches": "/NFL", "retries": 2, "wait": 3}
            )
            plan = builder.build_execution_plan()
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan_blocking_errors(plan))

    def test_valid_plan_has_robocopy_then_psexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _touch(os.path.join(tmp, "setup.msi"))
            builder = self._builder(path)
            builder.set_msi_params(
                {
                    "enable": True,
                    "action": "/i",
                    "interface": "/qn",
                    "restart": "/norestart",
                }
            )
            plan = builder.build_execution_plan()
        self.assertEqual(len(plan), 2)
        self.assertEqual((plan[0].metadata or {}).get("kind"), "robocopy")
        self.assertFalse(plan_blocking_errors(plan))

    def test_no_robocopy_params_skips_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _touch(os.path.join(tmp, "app.exe"))
            builder = CommandBuilder()
            builder.set_file_selection({"mode": "file", "file": path, "folder": None})
            builder.set_psexec_params(
                {"host": "PC01", "psexec_path": "", "remote_cmd": ""}
            )
            builder.set_robocopy_params(None)
            plan = builder.build_execution_plan()
        self.assertEqual(len(plan), 1)
        self.assertNotEqual((plan[0].metadata or {}).get("kind"), "robocopy")


class RobocopyExitCodeTests(unittest.TestCase):
    def test_success_range(self) -> None:
        for code in range(0, 8):
            self.assertTrue(is_robocopy_success(code), msg=str(code))
            info = classify_robocopy_exit(code)
            self.assertTrue(info["success"])

    def test_failure_range(self) -> None:
        for code in (8, 9, 16, 32):
            self.assertFalse(is_robocopy_success(code))
            info = classify_robocopy_exit(code)
            self.assertFalse(info["success"])

    def test_bit_flags(self) -> None:
        info = classify_robocopy_exit(1)
        self.assertTrue(info["copied"])
        info = classify_robocopy_exit(3)
        self.assertTrue(info["copied"] and info["extras"])
        info = classify_robocopy_exit(0)
        self.assertTrue(info["no_changes"])


class PlanBlockingMetadataTests(unittest.TestCase):
    def test_robocopy_errors_key(self) -> None:
        from remoteops.core.models import CommandSpec

        spec = CommandSpec(
            executable="robocopy",
            display_command="# Erro: destino inválido",
            metadata={"kind": "robocopy", "robocopy_errors": ["destino inválido"]},
        )
        self.assertEqual(plan_blocking_errors([spec]), ["destino inválido"])


if __name__ == "__main__":
    unittest.main()
