import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.comment_age import is_within_comment_age_limit, parse_platform_timestamp


class CommentAgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    @patch("app.comment_age.config.COMMENT_MAX_AGE_DAYS", 90)
    def test_accepts_comments_inside_90_days(self):
        self.assertTrue(
            is_within_comment_age_limit("2026-08-01T12:00:00Z", now=self.now)
        )

    @patch("app.comment_age.config.COMMENT_MAX_AGE_DAYS", 90)
    def test_accepts_comment_exactly_at_90_day_cutoff(self):
        self.assertTrue(
            is_within_comment_age_limit("2026-06-10T12:00:00Z", now=self.now)
        )

    @patch("app.comment_age.config.COMMENT_MAX_AGE_DAYS", 90)
    def test_rejects_comments_older_than_90_days(self):
        self.assertFalse(
            is_within_comment_age_limit("2026-06-10T11:59:59+00:00", now=self.now)
        )

    def test_parses_meta_epoch_seconds(self):
        parsed = parse_platform_timestamp("1788868800")
        self.assertEqual(parsed, self.now)

    def test_unknown_timestamp_is_allowed(self):
        self.assertTrue(is_within_comment_age_limit("not-a-timestamp", now=self.now))


if __name__ == "__main__":
    unittest.main()
