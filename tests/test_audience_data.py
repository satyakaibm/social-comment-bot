import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db


class AudienceDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_audience_activity_and_text_are_channel_filterable(self):
        reference = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        with db.connect() as conn:
            for comment_id, page_key, text in (
                ("travel", "travel", "Where is this?"),
                ("home", "hindolroad", "Beautiful"),
            ):
                db.insert_comment(
                    conn, comment_id=comment_id, platform="youtube",
                    page_key=page_key, video_id="video", video_title="Title",
                    author="viewer", text=text, published_at="", draft_reply="",
                )
            timestamp = (reference - timedelta(hours=2)).isoformat()
            conn.execute("UPDATE comments SET created_at = ?", (timestamp,))
            conn.execute("UPDATE seen_comments SET created_at = ?", (timestamp,))

            activity = db.audience_activity(
                conn, horizon=timedelta(days=1), page_key="travel",
                reference_time=reference,
            )
            texts = db.comment_text_sample(
                conn, horizon=timedelta(days=1), page_key="travel",
                reference_time=reference,
            )

        self.assertEqual(sum(row["comment_count"] for row in activity), 1)
        self.assertEqual(activity[0]["page_key"], "travel")
        self.assertEqual([row["text"] for row in texts], ["Where is this?"])

    def test_recently_active_video_ids_filters_by_horizon_platform_and_page(self):
        reference = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id="fresh", platform="youtube", page_key="travel",
                video_id="fresh-video", video_title="Title", author="viewer",
                text="hi", published_at="", draft_reply="",
            )
            db.insert_comment(
                conn, comment_id="stale", platform="youtube", page_key="travel",
                video_id="stale-video", video_title="Title", author="viewer",
                text="hi", published_at="", draft_reply="",
            )
            db.insert_comment(
                conn, comment_id="other-platform", platform="facebook", page_key="travel",
                video_id="fb-video", video_title="Title", author="viewer",
                text="hi", published_at="", draft_reply="",
            )
            db.insert_comment(
                conn, comment_id="other-page", platform="youtube", page_key="hindolroad",
                video_id="other-page-video", video_title="Title", author="viewer",
                text="hi", published_at="", draft_reply="",
            )
            conn.execute(
                "UPDATE comments SET created_at = ? WHERE comment_id IN "
                "('fresh', 'other-platform', 'other-page')",
                ((reference - timedelta(hours=2)).isoformat(),),
            )
            conn.execute(
                "UPDATE comments SET created_at = ? WHERE comment_id = 'stale'",
                ((reference - timedelta(days=40)).isoformat(),),
            )

            active = db.recently_active_video_ids(
                conn, platform="youtube", page_key="travel",
                horizon=timedelta(days=30), reference_time=reference,
            )

        self.assertEqual(active, ["fresh-video"])


if __name__ == "__main__":
    unittest.main()
