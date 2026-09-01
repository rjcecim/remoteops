"""Testes do PsShutdown — sem rede real e sem o executável Sysinternals."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from remoteops.core.process_runner import CapturedProcess
from remoteops.services.power import (
    PowerService,
    interactive_sessions,
    sessions_warning,
)
from remoteops.utils.host_reachability import is_stale_host_result
from remoteops.utils.psshutdown import (
    ACTION_PROFILES,
    ADVANCED_ACTIONS,
    DEFAULT_COUNTDOWN_SECONDS,
    MAX_MESSAGE_LENGTH,
    PHASE_LABELS,
    PRIMARY_ACTIONS,
    PowerAction,
    PowerPhase,
    PowerRequest,
    ShutdownReasonKind,
    TimingMode,
    apply_action_defaults,
    build_psshutdown_argv,
    classify_psshutdown_output,
    confirmation_summary,
    effective_countdown_seconds,
    history_detail,
    is_multi_host_target,
    parse_uptime_seconds,
    phase_is_success_offline,
    preview_psshutdown_argv,
    psshutdown_available,
    resolve_psshutdown_exe,
    validate_power_host,
    validate_power_request,
)
from remoteops.utils.sessions import RemoteSession


def _request(**kwargs) -> PowerRequest:
    params = dict(
        host="pc01",
        action=PowerAction.RESTART,
        timing=TimingMode.COUNTDOWN,
        countdown_seconds=DEFAULT_COUNTDOWN_SECONDS,
        message="Manutenção planejada. Salve o seu trabalho.",
    )
    params.update(kwargs)
    return PowerRequest(**params)


class TestPsShutdownResolve(unittest.TestCase):
    def test_prefers_64bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsShutdown64.exe"), "wb").close()
            open(os.path.join(folder, "PsShutdown.exe"), "wb").close()
            resolved = resolve_psshutdown_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsShutdown64.exe")
            self.assertTrue(psshutdown_available(folder))

    def test_fallback_to_32bit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsShutdown.exe"), "wb").close()
            resolved = resolve_psshutdown_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsShutdown.exe")
            self.assertTrue(psshutdown_available(folder))

    def test_missing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            resolved = resolve_psshutdown_exe(folder)
            self.assertEqual(os.path.basename(resolved), "PsShutdown64.exe")
            self.assertFalse(psshutdown_available(folder))


class TestPowerHostValidation(unittest.TestCase):
    def test_empty_host_rejected(self) -> None:
        errors = validate_power_host("")
        self.assertTrue(errors)
        self.assertIn("neste computador", errors[0])
        self.assertEqual(build_psshutdown_argv(r"C:\PSTools\PsShutdown64.exe", _request(host="")), [])

    def test_rejects_wildcard_list_and_file(self) -> None:
        for host in ("*", r"\\*", "pc01,pc02", "@hosts.txt", r"\\@file"):
            self.assertTrue(is_multi_host_target(host), host)
            errors = validate_power_host(host)
            self.assertTrue(errors, host)
            self.assertEqual(
                build_psshutdown_argv(
                    r"C:\PSTools\PsShutdown64.exe",
                    _request(host=host),
                ),
                [],
                host,
            )


class TestPowerArgv(unittest.TestCase):
    def test_restart_countdown_message_and_defaults(self) -> None:
        argv = build_psshutdown_argv(
            r"C:\PSTools\PsShutdown64.exe",
            _request(),
            user="DOM\\op",
            password="s3nha",
        )
        self.assertEqual(argv[0], r"C:\PSTools\PsShutdown64.exe")
        self.assertIn("-accepteula", argv)
        self.assertIn(r"\\pc01", argv)
        self.assertEqual(argv[argv.index("-u") + 1], "DOM\\op")
        self.assertEqual(argv[argv.index("-p") + 1], "s3nha")
        self.assertIn("-r", argv)
        self.assertIn("-c", argv)
        self.assertNotIn("-f", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "120")
        self.assertEqual(argv[argv.index("-m") + 1], "Manutenção planejada. Salve o seu trabalho.")
        self.assertEqual(argv[argv.index("-e") + 1], "p:0:0")
        self.assertNotIn("-nobanner", argv)
        preview = preview_psshutdown_argv(argv)
        self.assertNotIn("s3nha", preview)
        self.assertIn("********", preview)

    def test_each_official_flag(self) -> None:
        expected = {
            PowerAction.POWER_OFF: "-k",
            PowerAction.RESTART: "-r",
            PowerAction.HIBERNATE: "-h",
            PowerAction.SUSPEND: "-d",
            PowerAction.LOCK: "-l",
            PowerAction.LOGOFF: "-o",
            PowerAction.ABORT: "-a",
            PowerAction.SHUTDOWN_NO_POWEROFF: "-s",
            PowerAction.MONITOR_OFF: "-x",
        }
        for action, flag in expected.items():
            req = _request(action=action)
            if not ACTION_PROFILES[action].supports_message:
                req = _request(action=action, message="", timing=TimingMode.IMMEDIATE)
            argv = build_psshutdown_argv(r"C:\PSTools\PsShutdown64.exe", req)
            self.assertIn(flag, argv, action)
            self.assertIn(r"\\pc01", argv)

    def test_scheduled_time_and_force(self) -> None:
        argv = build_psshutdown_argv(
            r"C:\PSTools\PsShutdown64.exe",
            _request(
                timing=TimingMode.SCHEDULED,
                scheduled_hour=18,
                scheduled_minute=5,
                force=True,
                notice_seconds=30,
                reason=ShutdownReasonKind.UNPLANNED,
            ),
        )
        self.assertEqual(argv[argv.index("-t") + 1], "18:05")
        self.assertIn("-f", argv)
        self.assertEqual(argv[argv.index("-v") + 1], "30")
        self.assertEqual(argv[argv.index("-e") + 1], "u:0:0")

    def test_reason_detail_codes(self) -> None:
        from remoteops.utils.psshutdown import reason_flag, reasons_for_kind

        planned = reasons_for_kind(ShutdownReasonKind.PLANNED)
        unplanned = reasons_for_kind(ShutdownReasonKind.UNPLANNED)
        self.assertTrue(any(o.major == 2 and o.minor == 3 for o in planned))
        self.assertFalse(any(o.major == 2 and o.minor == 3 for o in unplanned))
        self.assertTrue(any(o.major == 0 and o.minor == 5 for o in unplanned))
        self.assertFalse(any(o.major == 0 and o.minor == 5 for o in planned))
        argv = build_psshutdown_argv(
            r"C:\PSTools\PsShutdown64.exe",
            _request(
                reason=ShutdownReasonKind.PLANNED,
                reason_major=2,
                reason_minor=16,
            ),
        )
        self.assertEqual(argv[argv.index("-e") + 1], "p:2:16")
        self.assertEqual(
            reason_flag(ShutdownReasonKind.UNPLANNED, major=4, minor=5),
            "u:4:5",
        )

    def test_suggested_reason_messages(self) -> None:
        from remoteops.utils.psshutdown import (
            SHUTDOWN_REASON_OPTIONS,
            suggested_reason_message,
        )

        self.assertTrue(SHUTDOWN_REASON_OPTIONS)
        for option in SHUTDOWN_REASON_OPTIONS:
            text = suggested_reason_message(option)
            self.assertTrue(text, option.key)
            self.assertLessEqual(len(text), MAX_MESSAGE_LENGTH)
        planned_update = next(
            o for o in SHUTDOWN_REASON_OPTIONS if o.major == 2 and o.minor == 3
        )
        self.assertIn("Atualização", planned_update.message)

    def test_lock_omits_countdown_and_message(self) -> None:
        argv = build_psshutdown_argv(
            r"C:\PSTools\PsShutdown64.exe",
            _request(action=PowerAction.LOCK, message="x", countdown_seconds=120),
        )
        self.assertIn("-l", argv)
        self.assertNotIn("-t", argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("-c", argv)
        self.assertNotIn("-f", argv)

    def test_countdown_requires_message(self) -> None:
        errors = validate_power_request(_request(message=""))
        self.assertTrue(any("mensagem" in err.lower() for err in errors))
        self.assertEqual(
            build_psshutdown_argv(r"C:\PSTools\PsShutdown64.exe", _request(message="")),
            [],
        )

    def test_immediate_has_no_user_abort(self) -> None:
        req = apply_action_defaults(
            _request(
                timing=TimingMode.IMMEDIATE,
                allow_user_abort=True,
                message="Não deve ir",
                notice_seconds=30,
            )
        )
        self.assertFalse(req.allow_user_abort)
        self.assertEqual(req.message, "")
        self.assertIsNone(req.notice_seconds)
        self.assertEqual(effective_countdown_seconds(req), 0)
        argv = build_psshutdown_argv(r"C:\PSTools\PsShutdown64.exe", req)
        self.assertEqual(argv[argv.index("-t") + 1], "0")
        self.assertNotIn("-c", argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("-v", argv)

    def test_advanced_actions(self) -> None:
        self.assertIn(PowerAction.SHUTDOWN_NO_POWEROFF, ADVANCED_ACTIONS)
        self.assertIn(PowerAction.MONITOR_OFF, ADVANCED_ACTIONS)
        self.assertTrue(ACTION_PROFILES[PowerAction.SHUTDOWN_NO_POWEROFF].advanced)
        self.assertTrue(ACTION_PROFILES[PowerAction.MONITOR_OFF].advanced)
        self.assertNotIn(PowerAction.ABORT, PRIMARY_ACTIONS)
        self.assertNotIn(PowerAction.ABORT, ADVANCED_ACTIONS)


class TestPowerClassification(unittest.TestCase):
    def test_accepted_restart_is_pending(self) -> None:
        result = classify_psshutdown_output(
            "Shutdown initiated on pc01",
            returncode=0,
            action=PowerAction.RESTART,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.phase, PowerPhase.ACTION_PENDING)
        self.assertTrue(result.expected_offline)

    def test_abort_success_and_nothing_to_cancel(self) -> None:
        ok = classify_psshutdown_output(
            "Shutdown aborted",
            returncode=0,
            action=PowerAction.ABORT,
        )
        self.assertEqual(ok.phase, PowerPhase.ABORTED)
        self.assertTrue(ok.ok)
        fail = classify_psshutdown_output(
            "No shutdown is in progress",
            returncode=1,
            action=PowerAction.ABORT,
        )
        self.assertEqual(fail.phase, PowerPhase.FAILED)
        self.assertFalse(fail.ok)

    def test_local_cancel_is_not_remote_abort(self) -> None:
        result = classify_psshutdown_output(
            "",
            returncode=1,
            cancelled=True,
        )
        self.assertEqual(result.phase, PowerPhase.CANCELLED_LOCAL)
        self.assertIn("não cancela", result.detail.lower())

    def test_offline_after_shutdown_is_not_error(self) -> None:
        self.assertTrue(
            phase_is_success_offline(PowerPhase.HOST_UNAVAILABLE, PowerAction.POWER_OFF)
        )
        self.assertTrue(
            phase_is_success_offline(PowerPhase.ACTION_PENDING, PowerAction.RESTART)
        )
        self.assertFalse(
            phase_is_success_offline(PowerPhase.HOST_UNAVAILABLE, PowerAction.LOCK)
        )

    def test_lock_and_logoff_phases(self) -> None:
        lock = classify_psshutdown_output("has been locked", returncode=0, action=PowerAction.LOCK)
        self.assertEqual(lock.phase, PowerPhase.LOCKED)
        logoff = classify_psshutdown_output("logged off", returncode=0, action=PowerAction.LOGOFF)
        self.assertEqual(logoff.phase, PowerPhase.LOGGED_OFF)

    def test_tool_missing_and_access_denied(self) -> None:
        missing = classify_psshutdown_output("", spawn_error="Executável não encontrado: x")
        self.assertEqual(missing.phase, PowerPhase.TOOL_MISSING)
        denied = classify_psshutdown_output("Access is denied.", returncode=1)
        self.assertEqual(denied.phase, PowerPhase.FAILED)

    def test_phase_labels_cover_operator_language(self) -> None:
        self.assertEqual(PHASE_LABELS[PowerPhase.COMMAND_SENT], "Comando enviado")
        self.assertEqual(PHASE_LABELS[PowerPhase.COMMAND_ACCEPTED], "Comando aceito")
        self.assertEqual(PHASE_LABELS[PowerPhase.ACTION_PENDING], "Ação pendente")
        self.assertEqual(PHASE_LABELS[PowerPhase.HOST_UNAVAILABLE], "Host ficou indisponível")
        self.assertEqual(PHASE_LABELS[PowerPhase.HOST_RETURNED], "Host voltou após reinício")
        self.assertEqual(PHASE_LABELS[PowerPhase.UNCONFIRMED], "Resultado não confirmado")


class TestPowerHelpers(unittest.TestCase):
    def test_uptime_parse(self) -> None:
        self.assertEqual(
            parse_uptime_seconds("0 days 0 hours 3 minutes 12 seconds"),
            192,
        )
        self.assertIsNone(parse_uptime_seconds("Kernel version"))

    def test_confirmation_includes_force_and_host(self) -> None:
        text = confirmation_summary(
            _request(force=True),
            sessions_note="Há 1 sessão interativa: ana — Console.",
        )
        self.assertIn("pc01", text)
        self.assertIn("Reiniciar", text)
        self.assertIn("120 s", text)
        self.assertIn("sim", text)
        self.assertIn("ana", text)
        self.assertNotIn("s3nha", text)

    def test_history_has_no_password(self) -> None:
        classified = classify_psshutdown_output("ok", returncode=0)
        detail = history_detail(_request(), classified, operator="op01")
        self.assertIn("host=pc01", detail)
        self.assertIn("operator=op01", detail)
        self.assertNotIn("s3nha", detail)
        self.assertNotIn("Manutenção", detail)

    def test_stale_host(self) -> None:
        self.assertTrue(is_stale_host_result("pc01", "pc01", "pc02"))
        self.assertFalse(is_stale_host_result("pc01", "pc01", "pc01"))

    def test_interactive_sessions(self) -> None:
        sessions = [
            RemoteSession(0, "Services", "", "Desconectada"),
            RemoteSession(1, "Console", "ana", "Ativa"),
            RemoteSession(2, "RDP-Tcp#1", "bob", "Desconectada"),
        ]
        active = interactive_sessions(sessions)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].username, "ana")
        self.assertIn("ana", sessions_warning(sessions))


class TestPowerService(unittest.TestCase):
    def test_missing_tool_without_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = PowerService().run(_request(), pstools_dir=folder)
            self.assertEqual(result.phase, PowerPhase.TOOL_MISSING)
            self.assertFalse(result.ok)

    def test_empty_host_never_calls_runner(self) -> None:
        called = []

        def runner(argv, **_kwargs):
            called.append(argv)
            return CapturedProcess(returncode=0, stdout=b"initiated")

        result = PowerService().run(_request(host=""), runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(called, [])

    def test_run_restart_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsShutdown64.exe"), "wb").close()
            captured_argv = []

            def runner(argv, **_kwargs):
                captured_argv.append(list(argv))
                return CapturedProcess(returncode=0, stdout=b"Shutdown initiated")

            result = PowerService().run(
                _request(),
                user="op",
                password="s3nha",
                pstools_dir=folder,
                runner=runner,
                follow_up=False,
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.phase, PowerPhase.ACTION_PENDING)
            self.assertEqual(captured_argv[0][captured_argv[0].index("-p") + 1], "s3nha")
            self.assertNotIn("s3nha", result.argv_preview)
            self.assertNotIn("s3nha", result.output)

    def test_followup_offline_is_success_for_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsShutdown64.exe"), "wb").close()

            def runner(argv, **_kwargs):
                return CapturedProcess(returncode=0, stdout=b"initiated")

            def tcp_probe(_host, **_kwargs):
                return type("R", (), {"ok": False})()

            with patch("remoteops.services.power._wait", return_value=True):
                result = PowerService().run(
                    _request(action=PowerAction.POWER_OFF),
                    pstools_dir=folder,
                    runner=runner,
                    tcp_probe=tcp_probe,
                    follow_up=True,
                )
            self.assertTrue(result.ok)
            self.assertTrue(result.tcp_went_offline)
            self.assertEqual(result.phase, PowerPhase.HOST_UNAVAILABLE)

    def test_stale_host_invalidates_result(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsShutdown64.exe"), "wb").close()

            def runner(argv, **_kwargs):
                return CapturedProcess(returncode=0, stdout=b"initiated")

            result = PowerService().run(
                _request(),
                pstools_dir=folder,
                runner=runner,
                current_host="outro-pc",
                follow_up=False,
            )
            self.assertTrue(result.stale)
            self.assertEqual(result.phase, PowerPhase.STALE_HOST)


class TestProbeOptional(unittest.TestCase):
    def test_missing_psshutdown_does_not_break_health(self) -> None:
        from remoteops.utils.pstools import probe_pstools

        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsExec64.exe"), "wb").close()
            open(os.path.join(folder, "PsInfo64.exe"), "wb").close()
            info = probe_pstools(folder)
            labels = [tool["label"] for tool in info["tools"]]
            self.assertIn("PsShutdown", labels)
            self.assertTrue(info["healthy"])
            shutdown = next(
                tool for tool in info["tools"] if tool["label"] == "PsShutdown"
            )
            self.assertFalse(shutdown["found"])

    def test_resolve_prefers_64_then_falls_back_to_32(self) -> None:
        from remoteops.utils.pstools import resolve_pstools_tool

        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsExec.exe"), "wb").close()
            open(os.path.join(folder, "PsExec64.exe"), "wb").close()
            # 32 listado primeiro: mesmo assim executa o 64.
            both = resolve_pstools_tool(folder, ("PsExec.exe", "PsExec64.exe"))
            self.assertEqual(os.path.basename(both), "PsExec64.exe")

        with tempfile.TemporaryDirectory() as folder:
            open(os.path.join(folder, "PsExec.exe"), "wb").close()
            only_32 = resolve_pstools_tool(folder, ("PsExec64.exe", "PsExec.exe"))
            self.assertEqual(os.path.basename(only_32), "PsExec.exe")


if __name__ == "__main__":
    unittest.main()
