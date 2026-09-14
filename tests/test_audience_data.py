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


if __name__ == "__main__":
    unittest.main()
