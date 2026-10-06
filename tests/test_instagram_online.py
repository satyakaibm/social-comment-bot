"""Instagram's own follower-online counts: fetch, store, present, render."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash

from app import analytics, config, dashboard, db, meta_client, video_stats

from tests.helpers import make_page_config


def _graph_payload():
    """Shape of GET /{ig-user-id}/insights?metric=online_followers&period=lifetime."""
    def day(end, peak):
        hours = {str(h): 1000 for h in range(24)}
        for h in peak:
            hours[str(h)] = 4000
        return {"value": hours, "end_time": end}
    return {
        "data": [{
            "name": "online_followers",
            "period": "lifetime",
            "values": [
                day("2026-09-30T07:00:00+0000", (6, 7, 8)),
                day("2026-10-01T07:00:00+0000", (6, 7, 8)),
                {"value": {}, "end_time": "2026-10-02T07:00:00+0000"},   # not published yet
                {"value": {}, "end_time": "2026-10-03T07:00:00+0000"},
            ],
        }]
    }


class FetchTests(unittest.TestCase):
    def test_parses_published_days_and_drops_empty_ones(self):
        page = make_page_config(key="ig", instagram_user_id="1789")
        with patch.object(config, "PAGES", {**config.PAGES, "ig": page}), \
             patch.object(meta_client, "graph_get", return_value=_graph_payload()) as get:
            buckets = meta_client.get_instagram_online_followers(page_key="ig", days=7)

        self.assertEqual([b["bucket_end"] for b in buckets],
                         ["2026-09-30T07:00:00+0000", "2026-10-01T07:00:00+0000"])
        self.assertEqual(buckets[0]["hours"][6], 4000)
        self.assertEqual(buckets[0]["hours"][0], 1000)
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["metric"], "online_followers")
        self.assertEqual(kwargs["period"], "lifetime")
        # The trailing-week range is what makes Meta return published days at
        # all; without since/until only the two still-empty days come back.
        self.assertEqual(kwargs["until"] - kwargs["since"], 7 * 86400)
        self.assertEqual(get.call_args.args[0], "1789/insights")

    def test_requires_an_instagram_account(self):
        page = make_page_config(key="fb_only", instagram_user_id="")
        with patch.object(config, "PAGES", {**config.PAGES, "fb_only": page}):
            with self.assertRaises(RuntimeError):
                meta_client.get_instagram_online_followers(page_key="fb_only")


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()



class StoreTests(_DbCase):
    def test_stores_sqlite_readable_timestamps(self):
        # Meta's +0000 is not a zone SQLite's datetime() understands; stored
        # as +00:00 the date filters below actually match.
        with db.connect() as conn:
            db.upsert_audience_online(conn, platform="instagram", page_key="ig",
                                      buckets=[{"bucket_end": "2026-10-01T07:00:00+0000", "hours": {6: 1}}])
            rows = db.list_audience_online(conn, platform="instagram", page_key="ig", days=3650)
        self.assertEqual(rows[0]["bucket_end"], "2026-10-01T07:00:00+00:00")

    def test_upsert_is_idempotent_and_refetch_overwrites(self):
        buckets = [{"bucket_end": "2026-10-01T07:00:00+0000", "hours": {6: 4000, 7: 4100}}]
        with db.connect() as conn:
            self.assertEqual(db.upsert_audience_online(conn, platform="instagram", page_key="ig", buckets=buckets), 2)
            buckets[0]["hours"][6] = 4500
            db.upsert_audience_online(conn, platform="instagram", page_key="ig", buckets=buckets)
            rows = db.list_audience_online(conn, platform="instagram", page_key="ig", days=3650)
        self.assertEqual([(r["hour_utc"], r["online_count"]) for r in rows], [(6, 4500), (7, 4100)])

    def test_fetched_within_and_prune(self):
        with db.connect() as conn:
            self.assertFalse(db.audience_online_fetched_within(conn, platform="instagram", page_key="ig", hours=6))
            db.upsert_audience_online(conn, platform="instagram", page_key="ig",
                                      buckets=[{"bucket_end": "2020-01-01T07:00:00+0000", "hours": {6: 1}}])
            self.assertTrue(db.audience_online_fetched_within(conn, platform="instagram", page_key="ig", hours=6))
            self.assertFalse(db.audience_online_fetched_within(conn, platform="instagram", page_key="other", hours=6))
            self.assertEqual(db.prune_audience_online(conn, retention_days=90), 1)
            self.assertEqual(db.list_audience_online(conn, platform="instagram", days=3650), [])


class WorkerTests(_DbCase):
    def test_worker_fetches_each_ig_page_once_per_interval(self):
        buckets = [{"bucket_end": "2026-10-01T07:00:00+0000", "hours": {6: 4000}}]
        with patch.object(config, "instagram_page_keys", return_value=["ig"]), \
             patch.object(meta_client, "get_instagram_online_followers", return_value=buckets) as fetch, \
             db.connect() as conn:
            self.assertEqual(video_stats._refresh_instagram_online(conn), 1)
            self.assertEqual(video_stats._refresh_instagram_online(conn), 0)  # within interval
        self.assertEqual(fetch.call_count, 1)

    def test_worker_survives_a_failing_page(self):
        with patch.object(config, "instagram_page_keys", return_value=["bad", "good"]), \
             patch.object(meta_client, "get_instagram_online_followers",
                          side_effect=[RuntimeError("boom"),
                                       [{"bucket_end": "2026-10-01T07:00:00+0000", "hours": {6: 1}}]]), \
             db.connect() as conn:
            self.assertEqual(video_stats._refresh_instagram_online(conn), 1)


def _rows(page_key="ig", ends=("2026-09-30T07:00:00+0000", "2026-10-01T07:00:00+0000"), peak=(6, 7, 8)):
    rows = []
    for end in ends:
        for h in range(24):
            rows.append({"page_key": page_key, "platform": "instagram", "bucket_end": end,
                         "hour_utc": h, "online_count": 4000 if h in peak else 1000})
    return rows


class PresentTests(unittest.TestCase):
    def test_window_is_instagrams_peak_on_the_ist_clock(self):
        item = analytics.instagram_online_focus(_rows())[0]
        # UTC 06-09 -> 11:30 AM-2:30 PM IST, exactly what Instagram's app shows.
        self.assertEqual(item["window"], "11:30 AM–2:30 PM IST")
        self.assertEqual(item["window_avg_online"], 4000)
        self.assertEqual(item["peak_label"], "11:30 AM")
        self.assertEqual(item["days"], 2)
        self.assertEqual(item["confidence"], "early")
        self.assertIn("4,000 followers", item["message"])

    def test_profile_is_ordered_midnight_to_midnight_ist(self):
        profile = analytics.instagram_online_focus(_rows())[0]["hour_profile"]
        self.assertEqual(profile[0]["label"], "12:30 AM")     # UTC 19
        self.assertEqual(profile[-1]["label"], "11:30 PM")    # UTC 18
        self.assertEqual([s["hour_utc"] for s in profile[:3]], [19, 20, 21])
        self.assertEqual(sum(s["in_window"] for s in profile), 3)
        self.assertEqual(max(s["share"] for s in profile), 100)

    def test_window_may_wrap_midnight(self):
        # A late-night audience: UTC 17-19 = 10:30 PM-1:30 AM IST.
        item = analytics.instagram_online_focus(_rows(peak=(17, 18, 19)))[0]
        self.assertEqual(item["window"], "10:30 PM–1:30 AM IST")

    def test_weekday_attribution_splits_the_bucket_at_its_own_hour(self):
        # Bucket ending Thu 2026-10-01 07:00Z covers Wed 30 Sep 07-23Z and
        # Thu 1 Oct 00-06Z. A peak at UTC 3-5 (8:30-11:30 AM IST Thursday)
        # must land on Thursday; one at UTC 10-12 (3:30-6:30 PM IST
        # Wednesday) on Wednesday; and nothing on Friday or Tuesday.
        # Three buckets ending Wed 30 Sep, Thu 1 Oct, Fri 2 Oct 07:00Z fully
        # cover Wednesday and Thursday; Tuesday and Friday are partial.
        ends = tuple(f"2026-10-0{d}T07:00:00+0000" for d in (0, 1, 2)) if False else (
            "2026-09-30T07:00:00+0000", "2026-10-01T07:00:00+0000", "2026-10-02T07:00:00+0000")
        thursday = analytics.instagram_online_focus(_rows(ends=ends, peak=(3, 4, 5)))[0]
        wednesday = analytics.instagram_online_focus(_rows(ends=ends, peak=(10, 11, 12)))[0]
        by_day = {w["day"]: w for w in thursday["weekday_windows"]}
        self.assertEqual(by_day["Thursday"]["window"], "8:30 AM–11:30 AM IST")
        self.assertTrue(by_day["Wednesday"]["has_signal"])
        for quiet in ("Monday", "Sunday", "Saturday"):
            self.assertFalse(by_day[quiet]["has_signal"], quiet)
        for partial in ("Tuesday", "Friday"):
            self.assertTrue(by_day[partial]["partial"], partial)
        # Both fully covered days share the same curve here, so ties resolve
        # by score only; what matters is that the peak lands on a weekday at
        # all and the stored +00:00 form parses identically.
        self.assertIn(wednesday["best_weekday"], {"Wednesday", "Thursday"})
        same = analytics.instagram_online_focus(
            _rows(ends=tuple(e.replace("+0000", "+00:00") for e in ends), peak=(3, 4, 5))
        )[0]
        self.assertEqual({w["day"] for w in same["weekday_windows"] if w["has_signal"]},
                         {w["day"] for w in thursday["weekday_windows"] if w["has_signal"]})

    def test_partially_observed_weekday_is_not_ranked(self):
        # The newest bucket (ending Thu 07:00Z) gives Thursday only 00-06Z.
        # A peak inside those seven hours must not crown Thursday with a
        # "best window" built from a quarter of a day.
        item = analytics.instagram_online_focus(_rows(ends=("2026-10-01T07:00:00+0000",), peak=(3, 4, 5)))[0]
        by_day = {w["day"]: w for w in item["weekday_windows"]}
        self.assertFalse(by_day["Thursday"]["has_signal"])
        self.assertTrue(by_day["Thursday"]["partial"])
        self.assertFalse(by_day["Friday"]["partial"])
        # Wednesday has 07-23Z = 17 hours -- also short of a full day.
        self.assertFalse(by_day["Wednesday"]["has_signal"])
        self.assertEqual(item["best_weekday"], "")
        # Two consecutive buckets complete Thursday: its 00-06Z come from the
        # first (ending Thu 07:00Z) and its 07-23Z from the second (ending
        # Fri 07:00Z). Wednesday keeps only 07-23Z and Friday only 00-06Z,
        # so Thursday is the one weekday that ranks.
        two = analytics.instagram_online_focus(
            _rows(ends=("2026-10-01T07:00:00+0000", "2026-10-02T07:00:00+0000"), peak=(10, 11, 12))
        )[0]
        by_day = {w["day"]: w for w in two["weekday_windows"]}
        self.assertTrue(by_day["Thursday"]["has_signal"])
        self.assertEqual(by_day["Thursday"]["window"], "3:30 PM–6:30 PM IST")
        self.assertEqual(two["best_weekday"], "Thursday")
        self.assertTrue(by_day["Wednesday"]["partial"])
        self.assertTrue(by_day["Friday"]["partial"])
        self.assertFalse(by_day["Wednesday"]["has_signal"])

    def test_confidence_grows_with_days_held(self):
        ends = tuple(f"2026-09-{d:02d}T07:00:00+0000" for d in range(1, 15))
        self.assertEqual(analytics.instagram_online_focus(_rows(ends=ends))[0]["confidence"], "high")
        self.assertEqual(analytics.instagram_online_focus(_rows(ends=ends[:7]))[0]["confidence"], "medium")

    def test_no_rows_no_card(self):
        self.assertEqual(analytics.instagram_online_focus([]), [])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, attr, value in (
            (db, "DB_PATH", Path(self.temp.name) / "comments.db"),
            (config, "DATA_DIR", Path(self.temp.name)),
            (config, "DASHBOARD_USERNAME", "admin"),
            (config, "DASHBOARD_PASSWORD_HASH", generate_password_hash("secret")),
        ):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._login_failures.clear()
        db.init_db()
        self.client = dashboard.create_app().test_client()
        with self.client.session_transaction() as auth_session:
            auth_session["dashboard_authenticated"] = True
            auth_session["dashboard_auth_version"] = 1
            auth_session["dashboard_username"] = "admin"

    def _seed(self):
        from datetime import datetime, timedelta, timezone
        ends = [
            (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%dT07:00:00+0000")
            for offset in (3, 2)
        ]
        with db.connect() as conn:
            db.upsert_audience_online(
                conn, platform="instagram", page_key=config.DEFAULT_PAGE_KEY,
                buckets=[{"bucket_end": end, "hours": {h: (4000 if h in (6, 7, 8) else 1000) for h in range(24)}} for end in ends],
            )

    def test_momentum_shows_instagrams_most_active_times(self):
        self._seed()
        page = self.client.get("/momentum").data.decode()
        self.assertIn("Most active times · 11:30 AM–2:30 PM IST", page)
        self.assertIn("Instagram's own data", page)
        self.assertIn("source: Instagram online_followers, 2 day(s)", page)
        # One bar per hour, each carrying its IST label and count as a title.
        self.assertEqual(page.count(" followers online\""), 24)

    def test_card_is_scoped_to_the_instagram_filter(self):
        self._seed()
        self.assertIn("Most active times ·", self.client.get("/momentum?platform=instagram").data.decode())
        self.assertNotIn("Most active times ·", self.client.get("/momentum?platform=youtube").data.decode())

    def test_no_data_no_card(self):
        # The methodology footer always mentions the card by name; only the
        # card heading proves one was rendered.
        self.assertNotIn("Most active times ·", self.client.get("/momentum").data.decode())


if __name__ == "__main__":
    unittest.main()
