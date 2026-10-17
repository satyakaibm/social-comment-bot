"""Daily follower/subscriber counts: fetch, store, refresh, prune."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import config, db, meta_client, video_stats, youtube_client

from tests.helpers import make_page_config


class YouTubeFetchTests(unittest.TestCase):
    def _client(self, statistics):
        youtube = MagicMock()
        youtube.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC1", "statistics": statistics}]
        }
        return youtube

    def test_parses_counts_and_charges_one_unit(self):
        youtube = self._client({"subscriberCount": "12300", "videoCount": "410", "viewCount": "9876543",
                                "hiddenSubscriberCount": False})
        with patch.object(youtube_client, "db") as fake_db:
            counts = youtube_client.get_channel_statistics(youtube, page_key="hindolroad")
        self.assertEqual(counts, {"followers": 12300, "content_count": 410, "view_count": 9876543})
        youtube.channels.return_value.list.assert_called_once_with(part="statistics", mine=True)
        self.assertEqual(fake_db.add_quota_usage.call_args.args[2], 1)

    def test_hidden_subscriber_count_is_none(self):
        youtube = self._client({"subscriberCount": "0", "videoCount": "3", "hiddenSubscriberCount": "true"})
        with patch.object(youtube_client, "db"):
            counts = youtube_client.get_channel_statistics(youtube)
        self.assertIsNone(counts["followers"])
        self.assertEqual(counts["content_count"], 3)


class MetaFetchTests(unittest.TestCase):
    def test_facebook_page_followers(self):
        page = make_page_config(key="fb", facebook_page_id="555")
        with patch.object(config, "PAGES", {**config.PAGES, "fb": page}), \
             patch.object(meta_client, "graph_get", return_value={"followers_count": 820, "fan_count": 800}) as get:
            counts = meta_client.get_facebook_page_followers(page_key="fb")
        self.assertEqual(counts, {"followers": 820, "content_count": None})
        self.assertEqual(get.call_args.args[0], "555")
        self.assertEqual(get.call_args.kwargs["fields"], "followers_count,fan_count")

    def test_facebook_falls_back_to_fan_count(self):
        page = make_page_config(key="fb", facebook_page_id="555")
        with patch.object(config, "PAGES", {**config.PAGES, "fb": page}), \
             patch.object(meta_client, "graph_get", return_value={"fan_count": 800}):
            self.assertEqual(meta_client.get_facebook_page_followers(page_key="fb")["followers"], 800)

    def test_instagram_followers(self):
        page = make_page_config(key="ig", instagram_user_id="1789")
        with patch.object(config, "PAGES", {**config.PAGES, "ig": page}), \
             patch.object(meta_client, "graph_get", return_value={"followers_count": 4321, "media_count": 97}) as get:
            counts = meta_client.get_instagram_followers(page_key="ig")
        self.assertEqual(counts, {"followers": 4321, "content_count": 97})
        self.assertEqual(get.call_args.args[0], "1789")

    def test_requires_the_account(self):
        page = make_page_config(key="none")
        with patch.object(config, "PAGES", {**config.PAGES, "none": page}):
            with self.assertRaises(RuntimeError):
                meta_client.get_instagram_followers(page_key="none")
            with self.assertRaises(RuntimeError):
                meta_client.get_facebook_page_followers(page_key="none")


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()


class StoreTests(_DbCase):
    def test_one_row_per_ist_day_keeps_latest_value(self):
        with db.connect() as conn:
            # 20:00 UTC on the 1st is 01:30 IST on the 2nd: the row lands on the 2nd.
            day = db.upsert_audience_size(conn, platform="youtube", page_key="hindolroad", followers=100,
                                          captured_at="2026-10-01T20:00:00+00:00")
            self.assertEqual(day, "2026-10-02")
            db.upsert_audience_size(conn, platform="youtube", page_key="hindolroad", followers=105,
                                    captured_at="2026-10-02T10:00:00+00:00")
            rows = db.list_audience_size(conn, platform="youtube", days=36500)
        self.assertEqual([(r["day"], r["followers"]) for r in rows], [("2026-10-02", 105)])

    def test_list_filters_platform_page_and_window(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            for days_ago, followers in ((40, 1), (5, 2), (0, 3)):
                db.upsert_audience_size(conn, platform="instagram", page_key="hindolroad", followers=followers,
                                        captured_at=(now - timedelta(days=days_ago)).isoformat())
            db.upsert_audience_size(conn, platform="facebook", page_key="other", followers=9,
                                    captured_at=now.isoformat())
            recent = db.list_audience_size(conn, platform="instagram", days=30)
            by_page = db.list_audience_size(conn, page_key="other", days=30)
            everything = db.list_audience_size(conn, days=365)
        self.assertEqual([r["followers"] for r in recent], [2, 3])
        self.assertEqual([r["platform"] for r in by_page], ["facebook"])
        self.assertEqual(len(everything), 4)

    def test_fetched_within_and_prune(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            db.upsert_audience_size(conn, platform="youtube", page_key="hindolroad", followers=1,
                                    captured_at=(now - timedelta(hours=2)).isoformat())
            db.upsert_audience_size(conn, platform="youtube", page_key="hindolroad", followers=1,
                                    captured_at=(now - timedelta(days=45)).isoformat())
            db.upsert_audience_size(conn, platform="instagram", page_key="hindolroad", followers=1,
                                    captured_at=(now - timedelta(days=45)).isoformat())
            self.assertTrue(db.audience_size_fetched_within(conn, platform="youtube", page_key="hindolroad", hours=6))
            self.assertFalse(db.audience_size_fetched_within(conn, platform="youtube", page_key="hindolroad", hours=1))
            self.assertFalse(db.audience_size_fetched_within(conn, platform="instagram", page_key="hindolroad", hours=6))
            self.assertEqual(db.prune_audience_size(conn, platform="youtube", retention_days=30), 1)
            self.assertEqual(db.prune_audience_size(conn, platform="instagram", retention_days=365), 0)
            self.assertEqual(len(db.list_audience_size(conn, days=36500)), 2)


class RefreshTests(_DbCase):
    def _pages(self):
        return {
            "yt": make_page_config(key="yt", label="YT", youtube_refresh_token="tok"),
            "meta": make_page_config(key="meta", label="Meta", facebook_page_id="555", instagram_user_id="1789"),
        }

    def test_refresh_stores_every_configured_channel_once(self):
        fake_youtube = MagicMock()
        with patch.object(config, "PAGES", self._pages()), \
             patch.object(config, "AUDIENCE_SIZE_ENABLED", True), \
             patch.object(youtube_client, "get_client", return_value=fake_youtube), \
             patch.object(youtube_client, "get_channel_statistics",
                          return_value={"followers": 12300, "content_count": 410, "view_count": 1}) as yt, \
             patch.object(meta_client, "get_facebook_page_followers", return_value={"followers": 820, "content_count": None}) as fb, \
             patch.object(meta_client, "get_instagram_followers", return_value={"followers": 4321, "content_count": 97}) as ig:
            with db.connect() as conn:
                first = video_stats._refresh_audience_size(conn, youtube=True, meta=True)
                second = video_stats._refresh_audience_size(conn, youtube=True, meta=True)
                rows = db.list_audience_size(conn, days=30)
        self.assertEqual(first, 3)
        self.assertEqual(second, 0)   # within AUDIENCE_SIZE_REFRESH_HOURS: no second round of calls
        self.assertEqual(yt.call_count, 1)
        self.assertEqual(fb.call_count, 1)
        self.assertEqual(ig.call_count, 1)
        self.assertEqual({(r["platform"], r["page_key"], r["followers"]) for r in rows},
                         {("youtube", "yt", 12300), ("facebook", "meta", 820), ("instagram", "meta", 4321)})

    def test_refresh_respects_platform_ticks_and_failures(self):
        with patch.object(config, "PAGES", self._pages()), \
             patch.object(config, "AUDIENCE_SIZE_ENABLED", True), \
             patch.object(youtube_client, "get_client", side_effect=RuntimeError("no creds")) as yt, \
             patch.object(meta_client, "get_facebook_page_followers", side_effect=RuntimeError("graph down")), \
             patch.object(meta_client, "get_instagram_followers", return_value={"followers": None, "content_count": 1}):
            with db.connect() as conn:
                updated = video_stats._refresh_audience_size(conn, youtube=False, meta=True)
                rows = db.list_audience_size(conn, days=30)
        self.assertEqual(updated, 0)
        self.assertEqual(rows, [])
        yt.assert_not_called()   # youtube was not due this tick

    def test_disabled_flag_makes_no_calls(self):
        with patch.object(config, "PAGES", self._pages()), \
             patch.object(config, "AUDIENCE_SIZE_ENABLED", False), \
             patch.object(youtube_client, "get_client") as yt, \
             patch.object(meta_client, "get_instagram_followers") as ig:
            with db.connect() as conn:
                self.assertEqual(video_stats._refresh_audience_size(conn, youtube=True, meta=True), 0)
        yt.assert_not_called()
        ig.assert_not_called()
