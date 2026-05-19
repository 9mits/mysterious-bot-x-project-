import unittest
from datetime import datetime, timezone

from modules.utils import (
    create_progress_bar,
    extract_snowflake_id,
    format_duration,
    iso_to_dt,
    now_iso,
    parse_duration_str,
    truncate_text,
)


class MbxUtilsTests(unittest.TestCase):
    def test_now_iso_returns_utc_timestamp(self):
        parsed = datetime.fromisoformat(now_iso())
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_iso_to_dt_handles_invalid_input(self):
        self.assertIsNone(iso_to_dt(None))
        self.assertIsNone(iso_to_dt("not-a-date"))

    def test_parse_duration_str_supports_units_and_ban(self):
        self.assertEqual(parse_duration_str("1d 2h 30m"), 1590)
        self.assertEqual(parse_duration_str("ban"), -1)
        self.assertEqual(parse_duration_str("warn"), 0)

    def test_extract_snowflake_id_finds_embedded_ids(self):
        self.assertEqual(extract_snowflake_id("<@123456789012345678>"), 123456789012345678)
        self.assertIsNone(extract_snowflake_id("no id here"))

    def test_text_helpers_keep_output_compact(self):
        self.assertEqual(truncate_text("abcdef", 4), "a...")
        self.assertEqual(format_duration(120), "2 hours")
        self.assertEqual(create_progress_bar(0.5, length=4), "██░░")


if __name__ == "__main__":
    unittest.main()
