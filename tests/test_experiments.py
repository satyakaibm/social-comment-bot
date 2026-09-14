import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


class RecommendationExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_experiment_captures_baseline_and_reports_later_delta(self):
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", page_key="travel", video_id="video",
                video_title="Beach", view_count=100, like_count=10,
                comment_count=2, share_count=None,
            )
            created = db.create_recommendation_experiment(
                conn, recommendation_key="key", recommendation_type="current",
                platform="youtube", page_key="travel", video_id="video",
                title="Beach", recommendation="Build on Beach",
                test_dimension="title", variant_label="Question title",
                notes="A question may improve discovery",
            )
            duplicate = db.create_recommendation_experiment(
                conn, recommendation_key="key", recommendation_type="current",
                platform="youtube", page_key="travel", video_id="video",
                title="Beach", recommendation="Build on Beach",
            )
            db.upsert_video_stats(
                conn, platform="youtube", page_key="travel", video_id="video",
                video_title="Beach", view_count=175, like_count=16,
                comment_count=5, share_count=None,
            )
            experiments = db.list_recommendation_experiments(conn)

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(experiments[0]["baseline_views"], 100)
        self.assertEqual(experiments[0]["test_dimension"], "title")
        self.assertEqual(experiments[0]["variant_label"], "Question title")
        self.assertEqual(experiments[0]["notes"], "A question may improve discovery")
        self.assertEqual(experiments[0]["delta_views"], 75)
        self.assertEqual(experiments[0]["delta_likes"], 6)
        self.assertEqual(experiments[0]["delta_comments"], 3)
        self.assertIsNone(experiments[0]["delta_shares"])

    def test_experiment_status_is_validated(self):
        with db.connect() as conn:
            db.create_recommendation_experiment(
                conn, recommendation_key="key", recommendation_type="intent",
                platform="instagram", page_key="travel", video_id=None,
                title="Questions", recommendation="Create a Q&A",
            )
            experiment_id = db.list_recommendation_experiments(conn)[0]["id"]
            self.assertTrue(
                db.update_recommendation_experiment_status(conn, experiment_id, "completed")
            )
            self.assertFalse(
                db.update_recommendation_experiment_status(conn, experiment_id, "invalid")
            )
            status = db.list_recommendation_experiments(conn)[0]["status"]

        self.assertEqual(status, "completed")


if __name__ == "__main__":
    unittest.main()
