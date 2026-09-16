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
        self.assertEqual(activity[0]["unique_authors"], 1)
        self.assertEqual([row["text"] for row in texts], ["Where is this?"])
        self.assertIn("weekday_ist", texts[0])
        self.assertIn("hour_ist", texts[0])

    def test_engagement_activity_attributes_growth_to_later_capture_hour(self):
        reference = datetime(2026, 9, 11, 13, tzinfo=timezone.utc)
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", video_id="peak", page_key="travel",
                video_title="Aarti", view_count=100, like_count=10,
                share_count=1, comment_count=4,
            )
            db.upsert_video_stats(
                conn, platform="youtube", video_id="peak", page_key="travel",
                video_title="Aarti", view_count=180, like_count=22,
                share_count=3, comment_count=9,
            )
            rows = list(conn.execute(
                "SELECT id FROM video_stats_history WHERE video_id = 'peak' ORDER BY id"
            ))
            conn.execute(
                "UPDATE video_stats_history SET captured_at = ? WHERE id = ?",
                ((reference - timedelta(minutes=40)).isoformat(), rows[0]["id"]),
            )
            conn.execute(
                "UPDATE video_stats_history SET captured_at = ? WHERE id = ?",
                ((reference - timedelta(minutes=10)).isoformat(), rows[1]["id"]),
            )
            activity = db.engagement_activity(
                conn, horizon=timedelta(days=1), page_key="travel",
                reference_time=reference,
            )

        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["view_growth"], 80)
        self.assertEqual(activity[0]["like_growth"], 12)
        self.assertEqual(activity[0]["weekday_ist"], 5)
        self.assertEqual(activity[0]["hour_ist"], 18)

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
