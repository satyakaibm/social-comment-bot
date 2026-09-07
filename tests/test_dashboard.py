import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, dashboard


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

    def test_default_view_hides_pending_and_approved_tabs(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"status=posted", page.data)
        self.assertIn(b"already replied", page.data)
        self.assertNotIn(b"status=pending_review", page.data)
        self.assertNotIn(b"status=approved", page.data)
        self.assertNotIn(b"What time is aarti?", page.data)
        remapped = self.client.get("/?status=pending_review")
        self.assertNotIn(b"What time is aarti?", remapped.data)
        self.assertNotIn(b"pending review", remapped.data)

    def test_posted_failed_and_already_replied_tabs(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="r1", error="")
        posted = self.client.get("/?status=posted")
        self.assertIn(b"posted_at_ist", posted.data)
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

    def test_pick_free_port_skips_occupied_port(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        self.addCleanup(occupied.close)
        busy = occupied.getsockname()[1]
        chosen = dashboard.pick_free_port("127.0.0.1", busy, attempts=5)
        self.assertNotEqual(chosen, busy)
        self.assertGreater(chosen, busy)

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

    def test_posted_tab_hides_updated_at(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="r2", error="")
        posted = self.client.get("/?status=posted")
        self.assertIn(b"created_at_ist", posted.data)
        self.assertNotIn(b"<th>updated_at</th>", posted.data)

    def test_list_comments_converts_timestamps_to_ist(self):
        with db.connect() as conn:
            conn.execute(
                """UPDATE comments SET created_at = '2026-09-07 00:00:00',
                   updated_at = '2026-09-07 01:00:00' WHERE comment_id = 'c1'"""
            )
            row = db.list_comments(conn, status="pending_review")[0]
        self.assertEqual(row["created_at_ist"], "2026-09-07 05:30:00")
        self.assertEqual(row["posted_at_ist"], "2026-09-07 06:30:00")


if __name__ == "__main__":
    unittest.main()
