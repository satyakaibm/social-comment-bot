import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
from googleapiclient.errors import HttpError

from app import config, db, post, youtube_client


def _http_error(status: int, reason: str) -> HttpError:
    body = {"error": {"code": status, "message": reason, "errors": [{"reason": reason, "domain": "youtube.comment"}]}}
    return HttpError(httplib2.Response({"status": status, "reason": "x"}), json.dumps(body).encode())


class YouTubePublishFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        with db.connect() as conn:
            db.insert_comment(conn, comment_id="yt1", platform="youtube", video_id="vid",
                              video_title="Title", author="viewer", text="Jai Maa",
                              published_at="", draft_reply="🙏")
            db.update_status(conn, "yt1", "approved")

    def _post_with(self, error: HttpError) -> MagicMock:
        execute = MagicMock(side_effect=error)
        with patch.object(post, "get_client", return_value=MagicMock()), \
             patch.object(post, "get_my_channel_id", return_value="oauth-channel"), \
             patch.object(post, "get_video_channel_ids", return_value={"vid": "oauth-channel"}), \
             patch.object(post, "find_own_reply", return_value=None), \
             patch.object(post, "execute", execute):
            post.post_approved(platform="youtube", include_failed=True)
        return execute

    def _row(self):
        with db.connect() as conn:
            return dict(conn.execute("SELECT status, retry_count, error FROM comments WHERE comment_id='yt1'").fetchone())

    def test_deleted_comment_is_rejected_on_first_attempt(self):
        execute = self._post_with(_http_error(404, "parentCommentNotFound"))
        self.assertEqual(execute.call_count, 1)
        row = self._row()
        self.assertEqual(row["status"], "rejected")
        self.assertIn("parentCommentNotFound", row["error"])
        # Nothing left for the next cycle to retry.
        self.assertEqual(self._post_with(_http_error(404, "parentCommentNotFound")).call_count, 0)

    def test_replies_disabled_thread_is_rejected_on_first_attempt(self):
        self._post_with(_http_error(400, "operationNotSupported"))
        self.assertEqual(self._row()["status"], "rejected")

    def test_transient_error_is_retried_then_capped(self):
        with patch.object(config, "YOUTUBE_MAX_POST_ATTEMPTS", 3):
            self._post_with(_http_error(500, "backendError"))
            self.assertEqual(self._row(), {"status": "failed", "retry_count": 1, "error": self._row()["error"]})
            self._post_with(_http_error(500, "backendError"))
            self.assertEqual(self._row()["status"], "failed")
            self.assertEqual(self._row()["retry_count"], 2)
            self._post_with(_http_error(500, "backendError"))
        row = self._row()
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["retry_count"], 3)
        self.assertIn("Gave up after 3 failed attempts", row["error"])

    def test_quota_exhaustion_is_not_counted_as_an_attempt(self):
        self._post_with(_http_error(403, "quotaExceeded"))
        row = self._row()
        self.assertNotEqual(row["status"], "rejected")
        self.assertEqual(row["retry_count"], 0)

    def test_classifier_reads_reason_from_details_or_body(self):
        self.assertTrue(youtube_client.is_permanent_reply_failure(_http_error(404, "parentCommentNotFound")))
        self.assertFalse(youtube_client.is_permanent_reply_failure(_http_error(500, "backendError")))
        self.assertFalse(youtube_client.is_permanent_reply_failure(_http_error(403, "quotaExceeded")))
        raw = HttpError(httplib2.Response({"status": 400}), b"operationNotSupported: canReply is false")
        self.assertTrue(youtube_client.is_permanent_reply_failure(raw))


if __name__ == "__main__":
    unittest.main()
