import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, db, dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        self.app = dashboard.create_app()
        self.client = self.app.test_client()
        with db.connect() as conn:
            db.insert_comment(
                conn,
                comment_id="c1",
                platform="youtube",
                video_id="vid",
                video_title="Aarti",
                author="viewer",
                text="What time is aarti?",
                published_at="",
                draft_reply="Aarti time is in the description.",
            )

    def test_pending_review_tab_is_shown(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"status=pending_review", page.data)
        self.assertIn(b"status=posted", page.data)
        self.assertNotIn(b"status=approved", page.data)
        pending = self.client.get("/?status=pending_review")
        self.assertIn(b"What time is aarti?", pending.data)
        self.assertIn(b"pending review", pending.data)

    def test_activity_summary_shows_24_hour_week_and_year_totals(self):
        reference = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="reply")
            conn.execute(
                "UPDATE comments SET created_at = ?, updated_at = ? WHERE comment_id = 'c1'",
                ("2026-09-07T10:00:00+00:00", "2026-09-07T11:00:00+00:00"),
            )
            db.insert_comment(
                conn, comment_id="week", platform="facebook", video_id="post",
                video_title="Post", author="viewer", text="Hello",
                published_at="", draft_reply="🙏",
            )
            db.update_status(conn, "week", "already_replied", reply_comment_id="manual")
            conn.execute(
                "UPDATE comments SET created_at = ?, updated_at = ? WHERE comment_id = 'week'",
                ("2026-09-03T10:00:00+00:00", "2026-09-03T11:00:00+00:00"),
            )
            activity = db.activity_summary(conn, reference_time=reference)

        self.assertEqual(
            activity,
            [
                {"label": "Last 24 hours", "received": 1, "posted": 1, "already_replied": 0, "handled": 1},
                {"label": "Last 7 days", "received": 2, "posted": 1, "already_replied": 1, "handled": 2},
                {"label": "Last 1 year", "received": 2, "posted": 1, "already_replied": 1, "handled": 2},
            ],
        )
        page = self.client.get("/")
        self.assertIn(b"Last 24 hours", page.data)
        self.assertIn(b"Last 7 days", page.data)
        self.assertIn(b"Last 1 year", page.data)
        self.assertEqual(page.data.count(b'<button class="range-button'), 3)
        self.assertIn(b"Replies posted", page.data)
        self.assertIn(b"Handled total", page.data)

        facebook = self.client.get("/?status=already_replied&platform=facebook")
        self.assertIn(b'class="activity-platform active" href="/?status=already_replied&amp;platform=facebook"', facebook.data)
        self.assertIn(b'<span class="stat-label">already replied</span><span class="stat-value">1</span>', facebook.data)
        self.assertIn(b'data-received="1"', facebook.data)
        self.assertIn(b'data-already="1"', facebook.data)

        youtube = self.client.get("/?status=posted&platform=youtube")
        self.assertIn(b'<span class="stat-label">posted</span><span class="stat-value">1</span>', youtube.data)
        self.assertIn(b'data-posted="1"', youtube.data)

    def test_posted_failed_and_already_replied_tabs(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="r1", error="")
        posted = self.client.get("/?status=posted")
        self.assertIn(b"Posted (IST)", posted.data)
        self.assertIn(b">r1<", posted.data)
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id="c2", platform="facebook", video_id="post",
                video_title="Post", author="bhakta", text="Jai Maa",
                published_at="", draft_reply="🙏",
            )
            db.update_status(conn, "c2", "failed", error="token expired")
            db.insert_comment(
                conn, comment_id="c3", platform="instagram", video_id="media",
                video_title="Reel", author="fan", text="Nice",
                published_at="", draft_reply="",
            )
            db.update_status(conn, "c3", "already_replied", reply_comment_id="manual")
        failed = self.client.get("/?status=failed")
        self.assertIn(b"token expired", failed.data)
        already = self.client.get("/?status=already_replied")
        self.assertIn(b"already_replied", already.data)
        self.assertIn(b">manual<", already.data)

    def test_retry_posts_failed_comment(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "failed", error="token expired")

        def fake_post(**_kwargs):
            with db.connect() as conn:
                db.update_status(conn, "c1", "posted", reply_comment_id="r1", error="")
            return 1

        with patch.object(dashboard, "post_approved", side_effect=fake_post) as send:
            response = self.client.post(
                "/comments/c1/publish?status=failed",
                data={"draft_reply": "Updated"},
            )
        send.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertIn("status=posted", response.headers["Location"])
        with db.connect() as conn:
            self.assertEqual(db.get_comment(conn, "c1")["draft_reply"], "Updated")

    def test_require_port_rejects_occupied_port(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        self.addCleanup(occupied.close)
        busy = occupied.getsockname()[1]
        with self.assertRaisesRegex(RuntimeError, "already in use"):
            dashboard.require_port("127.0.0.1", busy)

    def test_dashboard_hosts_meta_webhook_verification(self):
        with patch.object(config, "META_WEBHOOK_VERIFY_TOKEN", "verify"):
            response = self.client.get(
                "/webhooks/meta?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=321"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "321")

    def test_health_page_and_machine_readable_endpoint(self):
        page = self.client.get("/health")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Gateway is healthy", page.data)
        self.assertEqual(self.client.get("/api/health").json, {"status": "ok"})

    def test_serving_app_starts_webhook_worker(self):
        with patch.object(config, "META_APP_SECRET", "secret"), \
             patch.object(config, "META_WEBHOOK_VERIFY_TOKEN", "verify"), \
             patch.object(dashboard, "start_event_worker") as start:
            serving_app = dashboard.create_serving_app()
        start.assert_called_once_with(serving_app)

    def test_every_tab_trims_video_title_to_50_chars(self):
        long_title = "A" * 80
        with db.connect() as conn:
            for status in dashboard.STATUSES:
                comment_id = f"title-{status}"
                db.insert_comment(
                    conn,
                    comment_id=comment_id,
                    platform="youtube",
                    video_id="vid2",
                    video_title=long_title,
                    author="viewer",
                    text="hello",
                    published_at="",
                    draft_reply="🙏",
                )
                db.update_status(conn, comment_id, status, reply_comment_id="r2", error="")
        for status in dashboard.STATUSES:
            page = self.client.get(f"/?status={status}")
            self.assertIn(b"A" * 50, page.data, status)
            self.assertNotIn(b"A" * 51, page.data, status)

    def test_dashboard_uses_posted_ist_without_updated_column(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="r2", error="")
        page = self.client.get("/?status=posted")
        self.assertIn(b"Posted (IST)", page.data)
        self.assertNotIn(b"<th>Updated</th>", page.data)

    def test_list_comments_converts_timestamps_to_ist(self):
        with db.connect() as conn:
            conn.execute(
                """UPDATE comments SET
                   published_at = '2026-09-07 00:00:00',
                   created_at = '2026-09-07 00:00:00',
                   updated_at = '2026-09-07 01:00:00' WHERE comment_id = 'c1'"""
            )
            row = db.list_comments(conn, status="pending_review")[0]
        self.assertEqual(row["published_at_ist"], "2026-09-07 05:30:00")
        self.assertEqual(row["created_at_ist"], "2026-09-07 05:30:00")
        self.assertEqual(row["posted_at_ist"], "2026-09-07 06:30:00")
        with db.connect() as conn:
            conn.execute(
                "UPDATE comments SET published_at = '2026-07-02T06:58:32+0000' WHERE comment_id = 'c1'"
            )
            row = db.list_comments(conn, status="pending_review")[0]
        self.assertEqual(row["published_at_ist"], "2026-07-02 12:28:32")


if __name__ == "__main__":
    unittest.main()
