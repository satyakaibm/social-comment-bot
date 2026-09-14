import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import config, db, meta_client, video_stats, youtube_client


class VideoStatsDbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_distinct_containers_returns_most_recently_active_first(self):
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id="c1", platform="youtube", video_id="vid1",
                video_title="Older", author="a", text="hi", published_at="",
                draft_reply="",
            )
            db.insert_comment(
                conn, comment_id="c2", platform="youtube", video_id="vid2",
                video_title="Newer", author="a", text="hi", published_at="",
                draft_reply="",
            )
            conn.execute(
                "UPDATE comments SET created_at = '2026-01-01T00:00:00+00:00' "
                "WHERE comment_id = 'c1'"
            )
            conn.execute(
                "UPDATE comments SET created_at = '2026-06-01T00:00:00+00:00' "
                "WHERE comment_id = 'c2'"
            )
            containers = db.distinct_containers(conn, platform="youtube", limit=10)
        self.assertEqual([c["video_id"] for c in containers], ["vid2", "vid1"])
        self.assertEqual(containers[0]["page_key"], config.DEFAULT_PAGE_KEY)

    def test_distinct_containers_respects_limit(self):
        with db.connect() as conn:
            for i in range(5):
                db.insert_comment(
                    conn, comment_id=f"c{i}", platform="youtube", video_id=f"vid{i}",
                    video_title="T", author="a", text="hi", published_at="",
                    draft_reply="",
                )
            containers = db.distinct_containers(conn, platform="youtube", limit=2)
        self.assertEqual(len(containers), 2)

    def test_upsert_and_list_video_stats(self):
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1",
                page_key=config.DEFAULT_PAGE_KEY, video_title="Aarti",
                like_count=10, share_count=None, comment_count=3,
            )
        with db.connect() as conn:
            rows = db.list_video_stats(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["like_count"], 10)
        self.assertIsNone(rows[0]["share_count"])
        self.assertEqual(rows[0]["comment_count"], 3)

    def test_upsert_video_stats_overwrites_existing_row(self):
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1", page_key="",
                video_title="Aarti", like_count=10, share_count=None,
                comment_count=3,
            )
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1", page_key="",
                video_title="Aarti", like_count=20, share_count=None,
                comment_count=5,
            )
            rows = db.list_video_stats(conn)
            history = db.list_video_stats_history(conn, video_id="vid1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(history), 2)
        self.assertEqual(rows[0]["like_count"], 20)
        self.assertEqual(rows[0]["comment_count"], 5)
        self.assertEqual([row["like_count"] for row in history], [20, 10])

    def test_prune_video_stats_history_removes_expired_snapshots(self):
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1", page_key="",
                video_title="Aarti", like_count=10, share_count=None,
                comment_count=3,
            )
            conn.execute(
                "UPDATE video_stats_history SET captured_at = '2020-01-01T00:00:00+00:00'"
            )
            removed = db.prune_video_stats_history(conn, retention_days=90)
            history = db.list_video_stats_history(conn)

        self.assertEqual(removed, 1)
        self.assertEqual(history, [])

    def test_video_stats_growth_returns_real_deltas(self):
        reference_time = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1", page_key="travel",
                video_title="Beach", view_count=100, like_count=10,
                share_count=None, comment_count=2,
            )
            conn.execute(
                "UPDATE video_stats_history SET captured_at = ?",
                ((reference_time - timedelta(hours=20)).isoformat(),),
            )
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1", page_key="travel",
                video_title="Beach", view_count=180, like_count=16,
                share_count=None, comment_count=5,
            )
            conn.execute(
                "UPDATE video_stats_history SET captured_at = ? WHERE id = 2",
                ((reference_time - timedelta(hours=1)).isoformat(),),
            )
            growth = db.video_stats_growth(
                conn, horizon=timedelta(hours=24), page_key="travel",
                reference_time=reference_time,
            )

        self.assertEqual(len(growth), 1)
        self.assertEqual(growth[0]["view_growth"], 80)
        self.assertEqual(growth[0]["like_growth"], 6)
        self.assertEqual(growth[0]["comment_growth"], 3)
        self.assertIsNone(growth[0]["share_growth"])
        self.assertAlmostEqual(growth[0]["elapsed_hours"], 19, places=3)

    def test_list_video_stats_filters_by_platform(self):
        with db.connect() as conn:
            db.upsert_video_stats(
                conn, platform="youtube", video_id="vid1", page_key="",
                video_title="Aarti", like_count=1, share_count=None,
                comment_count=1,
            )
            db.upsert_video_stats(
                conn, platform="facebook", video_id="post1", page_key="",
                video_title="Post", like_count=2, share_count=1,
                comment_count=2,
            )
            rows = db.list_video_stats(conn, platform="facebook")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "facebook")

    def test_list_video_stats_sorts_by_updated_time(self):
        with db.connect() as conn:
            for video_id in ("older", "newer"):
                db.upsert_video_stats(
                    conn, platform="youtube", video_id=video_id, page_key="",
                    video_title=video_id.title(), like_count=1,
                    share_count=None, comment_count=1,
                )
            conn.execute(
                "UPDATE video_stats SET updated_at = ? WHERE video_id = ?",
                ("2026-01-01T00:00:00+00:00", "older"),
            )
            conn.execute(
                "UPDATE video_stats SET updated_at = ? WHERE video_id = ?",
                ("2026-06-01T00:00:00+00:00", "newer"),
            )
            descending = db.list_video_stats(
                conn, sort_by="updated", sort_dir="desc"
            )
            ascending = db.list_video_stats(
                conn, sort_by="updated", sort_dir="asc"
            )

        self.assertEqual([row["video_id"] for row in descending], ["newer", "older"])
        self.assertEqual([row["video_id"] for row in ascending], ["older", "newer"])


class YouTubeStatsTests(unittest.TestCase):
    def test_get_video_stats_batches_and_parses_counts(self):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {"id": "vid1", "statistics": {
                    "viewCount": "1200", "likeCount": "10", "commentCount": "3"
                }},
                {"id": "vid2", "statistics": {}},
            ]
        }
        with patch.object(youtube_client, "db"):
            stats = youtube_client.get_video_stats(youtube, ["vid1", "vid2"])
        self.assertEqual(stats["vid1"], {
            "view_count": 1200, "like_count": 10, "comment_count": 3,
        })
        self.assertEqual(stats["vid2"], {
            "view_count": None, "like_count": None, "comment_count": None,
        })
        youtube.videos.return_value.list.assert_called_once_with(
            part="statistics", id="vid1,vid2"
        )

    def test_get_video_stats_dedupes_and_batches_over_fifty(self):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {"items": []}
        video_ids = [f"vid{i}" for i in range(60)] + ["vid0"]
        with patch.object(youtube_client, "db"):
            youtube_client.get_video_stats(youtube, video_ids)
        self.assertEqual(youtube.videos.return_value.list.call_count, 2)


class MetaStatsTests(unittest.TestCase):
    def test_get_facebook_post_stats_parses_summaries(self):
        with patch.object(
            meta_client, "graph_get",
            return_value={
                "likes": {"summary": {"total_count": 5}},
                "comments": {"summary": {"total_count": 2}},
                "shares": {"count": 7},
            },
        ) as graph_get:
            stats = meta_client.get_facebook_post_stats("post1", page_key="p")
        self.assertEqual(stats, {"like_count": 5, "comment_count": 2, "share_count": 7})
        graph_get.assert_called_once_with(
            "post1",
            fields="likes.summary(true).limit(0),comments.summary(true).limit(0),shares",
            page_key="p",
        )

    def test_get_facebook_post_stats_defaults_shares_to_zero_when_absent(self):
        with patch.object(
            meta_client, "graph_get",
            return_value={
                "likes": {"summary": {"total_count": 5}},
                "comments": {"summary": {"total_count": 0}},
            },
        ):
            stats = meta_client.get_facebook_post_stats("post1")
        self.assertEqual(stats["share_count"], 0)

    def test_get_instagram_media_stats_parses_counts(self):
        with patch.object(
            meta_client, "graph_get",
            return_value={"like_count": 4, "comments_count": 1},
        ) as graph_get:
            stats = meta_client.get_instagram_media_stats("media1", page_key="p")
        self.assertEqual(stats, {"like_count": 4, "comment_count": 1})
        graph_get.assert_called_once_with(
            "media1", fields="like_count,comments_count", page_key="p"
        )


class RefreshAllTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_refresh_all_upserts_stats_for_each_platform(self):
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id="c1", platform="youtube", video_id="vid1",
                video_title="Aarti", author="a", text="hi", published_at="",
                draft_reply="",
            )
            db.insert_comment(
                conn, comment_id="c2", platform="facebook", video_id="post1",
                video_title="Post", author="a", text="hi", published_at="",
                draft_reply="",
            )
            db.insert_comment(
                conn, comment_id="c3", platform="instagram", video_id="media1",
                video_title="Reel", author="a", text="hi", published_at="",
                draft_reply="",
            )
        fake_youtube = MagicMock()
        with patch.object(video_stats.db, "connect", wraps=db.connect) as connections, \
             patch.object(youtube_client, "get_client", return_value=fake_youtube), \
             patch.object(
                 youtube_client, "get_video_stats",
                 return_value={"vid1": {
                     "view_count": 1200, "like_count": 10, "comment_count": 3,
                 }},
             ) as youtube_stats, \
             patch.object(
                 meta_client, "get_facebook_post_stats",
                 return_value={"like_count": 5, "comment_count": 2, "share_count": 1},
             ) as facebook_stats, \
             patch.object(
                 meta_client, "get_instagram_media_stats",
                 return_value={"like_count": 8, "comment_count": 4},
             ) as instagram_stats:
            updated = video_stats.refresh_all()

        self.assertEqual(updated, 3)
        self.assertEqual(connections.call_count, 1)
        quota_connections = {
            youtube_stats.call_args.kwargs["quota_conn"],
            facebook_stats.call_args.kwargs["quota_conn"],
            instagram_stats.call_args.kwargs["quota_conn"],
        }
        self.assertEqual(len(quota_connections), 1)
        with db.connect() as conn:
            rows = {row["platform"]: row for row in db.list_video_stats(conn, limit=10)}
        self.assertEqual(rows["youtube"]["like_count"], 10)
        self.assertEqual(rows["youtube"]["view_count"], 1200)
        self.assertIsNone(rows["youtube"]["share_count"])
        self.assertIsNone(rows["facebook"]["view_count"])
        self.assertEqual(rows["facebook"]["share_count"], 1)
        self.assertEqual(rows["instagram"]["like_count"], 8)
        self.assertIsNone(rows["instagram"]["share_count"])
        self.assertIsNone(rows["instagram"]["view_count"])

    def test_refresh_all_continues_after_one_platform_errors(self):
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id="c1", platform="facebook", video_id="post1",
                video_title="Post", author="a", text="hi", published_at="",
                draft_reply="",
            )
        with patch.object(youtube_client, "get_client", side_effect=RuntimeError("boom")), \
             patch.object(
                 meta_client, "get_facebook_post_stats",
                 return_value={"like_count": 1, "comment_count": 1, "share_count": 0},
             ):
            updated = video_stats.refresh_all()
        self.assertEqual(updated, 1)


class WorkerScheduleTests(unittest.TestCase):
    def test_worker_refreshes_youtube_and_meta_every_thirty_minutes(self):
        class StopAfterTwoWaits:
            def __init__(self):
                self.stopped = False
                self.waits = []

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.waits.append(seconds)
                if len(self.waits) == 2:
                    self.stopped = True

        stop = StopAfterTwoWaits()
        with patch.object(video_stats.db, "init_db"), \
             patch.object(config, "YOUTUBE_VIDEO_STATS_REFRESH_MINUTES", 30), \
             patch.object(config, "META_VIDEO_STATS_REFRESH_MINUTES", 30), \
             patch.object(
                 video_stats.time, "monotonic",
                 side_effect=[0, 1, 1, 1801, 1802, 1802],
             ), \
             patch.object(video_stats, "refresh_selected") as refresh:
            video_stats.worker_loop(stop)

        self.assertEqual(
            [call.kwargs for call in refresh.call_args_list],
            [
                {"youtube": True, "meta": True},
                {"youtube": True, "meta": True},
            ],
        )
        self.assertEqual(stop.waits, [1800, 1800])


if __name__ == "__main__":
    unittest.main()
