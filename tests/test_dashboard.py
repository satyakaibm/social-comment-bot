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

    def test_pending_list_and_approve(self):
        page = self.client.get("/?status=pending_review")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"What time is aarti?", page.data)
        self.assertIn(b"pending review", page.data)
        response = self.client.post(
            "/comments/c1/approve?status=pending_review",
            data={"draft_reply": "Aarti is at 6pm."},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Approved", response.data)
        with db.connect() as conn:
            row = db.get_comment(conn, "c1")
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["draft_reply"], "Aarti is at 6pm.")

    def test_posted_failed_and_already_replied_tabs(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="r1", error="")
        posted = self.client.get("/?status=posted")
        self.assertIn(b"Reply id: r1", posted.data)
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
        self.assertIn(b"already replied", already.data)
        self.assertIn(b"Reply id: manual", already.data)

    def test_publish_posts_pending_comment(self):
        def fake_post(**_kwargs):
            with db.connect() as conn:
                db.update_status(conn, "c1", "posted", reply_comment_id="r1", error="")
            return 1

        with patch.object(dashboard, "post_approved", side_effect=fake_post) as send:
            response = self.client.post(
                "/comments/c1/publish?status=pending_review",
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


if __name__ == "__main__":
    unittest.main()
