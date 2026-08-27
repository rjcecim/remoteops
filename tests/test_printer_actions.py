"""Testes das ações do menu de contexto (remover / padrão / página de teste)."""

from __future__ import annotations

import unittest

from remoteops.utils.printers import (
    ACTION_REMOVE,
    ACTION_SET_DEFAULT,
    ACTION_TEST_PAGE,
    PRINTUI_DL,
    PRINTUI_DN,
    PRINTUI_GD,
    PRINTUI_K,
    PRINTUI_Y,
    NetworkPrinter,
    build_computer_disconnect_script,
    build_local_delete_script,
    build_user_action_wrapper_script,
    installed_action_requires_user,
    printer_action_name,
    printui_argv,
    printui_command_line,
    remove_printui_flag,
    resolve_installed_action_flag,
    result_file_name,
)


class TestPrintUiActionFlags(unittest.TestCase):
    def test_printui_argv_new_flags(self) -> None:
        for flag in (PRINTUI_DN, PRINTUI_GD, PRINTUI_DL, PRINTUI_Y):
            argv = printui_argv(flag, r"\\PRINT01\Financeiro")
            self.assertEqual(argv[0], "rundll32.exe")
            self.assertIn(f"/{flag}", argv)
            self.assertIn(r"/n\\PRINT01\Financeiro", argv)
            self.assertIn("/q", argv)

        k_argv = printui_argv(PRINTUI_K, "HP Local")
        self.assertIn("/k", k_argv)
        self.assertIn("/nHP Local", k_argv)
        self.assertNotIn("/q", k_argv)

    def test_remove_flag_by_type(self) -> None:
        self.assertEqual(remove_printui_flag("Rede — usuário"), PRINTUI_DN)
        self.assertEqual(remove_printui_flag("Rede — computador"), PRINTUI_GD)
        self.assertEqual(remove_printui_flag("Local"), PRINTUI_DL)
        self.assertEqual(remove_printui_flag("USB"), PRINTUI_DL)
        self.assertEqual(remove_printui_flag("TCP/IP"), PRINTUI_DL)
        self.assertEqual(remove_printui_flag("Virtual"), PRINTUI_DL)

    def test_resolve_action_flag(self) -> None:
        self.assertEqual(
            resolve_installed_action_flag(ACTION_SET_DEFAULT), PRINTUI_Y
        )
        self.assertEqual(resolve_installed_action_flag(ACTION_TEST_PAGE), PRINTUI_K)
        self.assertEqual(
            resolve_installed_action_flag(ACTION_REMOVE, "Rede — usuário"),
            PRINTUI_DN,
        )
        self.assertEqual(
            resolve_installed_action_flag(ACTION_REMOVE, "Rede — computador"),
            PRINTUI_GD,
        )
        self.assertEqual(
            resolve_installed_action_flag(ACTION_REMOVE, "Local"),
            PRINTUI_DL,
        )
        with self.assertRaises(ValueError):
            resolve_installed_action_flag("pause")

    def test_requires_user_session(self) -> None:
        self.assertTrue(installed_action_requires_user(ACTION_SET_DEFAULT))
        self.assertTrue(installed_action_requires_user(ACTION_TEST_PAGE))
        self.assertTrue(
            installed_action_requires_user(ACTION_REMOVE, "Rede — usuário")
        )
        self.assertFalse(
            installed_action_requires_user(ACTION_REMOVE, "Rede — computador")
        )
        self.assertFalse(installed_action_requires_user(ACTION_REMOVE, "Local"))

    def test_printer_action_name(self) -> None:
        self.assertEqual(
            printer_action_name(
                NetworkPrinter(name=r"\\PRINT01\Financeiro", share_name="Financeiro")
            ),
            r"\\PRINT01\Financeiro",
        )
        self.assertEqual(
            printer_action_name(NetworkPrinter(name="", share_name="OnlyShare")),
            "OnlyShare",
        )
        self.assertEqual(printer_action_name(None), "")


class TestActionScripts(unittest.TestCase):
    def test_user_action_wrapper_dn(self) -> None:
        script = build_user_action_wrapper_script(
            printer_name=r"\\PRINT01\Financeiro",
            flag=PRINTUI_DN,
            result_filename=result_file_name("abc"),
        )
        self.assertIn("/dn", script)
        self.assertIn(r"\\PRINT01\Financeiro", script)
        self.assertIn("Test-UserPrinter", script)
        self.assertNotIn("-p ", script.casefold())
        self.assertNotIn("password", script.casefold())

    def test_user_action_wrapper_y_and_k(self) -> None:
        y_script = build_user_action_wrapper_script(
            printer_name="HP Local",
            flag=PRINTUI_Y,
            result_filename=result_file_name("y1"),
        )
        self.assertIn("/y", y_script)
        self.assertIn("/q", y_script)

        k_script = build_user_action_wrapper_script(
            printer_name="HP Local",
            flag=PRINTUI_K,
            result_filename=result_file_name("k1"),
        )
        self.assertIn("/k", k_script)
        self.assertNotIn("/q", k_script)

    def test_computer_and_local_scripts(self) -> None:
        gd = build_computer_disconnect_script(r"\\PRINT01\Financeiro")
        self.assertIn("/gd", gd)
        self.assertIn("rundll32.exe", gd)
        self.assertNotIn("password", gd.casefold())

        dl = build_local_delete_script("HP Local")
        self.assertIn("/dl", dl)
        self.assertIn("HP Local", dl)

    def test_command_line_helpers(self) -> None:
        line = printui_command_line(PRINTUI_GD, r"\\S\Q")
        self.assertIn("PrintUIEntry", line)
        self.assertIn("/gd", line)


if __name__ == "__main__":
    unittest.main()
