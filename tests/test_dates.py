"""Datas da UI: sempre dia/mês/ano (pt-BR)."""

from __future__ import annotations

import unittest
from datetime import datetime

from remoteops.utils.dates import (
    DISPLAY_DATE,
    parse_any_date,
    to_display_date,
    to_display_datetime,
)
from remoteops.utils.inventory.formatters import normalize_wmi_date


class TestParseAndDisplayDates(unittest.TestCase):
    def test_iso_date(self) -> None:
        self.assertEqual(to_display_date("2024-10-04"), "04/10/2024")

    def test_iso_datetime(self) -> None:
        self.assertEqual(
            to_display_datetime("2024-10-04T15:30:00"),
            "04/10/2024 15:30:00",
        )
        self.assertEqual(
            to_display_datetime("2024-10-04 08:05:09"),
            "04/10/2024 08:05:09",
        )

    def test_us_slash_when_day_gt_12(self) -> None:
        self.assertEqual(to_display_date("10/15/2024"), "15/10/2024")

    def test_br_slash_is_day_month(self) -> None:
        self.assertEqual(to_display_date("04/10/2024"), "04/10/2024")
        self.assertEqual(to_display_date("31/08/2026"), "31/08/2026")

    def test_ambiguous_slash_prefers_day_month(self) -> None:
        self.assertEqual(to_display_date("11/07/2012"), "11/07/2012")

    def test_wmi_datetime(self) -> None:
        self.assertEqual(to_display_date("20241004120000.000000+000"), "04/10/2024")
        self.assertEqual(to_display_date("20241004"), "04/10/2024")

    def test_dotnet_json_date(self) -> None:
        self.assertEqual(to_display_date("/Date(1728000000000)/"), "04/10/2024")
        self.assertEqual(normalize_wmi_date("/Date(1728000000000)/"), "04/10/2024")

    def test_us_ampm_to_24h(self) -> None:
        self.assertEqual(
            to_display_datetime("3/24/2020 2:28:50 PM"),
            "24/03/2020 14:28:50",
        )
        self.assertEqual(
            to_display_datetime("3/24/2020 12:05:00 AM"),
            "24/03/2020 00:05:00",
        )

    def test_midnight_keeps_clock_when_input_has_time(self) -> None:
        self.assertEqual(
            to_display_datetime("04/10/2024 00:00:00"),
            "04/10/2024 00:00:00",
        )

    def test_datetime_object(self) -> None:
        dt = datetime(2024, 10, 4, 9, 8, 7)
        self.assertEqual(to_display_date(dt), "04/10/2024")
        self.assertEqual(to_display_datetime(dt), "04/10/2024 09:08:07")

    def test_empty_and_placeholder(self) -> None:
        self.assertEqual(to_display_date("", default="—"), "—")
        self.assertEqual(to_display_date("—", default="—"), "—")
        self.assertIsNone(parse_any_date(None))

    def test_display_pattern(self) -> None:
        self.assertEqual(DISPLAY_DATE, "%d/%m/%Y")


if __name__ == "__main__":
    unittest.main()
