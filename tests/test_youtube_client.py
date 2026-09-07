import unittest

from app.youtube_client import is_quota_exceeded


class YouTubeClientTests(unittest.TestCase):
    def test_detects_quota_reason_from_error_details(self):
        error = Exception("request failed")
        error.error_details = [{"reason": "quotaExceeded"}]
        self.assertTrue(is_quota_exceeded(error))

    def test_does_not_treat_other_errors_as_quota_exhaustion(self):
        error = Exception("request failed")
        error.error_details = [{"reason": "forbidden"}]
        self.assertFalse(is_quota_exceeded(error))
