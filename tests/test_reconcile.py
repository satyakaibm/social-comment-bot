import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

from app import db, reconcile


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        with db.connect() as conn:
            for comment_id in ("answered", "open"):
                db.insert_comment(
                    conn,
                    comment_id=comment_id,
                    platform="youtube",
                    video_id="video",
                    video_title="Title",
                    author="viewer",
                    text="Jai Maa",
                    published_at="",
                    draft_reply="🙏",
                )

    def test_marks_only_confirmed_existing_replies(self):
        youtube = MagicMock()
        with patch.object(reconcile, "get_client", return_value=youtube), \
             patch.object(reconcile, "get_my_channel_id", return_value="oauth"), \
             patch.object(reconcile, "get_video_channel_ids", return_value={"video": "owner"}), \
             patch.object(reconcile, "find_own_reply", side_effect=["reply", None]) as find:
            result = reconcile.reconcile_youtube_pending()

        self.assertEqual(result["already_replied"], 1)
        self.assertEqual(result["unanswered"], 1)
        self.assertEqual(find.call_args_list[0].args[2], {"oauth", "owner"})
        with db.connect() as conn:
            self.assertEqual(db.get_comment(conn, "answered")["status"], "already_replied")
            self.assertEqual(db.get_comment(conn, "open")["status"], "pending_review")

    def test_quota_exhaustion_before_checks_returns_cleanly(self):
        error = HttpError(
            Response({"status": "403"}),
            b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}',
        )
        with patch.object(reconcile, "get_client", return_value=MagicMock()), \
             patch.object(reconcile, "get_my_channel_id", side_effect=error):
            result = reconcile.reconcile_youtube_pending()

        self.assertTrue(result["quota_exhausted"])
        self.assertEqual(result["checked"], 0)
        with db.connect() as conn:
            self.assertEqual(len(db.list_by_status(conn, "pending_review")), 2)
