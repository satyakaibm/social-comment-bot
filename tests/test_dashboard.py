import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash

from app import config, db, dashboard


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        username = patch.object(config, "DASHBOARD_USERNAME", "admin")
        password = patch.object(
            config, "DASHBOARD_PASSWORD_HASH", generate_password_hash("secret")
        )
        username.start()
        password.start()
        self.addCleanup(username.stop)
        self.addCleanup(password.stop)
        dashboard._login_failures.clear()
        db.init_db()
        self.app = dashboard.create_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as auth_session:
            auth_session["dashboard_authenticated"] = True
            auth_session["dashboard_auth_version"] = 1
            auth_session["dashboard_username"] = "admin"
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
        self.assertIn(b'<header class="site-header">', page.data)
        self.assertIn(b'<div class="topbar">', page.data)
        self.assertIn(b'class="service-nav"', page.data)
        self.assertIn(b"Social Comment Studio", page.data)
        self.assertIn(b"Gateway Health", page.data)
        self.assertNotIn(b"Gateway operational", page.data)
        self.assertIn(b'<footer class="site-footer">', page.data)
        self.assertIn(b"Capture. Curate. Publish.", page.data)
        self.assertIn(b"Gateway health", page.data)
        self.assertIn(b'id="auto-refresh"', page.data)
        self.assertIn(b'aria-label="Enable auto refresh"', page.data)
        self.assertIn(b"comment-dashboard-auto-refresh", page.data)
        self.assertNotIn(b">Auto refresh</button>", page.data)
        self.assertNotIn(b"All timestamps shown in IST", page.data)
        self.assertIn(b"Community workspace", page.data)
        self.assertNotIn(
            b"Review conversations and monitor automated replies", page.data
        )
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
                {"label": "1 Hour", "received": 0, "posted": 1, "already_replied": 0, "handled": 1},
                {"label": "24 Hours", "received": 1, "posted": 1, "already_replied": 0, "handled": 1},
                {"label": "7 Day", "received": 2, "posted": 1, "already_replied": 1, "handled": 2},
                {"label": "365 Days", "received": 2, "posted": 1, "already_replied": 1, "handled": 2},
            ],
        )
        page = self.client.get("/")
        self.assertIn(b"1 Hour", page.data)
        self.assertIn(b"24 Hours", page.data)
        self.assertIn(b"7 Day", page.data)
        self.assertIn(b"365 Days", page.data)
        self.assertEqual(page.data.count(b'<button class="range-button'), 4)
        self.assertIn(b"Replies posted", page.data)
        self.assertIn(b"Handled total", page.data)

        facebook = self.client.get("/?status=already_replied&platform=facebook")
        self.assertIn(b'<body class="theme-facebook">', facebook.data)
        self.assertIn(b'class="activity-platform active" href="/?status=already_replied&amp;platform=facebook"', facebook.data)
        self.assertIn(b'<span class="stat-label">already replied</span><span class="stat-value">1</span>', facebook.data)
        self.assertIn(b'data-received="1"', facebook.data)
        self.assertIn(b'data-already="1"', facebook.data)

        youtube = self.client.get("/?status=posted&platform=youtube")
        self.assertIn(b'<body class="theme-youtube">', youtube.data)
        self.assertIn(b'<span class="stat-label">posted</span><span class="stat-value">1</span>', youtube.data)
        self.assertIn(b'data-posted="1"', youtube.data)

        instagram = self.client.get("/?status=posted&platform=instagram")
        self.assertIn(b'<body class="theme-instagram">', instagram.data)

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

    def test_comments_are_sorted_by_posted_time_newest_first(self):
        with db.connect() as conn:
            db.insert_comment(
                conn, comment_id="newest", platform="facebook", video_id="post",
                video_title="Post", author="new author", text="New activity",
                published_at="", draft_reply="🙏",
            )
            db.update_status(conn, "c1", "posted", reply_comment_id="old-reply")
            db.update_status(conn, "newest", "posted", reply_comment_id="new-reply")
            conn.execute(
                "UPDATE comments SET updated_at = ? WHERE comment_id = ?",
                ("2026-09-07T10:00:00+00:00", "c1"),
            )
            conn.execute(
                "UPDATE comments SET updated_at = ? WHERE comment_id = ?",
                ("2026-09-07T11:00:00+00:00", "newest"),
            )
            rows = db.list_comments(conn, status="posted")

        self.assertEqual([row["comment_id"] for row in rows], ["newest", "c1"])

    def test_posted_column_sort_button_toggles_order(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="reply")
        descending = self.client.get("/?status=posted")
        self.assertIn(b'class="sort-button"', descending.data)
        self.assertIn(b'sort=asc', descending.data)
        self.assertIn("↓".encode(), descending.data)

        ascending = self.client.get("/?status=posted&sort=asc")
        self.assertIn(b'sort=desc', ascending.data)
        self.assertIn("↑".encode(), ascending.data)

    def test_service_navigation_is_below_community_banner(self):
        page = self.client.get("/")
        markup = page.data.decode()
        banner_end = markup.index("</section>", markup.index('class="intro"'))
        self.assertLess(banner_end, markup.index('class="service-nav"'))
        self.assertIn("Gateway Health", markup)
        self.assertNotIn('class="service-link"', markup)

    def test_comment_table_paginates_in_batches_of_one_hundred(self):
        with db.connect() as conn:
            db.update_status(conn, "c1", "posted", reply_comment_id="reply-c1")
            for index in range(100):
                comment_id = f"page-comment-{index:03d}"
                db.insert_comment(
                    conn,
                    comment_id=comment_id,
                    platform="youtube",
                    video_id="video",
                    video_title="Title",
                    author="viewer",
                    text=f"Comment {index}",
                    published_at="",
                    draft_reply="🙏",
                )
                db.update_status(
                    conn, comment_id, "posted", reply_comment_id=f"reply-{index}"
                )

        first_page = self.client.get("/?status=posted")
        self.assertIn(b"Page 1 of 2", first_page.data)
        self.assertIn(b"Next", first_page.data)
        self.assertEqual(first_page.data.count(b"<tbody>") , 1)
        self.assertEqual(first_page.data.count(b"<tr>"), 101)

        second_page = self.client.get("/?status=posted&page=2")
        self.assertIn(b"Page 2 of 2", second_page.data)
        self.assertIn(b"Previous", second_page.data)
        self.assertEqual(second_page.data.count(b"<tr>"), 2)

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
        self.assertIn(b'aria-label="Back to Comment Dashboard"', page.data)
        self.assertIn(b"event.key === 'Backspace'", page.data)
        self.assertIn(b"window.location.assign('/')", page.data)
        self.assertEqual(self.client.get("/api/health").json, {"status": "ok"})

    def test_dashboard_requires_login_and_accepts_valid_credentials(self):
        client = self.app.test_client()
        redirect_response = client.get("/?status=posted")
        self.assertEqual(redirect_response.status_code, 302)
        self.assertIn("/login?next=", redirect_response.headers["Location"])

        login_page = client.get("/login?next=/?status=posted")
        self.assertIn(b"Welcome back", login_page.data)
        self.assertIn(b"Create an account", login_page.data)
        with client.session_transaction() as login_session:
            csrf_token = login_session["csrf_token"]
        invalid = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "wrong",
                "csrf_token": csrf_token,
            },
        )
        self.assertEqual(invalid.status_code, 401)
        valid = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "secret",
                "csrf_token": csrf_token,
                "next": "/?status=posted",
            },
        )
        self.assertEqual(valid.status_code, 302)
        self.assertTrue(valid.headers["Location"].endswith("/?status=posted"))
        self.assertEqual(client.get("/?status=posted").status_code, 200)

    def test_signup_creates_unique_user_and_redirects_to_login(self):
        client = self.app.test_client()
        signup_page = client.get("/signup")
        self.assertEqual(signup_page.status_code, 200)
        self.assertIn(b"Create account", signup_page.data)
        self.assertIn(b'minlength="8"', signup_page.data)
        self.assertIn(
            b"one uppercase letter, one number, and one special character",
            signup_page.data,
        )
        with client.session_transaction() as signup_session:
            token = signup_session["csrf_token"]
        created = client.post(
            "/signup",
            data={
                "username": "new.user",
                "password": "Fresh1!x",
                "confirm_password": "Fresh1!x",
                "csrf_token": token,
            },
        )
        self.assertEqual(created.status_code, 302)
        self.assertIn("/login?registered=1", created.headers["Location"])

        duplicate = client.post(
            "/signup",
            data={
                "username": "NEW.USER",
                "password": "Fresh1!x",
                "confirm_password": "Fresh1!x",
                "csrf_token": token,
            },
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn(b"already registered", duplicate.data)

        login_page = client.get("/login?registered=1")
        self.assertIn(b"Account created", login_page.data)
        with client.session_transaction() as login_session:
            login_token = login_session["csrf_token"]
        logged_in = client.post(
            "/login",
            data={
                "username": "new.user",
                "password": "Fresh1!x",
                "csrf_token": login_token,
            },
        )
        self.assertEqual(logged_in.status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)

    def test_machine_health_and_meta_webhook_remain_public(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/api/health").status_code, 200)
        with patch.object(config, "META_WEBHOOK_VERIFY_TOKEN", "verify"):
            response = client.get(
                "/webhooks/meta?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=321"
            )
        self.assertEqual(response.status_code, 200)

    def test_logout_requires_csrf_and_ends_session(self):
        with self.client.session_transaction() as auth_session:
            auth_session["csrf_token"] = "token"
        self.assertEqual(self.client.post("/logout").status_code, 400)
        response = self.client.post("/logout", data={"csrf_token": "token"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_profile_menu_resets_password_and_invalidates_sessions(self):
        page = self.client.get("/")
        self.assertIn(b'aria-label="Open My Profile menu"', page.data)
        self.assertIn(b'title="My Profile">A</summary>', page.data)
        self.assertIn(b"My Profile", page.data)
        self.assertIn(b'class="profile-menu-user">admin</span>', page.data)
        self.assertIn(b"Reset password", page.data)
        self.assertIn(b"Log out", page.data)

        profile_page = self.client.get("/profile")
        self.assertEqual(profile_page.status_code, 200)
        self.assertIn(b"Manage your portal account details", profile_page.data)
        self.assertIn(b'value="admin" readonly', profile_page.data)
        self.assertIn(b'<a class="back" href="/">BACK</a>', profile_page.data)
        self.assertNotIn(b"Back to dashboard", profile_page.data)
        with self.client.session_transaction() as auth_session:
            auth_session["csrf_token"] = "profile-token"
        updated_profile = self.client.post(
            "/profile",
            data={
                "csrf_token": "profile-token",
                "display_name": "Portal Admin",
                "email": "admin@example.com",
            },
        )
        self.assertEqual(updated_profile.status_code, 200)
        self.assertIn(b"Profile details updated", updated_profile.data)
        with db.connect() as conn:
            user = db.get_dashboard_user(conn, "admin")
        self.assertEqual(user["display_name"], "Portal Admin")
        self.assertEqual(user["email"], "admin@example.com")

        with db.connect() as conn:
            db.create_dashboard_user(
                conn, "second-user", generate_password_hash("Second1!")
            )
            self.assertTrue(
                db.update_dashboard_user_profile(
                    conn,
                    "second-user",
                    display_name="Second User",
                    email="second@example.com",
                )
            )
        duplicate_email = self.client.post(
            "/profile",
            data={
                "csrf_token": "profile-token",
                "display_name": "Portal Admin",
                "email": "SECOND@example.com",
            },
        )
        self.assertEqual(duplicate_email.status_code, 409)
        self.assertIn(b"already registered to another account", duplicate_email.data)
        self.assertIn(b'value="SECOND@example.com"', duplicate_email.data)
        with db.connect() as conn:
            self.assertEqual(
                db.get_dashboard_user(conn, "admin")["email"], "admin@example.com"
            )

        reset_page = self.client.get("/profile/password")
        self.assertIn(b'minlength="8"', reset_page.data)
        self.assertIn(b"one uppercase letter, one number, and one special character", reset_page.data)

        with self.client.session_transaction() as auth_session:
            auth_session["csrf_token"] = "weak-token"
        weak = self.client.post(
            "/profile/password",
            data={
                "csrf_token": "weak-token",
                "current_password": "secret",
                "new_password": "lowercase1",
                "confirm_password": "lowercase1",
            },
        )
        self.assertEqual(weak.status_code, 400)
        self.assertIn(b"one uppercase letter, one number, and one special character", weak.data)

        other_client = self.app.test_client()
        with other_client.session_transaction() as other_session:
            other_session["dashboard_authenticated"] = True
            other_session["dashboard_auth_version"] = 1
            other_session["dashboard_username"] = "admin"
        with self.client.session_transaction() as auth_session:
            auth_session["csrf_token"] = "reset-token"
        response = self.client.post(
            "/profile/password",
            data={
                "csrf_token": "reset-token",
                "current_password": "secret",
                "new_password": "New-secret1",
                "confirm_password": "New-secret1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("password_changed=1", response.headers["Location"])
        self.assertEqual(other_client.get("/").status_code, 302)

        login_page = self.client.get("/login")
        with self.client.session_transaction() as login_session:
            token = login_session["csrf_token"]
        logged_in = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "New-secret1",
                "csrf_token": token,
            },
        )
        self.assertEqual(logged_in.status_code, 302)

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
