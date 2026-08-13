"""Spinner de espera do winget (Waiting for another install...) vira uma linha só."""

from __future__ import annotations

import unittest

from remoteops.winget.winget_output import is_winget_spinner_status


class TestWingetSpinnerStatus(unittest.TestCase):
    def test_waiting_frames(self) -> None:
        for spin in ("-", "\\", "|", "/"):
            line = f"   {spin} Waiting for another install/uninstall to complete..."
            self.assertTrue(is_winget_spinner_status(line), line)

    def test_portuguese_waiting(self) -> None:
        self.assertTrue(
            is_winget_spinner_status(
                "   | Aguardando outra instalação/desinstalação ser concluída..."
            )
        )

    def test_rejects_unrelated(self) -> None:
        self.assertFalse(is_winget_spinner_status("-"))
        self.assertFalse(is_winget_spinner_status("\\"))
        self.assertFalse(is_winget_spinner_status("|"))
        self.assertFalse(is_winget_spinner_status("/"))
        self.assertFalse(is_winget_spinner_status("Successfully verified installer hash"))
        self.assertFalse(is_winget_spinner_status("-1978335189"))
        self.assertFalse(is_winget_spinner_status("7-Zip  7zip.7zip  24.09  winget"))
        self.assertFalse(is_winget_spinner_status(""))


if __name__ == "__main__":
    unittest.main()
