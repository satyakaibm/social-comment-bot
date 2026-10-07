"""Profile email verification (code by email, green tick) and profile photo."""

import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash

from app import config, dashboard, db, mailer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64


class _ProfileCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, attr, value in (
            (db, "DB_PATH", Path(self.temp.name) / "comments.db"),
            (config, "DATA_DIR", Path(self.temp.name)),
            (config, "AVATAR_DIR", Path(self.temp.name) / "avatars"),
            (config, "DASHBOARD_USERNAME", "admin"),
            (config, "DASHBOARD_PASSWORD_HASH", generate_password_hash("secret")),
            (config, "SMTP_HOST", "smtp.example.test"),
            (config, "MAIL_FROM", "portal@example.test"),
        ):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._login_failures.clear()
        db.init_db()
        self.client = dashboard.create_app().test_client()
        with self.client.session_transaction() as s:
            s["dashboard_authenticated"] = True
            s["dashboard_auth_version"] = 1
            s["dashboard_username"] = "admin"
            s["csrf_token"] = "tok"
        self.sent = []
        sender = patch.object(mailer, "send", side_effect=lambda **kw: self.sent.append(kw))
        sender.start()
        self.addCleanup(sender.stop)

    def user(self):
        with db.connect() as conn:
            return db.get_dashboard_user(conn, "admin")

    def save_email(self, email):
        return self.client.post("/profile", data={"csrf_token": "tok", "display_name": "Admin", "email": email})

    def code_from_mail(self):
        body = self.sent[-1]["body"]
        return body.split("code is: ")[1][:6]


class EmailVerificationTests(_ProfileCase):
    def test_new_email_is_unverified_and_gets_a_code(self):
        page = self.save_email("admin@example.com")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"We sent a 6-digit code to admin@example.com", page.data)
        self.assertIn(b"Not verified", page.data)
        self.assertNotIn(b">Verified<", page.data)
        self.assertEqual(self.sent[-1]["to"], "admin@example.com")
        self.assertRegex(self.code_from_mail(), r"^\d{6}$")
        user = self.user()
        self.assertIsNone(user["email_verified_at"])
        self.assertEqual(user["email_code_target"], "admin@example.com")
        self.assertNotIn(self.code_from_mail(), user["email_code_hash"])  # stored hashed

    def test_page_says_where_and_when_the_code_went_and_counts_down_resend(self):
        page = self.save_email("admin@example.com").data.decode()
        self.assertIn("Code sent to <strong>admin@example.com</strong> at ", page)
        self.assertIn(" IST on ", page)
        self.assertIn("Check the Spam folder", page)
        self.assertRegex(page, r'id="resend-code" disabled data-wait="\d+">Resend in \d+s</button>')

    def test_send_is_logged_with_recipient_and_host(self):
        with self.assertLogs("app.dashboard", level="INFO") as logs:
            self.save_email("admin@example.com")
        self.assertTrue(any("Verification code sent for admin to admin@example.com" in line for line in logs.output), logs.output)

    def test_correct_code_verifies_and_shows_the_green_tick(self):
        self.save_email("admin@example.com")
        page = self.client.post("/profile/email/verify", data={"csrf_token": "tok", "code": self.code_from_mail()})
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Email address verified", page.data)
        self.assertIn(b'class="badge verified"', page.data)
        self.assertIn(b"&#10003;", page.data)
        self.assertIsNotNone(self.user()["email_verified_at"])
        self.assertIsNone(self.user()["email_code_hash"])

    def test_wrong_code_counts_down_then_voids_the_code(self):
        self.save_email("admin@example.com")
        for left in (4, 3, 2, 1):
            page = self.client.post("/profile/email/verify", data={"csrf_token": "tok", "code": "000000"})
            self.assertEqual(page.status_code, 400)
            self.assertIn(f"{left} attempt(s) left".encode(), page.data)
        page = self.client.post("/profile/email/verify", data={"csrf_token": "tok", "code": "000000"})
        self.assertIn(b"Too many incorrect codes", page.data)
        self.assertIsNone(self.user()["email_code_hash"])
        # The real code is dead now too.
        page = self.client.post("/profile/email/verify", data={"csrf_token": "tok", "code": self.code_from_mail()})
        self.assertIn(b"No code is pending", page.data)

    def test_expired_code_is_rejected(self):
        self.save_email("admin@example.com")
        with db.connect() as conn:
            conn.execute("UPDATE dashboard_users SET email_code_expires_at = ?",
                         ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
        page = self.client.post("/profile/email/verify", data={"csrf_token": "tok", "code": self.code_from_mail()})
        self.assertEqual(page.status_code, 400)
        self.assertIn(b"No code is pending", page.data)

    def test_resend_is_throttled_to_once_a_minute(self):
        self.save_email("admin@example.com")
        page = self.client.post("/profile/email/send-code", data={"csrf_token": "tok"})
        self.assertEqual(page.status_code, 400)
        self.assertIn(b"A code was sent moments ago", page.data)
        self.assertEqual(len(self.sent), 1)
        with db.connect() as conn:
            conn.execute("UPDATE dashboard_users SET email_code_sent_at = ?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(),))
        page = self.client.post("/profile/email/send-code", data={"csrf_token": "tok"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(self.sent), 2)

    def test_changing_the_email_drops_verification(self):
        self.save_email("admin@example.com")
        self.client.post("/profile/email/verify", data={"csrf_token": "tok", "code": self.code_from_mail()})
        self.assertIsNotNone(self.user()["email_verified_at"])
        # Same address again (different case): still verified, no new mail.
        sent_before = len(self.sent)
        self.save_email("Admin@Example.com")
        self.assertIsNotNone(self.user()["email_verified_at"])
        self.assertEqual(len(self.sent), sent_before)
        # A different address: verification gone, new code sent.
        page = self.save_email("new@example.com")
        self.assertIsNone(self.user()["email_verified_at"])
        self.assertIn(b"Not verified", page.data)
        self.assertEqual(self.sent[-1]["to"], "new@example.com")

    def test_without_smtp_the_page_explains_instead_of_failing(self):
        with patch.object(config, "SMTP_HOST", ""):
            page = self.save_email("admin@example.com")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Profile details updated", page.data)
        self.assertIn(b"not set up on this portal yet", page.data)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.user()["email"], "admin@example.com")

    def test_mail_failure_is_reported_not_raised(self):
        with patch.object(mailer, "send", side_effect=mailer.MailError("relay down")):
            page = self.save_email("admin@example.com")
        self.assertIn(b"could not be sent", page.data)
        self.assertIsNone(self.user()["email_code_hash"])

    def test_verify_requires_csrf(self):
        self.save_email("admin@example.com")
        page = self.client.post("/profile/email/verify", data={"csrf_token": "bad", "code": self.code_from_mail()})
        self.assertEqual(page.status_code, 400)
        self.assertIsNone(self.user()["email_verified_at"])


class NoRealEmailGuardTests(_ProfileCase):
    def test_suite_wide_stub_blocks_smtp_even_with_credentials_configured(self):
        import smtplib
        from unittest.mock import patch as _patch
        self.addCleanup(_patch.stopall)
        for attr in ("SMTP", "SMTP_SSL"):
            _patch.object(smtplib, attr, side_effect=AssertionError("real SMTP reached")).start()
        with _patch.object(config, "SMTP_HOST", "smtp.gmail.com"), _patch.object(config, "MAIL_FROM", "x@gmail.com"):
            page = self.save_email("admin@example.com")
        self.assertIn(b"We sent a 6-digit code", page.data)


class AvatarTests(_ProfileCase):
    def upload(self, data, filename="me.png"):
        return self.client.post("/profile/avatar", data={"csrf_token": "tok", "avatar": (io.BytesIO(data), filename)},
                                content_type="multipart/form-data")

    def test_png_upload_is_stored_served_and_shown_in_the_header(self):
        page = self.upload(PNG)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Profile photo updated", page.data)
        user = self.user()
        self.assertEqual(user["avatar_path"], f"{user['id']}.png")
        self.assertTrue((config.AVATAR_DIR / user["avatar_path"]).is_file())
        served = self.client.get(f"/profile/avatar/admin?v={user['updated_at']}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "image/png")
        self.assertEqual(served.data, PNG)
        self.assertIn("private", served.headers["Cache-Control"])
        header = self.client.get("/").data
        self.assertIn(b'<img class="profile-photo" src="/profile/avatar/admin?v=', header)
        self.assertNotIn(b'title="My Profile">A</summary>', header)
        self.assertIn(b'<img src="/profile/avatar/admin?v=', self.client.get("/profile").data)

    def test_format_is_sniffed_not_trusted_from_the_filename(self):
        page = self.upload(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", filename="logo.png")
        self.assertEqual(page.status_code, 400)
        self.assertIn(b"Use a PNG, JPEG or WebP image", page.data)
        self.assertIsNone(self.user()["avatar_path"])
        self.assertEqual(self.upload(JPEG, filename="whatever.bin").status_code, 200)
        self.assertEqual(self.user()["avatar_path"].rsplit(".", 1)[1], "jpg")
        self.assertEqual(self.upload(WEBP, filename="x").status_code, 200)
        self.assertEqual(self.user()["avatar_path"].rsplit(".", 1)[1], "webp")
        # Replacing the format leaves exactly one file on disk.
        self.assertEqual(len(list(config.AVATAR_DIR.iterdir())), 1)

    def test_oversized_upload_is_rejected(self):
        page = self.upload(PNG + b"\x00" * config.AVATAR_MAX_BYTES)
        self.assertEqual(page.status_code, 400)
        self.assertIn(b"1 MB or smaller", page.data)
        self.assertIsNone(self.user()["avatar_path"])

    def test_remove_deletes_the_file_and_restores_the_initial(self):
        self.upload(PNG)
        path = config.AVATAR_DIR / self.user()["avatar_path"]
        page = self.client.post("/profile/avatar/remove", data={"csrf_token": "tok"})
        self.assertIn(b"Profile photo removed", page.data)
        self.assertFalse(path.exists())
        self.assertIsNone(self.user()["avatar_path"])
        self.assertIn(b'title="My Profile">A</summary>', self.client.get("/").data)
        self.assertEqual(self.client.get("/profile/avatar/admin").status_code, 404)

    def test_avatar_route_needs_a_signed_in_user(self):
        self.upload(PNG)
        anonymous = dashboard.create_app().test_client()
        response = anonymous.get("/profile/avatar/admin")
        self.assertIn(response.status_code, (302, 401, 403))

    def test_upload_button_is_secondary_and_inert_until_a_file_is_chosen(self):
        page = self.client.get("/profile").data.decode()
        self.assertIn('id="avatar-upload" disabled>Upload photo</button>', page)
        self.assertIn('class="small ghost" type="submit" id="avatar-upload"', page)
        # Exactly one primary (orange) submit on the page: Save profile, which
        # sits in the card footer beside BACK yet submits the profile form.
        self.assertEqual(page.count('<button type="submit" form="profile-form" class="primary">Save profile</button>'), 1)
        self.assertIn('<form method="post" id="profile-form">', page)
        footer = page[page.index('class="footer-actions"'):]
        self.assertLess(footer.index('href="/">BACK</a>'), footer.index('Save profile'))

    def test_upload_requires_csrf(self):
        page = self.client.post("/profile/avatar", data={"csrf_token": "bad", "avatar": (io.BytesIO(PNG), "me.png")},
                                content_type="multipart/form-data")
        self.assertEqual(page.status_code, 400)
        self.assertIsNone(self.user()["avatar_path"])


if __name__ == "__main__":
    unittest.main()
