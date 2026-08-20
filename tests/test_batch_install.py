"""Testes da decisão de instalação em lote e do fallback Win32_Product."""

from __future__ import annotations

import unittest

from remoteops.services.batch_install import (
    ACTION_INSTALL,
    ACTION_SKIP,
    ACTION_UPDATE,
    REASON_ALREADY_CURRENT,
    REASON_DETECT_FAILED,
    REASON_INSTALL_FAILED,
    REASON_INSTALLER_VERSION_UNKNOWN,
    REASON_NEWER_INSTALLED,
    REASON_NOT_INSTALLED,
    REASON_OFFLINE,
    REASON_OLD_VERSION,
    RESULT_ERROR,
    RESULT_INSTALLED,
    RESULT_SKIPPED,
    RESULT_UPDATED,
    BatchHostRow,
    RemoteInstallOutcome,
    apply_install_outcome,
    decide_host_action,
    enrich_inventory,
    resolve_target_version,
)
from remoteops.utils.product_identity import ProductIdentity
from remoteops.utils.psinfo import HostInventoryStatus, InstalledApp
from remoteops.utils.win32_product import apps_from_win32_json


def _app(name: str, version: str = "", arch: str = "64") -> InstalledApp:
    return InstalledApp(
        display_name=name,
        version=version,
        publisher="",
        display_line=f"{name} {version}".strip(),
        product_code="",
        uninstall_string="",
        quiet_uninstall_string="",
        is_msi=False,
        arch=arch,
    )


def _identity(
    *,
    label: str = "Produto Exemplo",
    needles: tuple[str, ...] = ("Produto Exemplo",),
    filename_needles: tuple[str, ...] = (),
    installer_version: str = "2.3.0",
) -> ProductIdentity:
    return ProductIdentity(
        label=label,
        needles=needles,
        filename_needles=filename_needles,
        installer_version=installer_version,
    )


def _inventory(ok: bool, apps: list[InstalledApp] | None = None, host: str = "PC01"):
    return HostInventoryStatus(
        host=host,
        ok=ok,
        apps=list(apps or []),
        error_kind="" if ok else "remote_registry",
        message="" if ok else "falha",
        stage="enumerate",
    )


def _decide(
    *,
    desired: str = "2.3.0",
    online: bool = True,
    inventory: HostInventoryStatus | None = None,
    identity: ProductIdentity | None = None,
    host: str = "PC01",
) -> BatchHostRow:
    return decide_host_action(
        host=host,
        desired_version=desired,
        online=online,
        inventory=inventory,
        identity=identity or _identity(),
    )


class ResolveTargetVersionTests(unittest.TestCase):
    def test_typed_version_wins(self) -> None:
        identity = _identity(installer_version="9.9.9")
        self.assertEqual(resolve_target_version("2.3.0", identity), "2.3.0")

    def test_empty_uses_product_version(self) -> None:
        identity = _identity(installer_version="2.3.0")
        self.assertEqual(resolve_target_version("", identity), "2.3.0")

    def test_empty_without_installer_version(self) -> None:
        identity = _identity(installer_version="")
        self.assertEqual(resolve_target_version("", identity), "")


class DecideHostActionTests(unittest.TestCase):
    def test_installed_older_updates(self) -> None:
        row = _decide(inventory=_inventory(True, [_app("Produto Exemplo", "2.1.0")]))
        self.assertEqual(row.action, ACTION_UPDATE)
        self.assertTrue(row.needs_install)
        self.assertEqual(row.reason, REASON_OLD_VERSION)
        self.assertEqual(row.desired, "2.3.0")
        self.assertEqual(row.version, "2.1.0")

    def test_installed_equal_skips(self) -> None:
        row = _decide(inventory=_inventory(True, [_app("Produto Exemplo", "2.3.0")]))
        self.assertEqual(row.action, ACTION_SKIP)
        self.assertFalse(row.needs_install)
        self.assertEqual(row.result, RESULT_SKIPPED)
        self.assertEqual(row.reason, REASON_ALREADY_CURRENT)

    def test_installed_newer_no_downgrade(self) -> None:
        row = _decide(inventory=_inventory(True, [_app("Produto Exemplo", "2.5.0")]))
        self.assertEqual(row.action, ACTION_SKIP)
        self.assertFalse(row.needs_install)
        self.assertEqual(row.result, RESULT_SKIPPED)
        self.assertEqual(row.reason, REASON_NEWER_INSTALLED)

    def test_app_missing_installs(self) -> None:
        row = _decide(inventory=_inventory(True, [_app("Outro App", "1.0")]))
        self.assertEqual(row.action, ACTION_INSTALL)
        self.assertTrue(row.needs_install)
        self.assertEqual(row.reason, REASON_NOT_INSTALLED)
        self.assertEqual(row.app_found, "—")

    def test_empty_desired_uses_installer_version(self) -> None:
        identity = _identity(installer_version="2.3.0")
        older = _decide(
            desired="",
            inventory=_inventory(True, [_app("Produto Exemplo", "2.1.0")]),
            identity=identity,
        )
        self.assertEqual(older.action, ACTION_UPDATE)
        self.assertEqual(older.desired, "2.3.0")
        same = _decide(
            desired="",
            inventory=_inventory(True, [_app("Produto Exemplo", "2.3.0")]),
            identity=identity,
        )
        self.assertEqual(same.action, ACTION_SKIP)
        self.assertEqual(same.reason, REASON_ALREADY_CURRENT)

    def test_exe_without_version_and_app_installed_is_error(self) -> None:
        row = _decide(
            desired="",
            inventory=_inventory(True, [_app("Produto Exemplo", "2.1.0")]),
            identity=_identity(installer_version=""),
        )
        self.assertEqual(row.action, ACTION_SKIP)
        self.assertFalse(row.needs_install)
        self.assertEqual(row.result, RESULT_ERROR)
        self.assertEqual(row.reason, REASON_INSTALLER_VERSION_UNKNOWN)
        self.assertEqual(row.desired, "—")
        self.assertEqual(row.app_found.split(" (")[0], "Produto Exemplo")

    def test_exe_without_version_and_app_missing_installs(self) -> None:
        row = _decide(
            desired="",
            inventory=_inventory(True, []),
            identity=_identity(installer_version=""),
        )
        self.assertEqual(row.action, ACTION_INSTALL)
        self.assertTrue(row.needs_install)
        self.assertEqual(row.reason, REASON_NOT_INSTALLED)

    def test_query_failure_is_not_missing_app(self) -> None:
        row = _decide(inventory=_inventory(False, []))
        self.assertEqual(row.action, ACTION_SKIP)
        self.assertFalse(row.needs_install)
        self.assertEqual(row.result, RESULT_ERROR)
        self.assertEqual(row.reason, REASON_DETECT_FAILED)

    def test_offline(self) -> None:
        row = _decide(online=False, inventory=None)
        self.assertEqual(row.action, ACTION_SKIP)
        self.assertEqual(row.result, RESULT_SKIPPED)
        self.assertEqual(row.reason, REASON_OFFLINE)
        self.assertFalse(row.online)

    def test_remote_registry_fills_name_and_version(self) -> None:
        row = _decide(inventory=_inventory(True, [_app("Produto Exemplo", "2.1.0")]))
        self.assertIn("Produto Exemplo", row.app_found)
        self.assertEqual(row.version, "2.1.0")
        self.assertEqual(row.desired, "2.3.0")

    def test_numeric_4_10_vs_4_9(self) -> None:
        identity = _identity(installer_version="4.10.0")
        row = _decide(
            desired="4.10.0",
            inventory=_inventory(True, [_app("Produto Exemplo", "4.9.0")]),
            identity=identity,
        )
        self.assertEqual(row.action, ACTION_UPDATE)
        self.assertEqual(row.reason, REASON_OLD_VERSION)


class EnrichInventoryTests(unittest.TestCase):
    def test_rr_match_does_not_call_win32(self) -> None:
        rr = _inventory(True, [_app("Produto Exemplo", "2.1.0")])

        def _boom(_host: str) -> HostInventoryStatus:
            raise AssertionError("Win32_Product não deve ser consultado")

        out = enrich_inventory(rr, _identity(), win32_query=_boom)
        self.assertIs(out, rr)

    def test_complete_rr_without_match_does_not_call_win32(self) -> None:
        rr = _inventory(True, [_app("Outro App", "1.0")])

        def _boom(_host: str) -> HostInventoryStatus:
            raise AssertionError("inventário RR completo não deve ir ao WMI")

        out = enrich_inventory(rr, _identity(), win32_query=_boom)
        self.assertIs(out, rr)

    def test_win32_not_used_when_rr_fails(self) -> None:
        """Host inacessível não dispara PowerShell/WMI (EDR)."""
        rr = _inventory(False, [])

        def _boom(_host: str) -> HostInventoryStatus:
            raise AssertionError("Win32_Product não deve rodar após falha do RR")

        out = enrich_inventory(rr, _identity(), win32_query=_boom)
        self.assertIs(out, rr)
        self.assertFalse(out.ok)

    def test_win32_fallback_when_rr_empty(self) -> None:
        rr = _inventory(True, [])
        wmi = _inventory(True, [_app("Produto Exemplo", "2.1.0")])
        out = enrich_inventory(rr, _identity(), win32_query=lambda _h: wmi)
        self.assertIs(out, wmi)

    def test_rr_failure_stays_failed_without_win32(self) -> None:
        rr = _inventory(False, [])
        out = enrich_inventory(rr, _identity(), win32_query=lambda _h: _inventory(True, []))
        self.assertFalse(out.ok)
        self.assertIs(out, rr)


class Win32JsonTests(unittest.TestCase):
    def test_single_object(self) -> None:
        payload = (
            '{"Name":"Produto Exemplo","Version":"2.1.0",'
            '"Vendor":"ACME","IdentifyingNumber":"{ABC}"}'
        )
        apps = apps_from_win32_json(payload)
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0].display_name, "Produto Exemplo")
        self.assertEqual(apps[0].version, "2.1.0")

    def test_list_and_empty_name_skipped(self) -> None:
        payload = (
            '[{"Name":"Produto Exemplo","Version":"2.1.0"},{"Name":"","Version":"1"}]'
        )
        apps = apps_from_win32_json(payload)
        self.assertEqual(len(apps), 1)


class InstallOutcomeTests(unittest.TestCase):
    def test_install_failure(self) -> None:
        row = BatchHostRow(
            host="PC01",
            action=ACTION_INSTALL,
            needs_install=True,
            reason=REASON_NOT_INSTALLED,
        )
        apply_install_outcome(
            row,
            RemoteInstallOutcome(ok=False, installer_ok=False, message="código 1603"),
        )
        self.assertEqual(row.result, RESULT_ERROR)
        self.assertEqual(row.reason, REASON_INSTALL_FAILED)
        self.assertFalse(row.needs_install)

    def test_success_codes(self) -> None:
        for code in (0, 1641, 3010):
            row = BatchHostRow(
                host="PC01",
                action=ACTION_INSTALL,
                needs_install=True,
                reason=REASON_NOT_INSTALLED,
            )
            apply_install_outcome(
                row,
                RemoteInstallOutcome(
                    ok=True, installer_ok=True, psexec_ok=True, return_code=code
                ),
            )
            self.assertEqual(row.result, RESULT_INSTALLED, msg=code)
            self.assertEqual(row.reason, REASON_NOT_INSTALLED)

    def test_update_success(self) -> None:
        row = BatchHostRow(
            host="PC01",
            action=ACTION_UPDATE,
            is_update=True,
            needs_install=True,
            app_found="Produto Exemplo",
            reason=REASON_OLD_VERSION,
        )
        apply_install_outcome(
            row,
            RemoteInstallOutcome(ok=True, installer_ok=True, psexec_ok=True, return_code=0),
        )
        self.assertEqual(row.result, RESULT_UPDATED)
        self.assertEqual(row.reason, REASON_OLD_VERSION)

    def test_error_on_one_host_does_not_affect_the_other(self) -> None:
        failed = BatchHostRow(host="PC01", action=ACTION_INSTALL, needs_install=True)
        ok_row = BatchHostRow(
            host="PC02",
            action=ACTION_INSTALL,
            needs_install=True,
            reason=REASON_NOT_INSTALLED,
        )
        apply_install_outcome(failed, RemoteInstallOutcome(ok=False, installer_ok=False))
        apply_install_outcome(
            ok_row,
            RemoteInstallOutcome(ok=True, installer_ok=True, psexec_ok=True, return_code=0),
        )
        self.assertEqual(failed.result, RESULT_ERROR)
        self.assertEqual(ok_row.result, RESULT_INSTALLED)
        self.assertEqual(ok_row.reason, REASON_NOT_INSTALLED)


if __name__ == "__main__":
    unittest.main()
