"""Momentum growth charts: dataset, direction rules, PNG rendering, route.

These tests need pandas/matplotlib/seaborn and a matplotlib that can start
on the host. On some macOS machines matplotlib segfaults while building its
font cache (a system font FreeType cannot parse), which would kill the whole
pytest process, so the import is probed in a subprocess first and the module
is skipped when it fails. The Docker test image (Dockerfile.test) always
runs them.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

from app import config, dashboard, db, growth_charts


def _matplotlib_starts() -> bool:
    if not growth_charts.available():
        return False
    probe = subprocess.run(
        [sys.executable, "-c", "import matplotlib.pyplot, seaborn, pandas"],
        capture_output=True, timeout=300, env={**os.environ, "MPLBACKEND": "Agg"},
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _matplotlib_starts(),
    reason="matplotlib/seaborn/pandas cannot start on this host; Dockerfile.test runs these",
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _history(conn, *, platform, video_id, title, start, per_day, likes_per_day, days=12,
             page_key=None):
    """Twelve days of two-hourly daytime snapshots so growth, daily and
    timing queries all have something to measure (engagement_activity only
    counts gaps of 4 hours or less), then the live video_stats row."""
    page_key = page_key or config.DEFAULT_PAGE_KEY
    now = datetime.now(timezone.utc)
    views = likes = 0
    for day in range(days, -1, -1):
        for hour in range(7, 24, 2):
            stamp = (now - timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
            if stamp > now:
                continue
            step = days - day + (hour - 7) / 16
            views = int(start + per_day * step)
            likes = int(likes_per_day * step)
            conn.execute(
                """INSERT INTO video_stats_history
                       (platform, video_id, page_key, video_title, view_count, like_count,
                        share_count, comment_count, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform, video_id, page_key, title, views, likes, 2, int(step), stamp.isoformat()),
            )
    db.upsert_video_stats(
        conn, platform=platform, video_id=video_id, page_key=page_key, video_title=title,
        view_count=views, like_count=likes, share_count=2, comment_count=days,
    )


class GrowthChartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, attr, value in (
            (db, "DB_PATH", Path(self.temp.name) / "comments.db"),
            (config, "DATA_DIR", Path(self.temp.name)),
            (config, "DASHBOARD_USERNAME", "admin"),
            (config, "DASHBOARD_PASSWORD_HASH", generate_password_hash("secret")),
            (config, "GROWTH_CHARTS_ENABLED", True),
        ):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._login_failures.clear()
        db.init_db()
        self._seed()
        self.app = dashboard.create_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as auth_session:
            auth_session["dashboard_authenticated"] = True
            auth_session["dashboard_auth_version"] = 1
            auth_session["dashboard_username"] = "admin"

    def _seed(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            _history(conn, platform="youtube", video_id="yt-hit", title="Puri beach at sunrise", start=1000, per_day=400, likes_per_day=30)
            _history(conn, platform="youtube", video_id="yt-slow", title="Temple walk", start=5000, per_day=5, likes_per_day=1)
            _history(conn, platform="youtube", video_id="yt-dead", title="Old vlog", start=800, per_day=0, likes_per_day=0)
            _history(conn, platform="instagram", video_id="ig-reel", title="Street food reel", start=300, per_day=120, likes_per_day=20)
            texts = [
                ("How do I reach Puri from Bhubaneswar?", "posted"),
                ("Please make a video on Konark", "posted"),
                ("Beautiful, love this", "posted"),
                ("Which camera do you use?", "pending_review"),
                ("Amazing work", "already_replied"),
                ("Sound is broken in this one", "failed"),
                ("Kab aa rahe ho Cuttack?", "posted"),
                ("Thanks for the tips", "rejected"),
            ]
            for index, (text, status) in enumerate(texts):
                platform = "instagram" if index % 3 == 2 else "youtube"
                published = now - timedelta(days=index % 6, hours=index)
                db.insert_comment(
                    conn, comment_id=f"c{index}", platform=platform,
                    video_id="yt-hit" if platform == "youtube" else "ig-reel",
                    video_title="Puri beach at sunrise", author=f"viewer{index % 4}",
                    text=text, published_at=published.isoformat(), draft_reply="Thanks!",
                    page_key=config.DEFAULT_PAGE_KEY,
                )
                conn.execute(
                    "UPDATE comments SET status = ?, updated_at = ? WHERE comment_id = ?",
                    (status, (published + timedelta(hours=2)).isoformat(), f"c{index}"),
                )
            # Repeat commenters across three earlier weeks, so the community
            # chart has returning people and the lifecycle chart has a plan.
            extra = [("viewer0", 20), ("viewer0", 13), ("viewer0", 6), ("viewer9", 20),
                     ("viewer8", 13), ("viewer8", 6), ("viewer8", 2)]
            for n, (author, day) in enumerate(extra):
                published = now - timedelta(days=day, hours=3)
                db.insert_comment(
                    conn, comment_id=f"x{n}", platform="youtube",
                    video_id="yt-hit" if n % 2 else "yt-slow", video_title="Puri beach at sunrise",
                    author=author, text="Nice one", published_at=published.isoformat(),
                    draft_reply="Thanks!", page_key=config.DEFAULT_PAGE_KEY,
                )
                conn.execute(
                    "UPDATE comments SET status = 'posted', updated_at = ? WHERE comment_id = ?",
                    ((published + timedelta(hours=2)).isoformat(), f"x{n}"),
                )
            db.sync_seen_stats_from_comments(conn)
            for day in range(12, -1, -1):
                taken = (now - timedelta(days=day)).isoformat()
                db.upsert_audience_size(conn, platform="youtube", page_key=config.DEFAULT_PAGE_KEY,
                                        followers=10_000 + 40 * (12 - day), content_count=120, captured_at=taken)
                db.upsert_audience_size(conn, platform="instagram", page_key=config.DEFAULT_PAGE_KEY,
                                        followers=2_500 + 5 * (12 - day), content_count=80, captured_at=taken)
            for day in range(3):
                bucket_end = (now - timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
                for hour in range(24):
                    conn.execute(
                        """INSERT INTO audience_online
                               (platform, page_key, bucket_end, hour_utc, online_count, fetched_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        ("instagram", config.DEFAULT_PAGE_KEY, bucket_end.isoformat(), hour,
                         50 + (100 if hour in (14, 15, 16) else 0), now.isoformat()),
                    )

    def _dataset(self, platform=None, page_key=None):
        with db.connect() as conn:
            return growth_charts.build_dataset(conn, platform=platform, page_key=page_key, days=30)

    def test_dataset_aggregates_every_source(self):
        data = self._dataset()
        board = data["scoreboard"].set_index("platform")
        self.assertEqual(sorted(board.index), ["instagram", "youtube"])
        self.assertEqual(board.loc["youtube", "videos_tracked"], 3)
        self.assertGreater(board.loc["youtube", "view_growth"], board.loc["instagram", "view_growth"])
        # yt-dead never moved, yt-slow and yt-hit did.
        self.assertEqual(board.loc["youtube", "videos_measured"], 3)
        self.assertEqual(board.loc["youtube", "videos_with_view_growth"], 2)
        self.assertEqual(data["movers"].iloc[0]["video_id"], "yt-hit")
        self.assertFalse(data["daily"].empty)
        self.assertFalse(data["engagement"].empty)
        self.assertFalse(data["audience"].empty)
        self.assertEqual(int(data["intents"]["total"].sum()), 15)
        # Comments 2 and 5 land on instagram (index % 3 == 2): one posted, one failed.
        coverage = data["coverage"].set_index("platform")
        self.assertEqual(coverage.loc["youtube", "total"], 13)
        self.assertEqual(coverage.loc["youtube", "failed"], 0)
        self.assertEqual(coverage.loc["youtube", "pending"], 1)
        self.assertAlmostEqual(coverage.loc["youtube", "median_reply_hours"], 2.0)
        self.assertEqual(coverage.loc["instagram", "failed"], 1)
        self.assertEqual(len(data["online_profile"]), 24)
        # Every seeded video was first seen 12 days ago, well inside the window.
        self.assertEqual(data["lifecycle"]["video_id"].nunique(), 4)
        verdicts = data["lifecycle_verdicts"].set_index("video_id")
        self.assertEqual(verdicts.loc["yt-hit", "verdict"], "above median")
        self.assertEqual(verdicts.loc["yt-dead", "verdict"], "below median")
        self.assertEqual(verdicts.loc["ig-reel", "verdict"], "no comparison yet")  # one instagram upload, no median
        weeks = data["community_weeks"]
        self.assertFalse(weeks.empty)
        self.assertGreaterEqual(int(weeks["returning"].sum()), 1)
        fans = data["superfans"].set_index("commenter")
        self.assertGreaterEqual(fans.loc["viewer0", "comments"], 3)
        summary = data["follower_summary"].set_index("platform")
        self.assertEqual(summary.loc["youtube", "latest"], 10_480)
        self.assertEqual(summary.loc["youtube", "gain_30d"], 480)
        self.assertEqual(summary.loc["youtube", "gain_7d"], 280)
        self.assertEqual(summary.loc["instagram", "gain_30d"], 60)

    def test_platform_filter_limits_dataset(self):
        data = self._dataset(platform="instagram")
        self.assertEqual(list(data["scoreboard"]["platform"]), ["instagram"])
        self.assertTrue((data["comments"]["platform"] == "instagram").all())
        self.assertFalse(data["online_profile"].empty)
        youtube_only = self._dataset(platform="youtube")
        self.assertTrue(youtube_only["online_profile"].empty)

    def test_direction_rules_turn_numbers_into_a_plan(self):
        result = growth_charts.direction(self._dataset())
        titles = [section["title"] for section in result["findings"]]
        self.assertIn("Where growth is coming from", titles)
        # 2 of the 3 measured youtube items moved, so the "stalled catalogue"
        # warning (fewer than half moving) must not fire.
        growth_points = " ".join(result["findings"][0]["points"])
        self.assertNotIn("gained no views", growth_points)
        self.assertIn("Are we replying well enough?", titles)
        plan = "\n".join(result["plan"])
        self.assertIn("failed instagram replies", plan)
        self.assertIn("Q&A post for youtube", plan)  # 3 of 6 youtube comments are questions
        self.assertIn("Schedule youtube uploads", plan)
        self.assertIn("Instagram reels at about", plan)
        self.assertIn("Start one experiment", plan)
        self.assertIn("Follow up on Puri beach at sunrise (youtube)", plan)
        self.assertIn("repeat commenters on youtube", plan)
        self.assertIn("How new uploads are tracking", titles)
        self.assertIn("Is a community forming?", titles)
        self.assertEqual(titles[0], "Audience size")
        audience = " ".join(result["findings"][0]["points"])
        self.assertIn("10,480 subscribers", audience)
        self.assertIn("+480 over the 13 days held", audience)
        self.assertEqual(len(result["plan"]), len(set(result["plan"])))

    def test_every_chart_with_data_renders_a_png(self):
        data = self._dataset()
        specs = {spec["name"] for spec in growth_charts.chart_specs(data)}
        self.assertEqual(specs, set(growth_charts.CHARTS))
        for name in growth_charts.CHARTS:
            png = growth_charts.render(data, name)
            self.assertIsNotNone(png, name)
            self.assertTrue(png.startswith(PNG_MAGIC), name)

    def test_empty_database_renders_nothing_but_still_directs(self):
        with db.connect() as conn:
            for table in ("comments", "seen_comments", "video_stats", "video_stats_history", "audience_online", "audience_size"):
                conn.execute(f"DELETE FROM {table}")
        data = self._dataset()
        self.assertEqual(growth_charts.chart_specs(data), [])
        for name in growth_charts.CHARTS:
            self.assertIsNone(growth_charts.render(data, name), name)
        self.assertTrue(growth_charts.direction(data)["plan"])

    def test_chart_route_serves_cached_png(self):
        response = self.client.get("/momentum/charts/scoreboard.png?platform=youtube")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertTrue(response.data.startswith(PNG_MAGIC))
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertEqual(self.client.get("/momentum/charts/nope.png").status_code, 404)
        # instagram_online has no data once youtube is selected -> 404, not a broken image
        self.assertEqual(self.client.get("/momentum/charts/instagram_online.png?platform=youtube").status_code, 404)

    def test_chart_route_requires_login(self):
        anonymous = self.app.test_client()
        response = anonymous.get("/momentum/charts/scoreboard.png")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_momentum_page_shows_section_and_chart_images(self):
        response = self.client.get("/momentum")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Growth charts and direction", html)
        self.assertIn("Your plan for the next 30 days", html)
        self.assertIn("/momentum/charts/scoreboard.png?platform=&amp;page_key=", html)
        self.assertIn("/momentum/charts/timing.png", html)
        self.assertIn("/momentum/charts/lifecycle.png", html)
        self.assertIn("/momentum/charts/community.png", html)
        self.assertIn("/momentum/charts/followers.png", html)

    def test_disabled_flag_hides_section_and_route(self):
        with patch.object(config, "GROWTH_CHARTS_ENABLED", False):
            html = self.client.get("/momentum").get_data(as_text=True)
            self.assertNotIn("Growth charts and direction", html)
            self.assertEqual(self.client.get("/momentum/charts/scoreboard.png").status_code, 404)
