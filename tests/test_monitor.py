"""Smoke tests for configuration and reusable helper behavior."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from dataclasses import replace

from monitor import load_settings, target_dates


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = Path(self.folder.name) / "config.yaml"
        self.config.write_text("""
monitor:
  poll_interval_seconds: 900
  delay_between_requests_seconds: 10
dates:
  days_ahead: 3
targets:
  - name: demo
    movie: Demo Film
    theater: Demo Theater
    listing_url_template: https://example.com/showtimes?date={date}
""", encoding="utf-8")

    def tearDown(self):
        self.folder.cleanup()

    def test_rolling_window(self):
        settings = load_settings(self.config)
        self.assertEqual(target_dates(settings, date(2026, 10, 15)), [
            "2026-10-15", "2026-10-16", "2026-10-17"
        ])

    def test_fixed_window(self):
        settings = load_settings(self.config)
        settings = replace(settings, start_date=date(2026, 11, 1), end_date=date(2026, 11, 2))
        self.assertEqual(target_dates(settings), ["2026-11-01", "2026-11-02"])

    def test_reject_rapid_polling(self):
        content = self.config.read_text().replace("poll_interval_seconds: 900", "poll_interval_seconds: 3")
        self.config.write_text(content)
        with self.assertRaisesRegex(ValueError, "300 seconds"):
            load_settings(self.config)

    def test_missing_date_placeholder(self):
        content = self.config.read_text().replace("?date={date}", "?date=2026-10-15")
        self.config.write_text(content)
        with self.assertRaisesRegex(ValueError, "include"):
            load_settings(self.config)


if __name__ == "__main__":
    unittest.main()
