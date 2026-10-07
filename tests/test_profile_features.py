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
        self.assertIn(b"Send Verification Code", page.data)
        self.assertIn(b'id="email-verification-dialog"', page.data)
        self.assertIn(b'data-open-on-load="true"', page.data)
        self.assertIn(b'<input name="code"', page.data)
        self.assertNotIn(b'<input disabled name="code"', page.data)
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
        # The photo rides along with the ordinary profile save.
        return self.client.post("/profile", data={"csrf_token": "tok", "display_name": "Admin", "email": "",
                                                  "avatar": (io.BytesIO(data), filename)},
                                content_type="multipart/form-data")

    def test_png_upload_is_stored_served_and_shown_in_the_header(self):
        page = self.upload(PNG)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Profile details and photo updated", page.data)
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
        self.assertIn(b"must be a PNG, JPEG or WebP image", page.data)
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

    def test_one_form_saves_everything_and_there_is_no_upload_button(self):
        page = self.client.get("/profile").data.decode()
        self.assertIn('<form method="post" action="/profile" id="profile-form" enctype="multipart/form-data">', page)
        self.assertIn('name="avatar" id="avatar-file"', page)
        self.assertNotIn("Upload photo", page)
        self.assertNotIn("Choose an image", page)
        self.assertNotIn("Account created", page)
        self.assertEqual(page.count('<button type="submit" form="profile-form" class="primary" disabled>Save profile</button>'), 1)
        self.assertLess(page.index('data-back-fallback'), page.index('id="profile-form"'))
        self.assertIn('aria-label="Open My Profile menu"', page)
        # Saving without choosing a file is simply a save -- never an error.
        saved = self.client.post("/profile", data={"csrf_token": "tok", "display_name": "Admin", "email": ""},
                                 content_type="multipart/form-data")
        self.assertEqual(saved.status_code, 200)
        self.assertIn(b"Profile details updated.", saved.data)
        self.assertNotIn(b"Choose an image", saved.data)
        self.assertIsNone(self.user()["avatar_path"])

    def test_bad_photo_does_not_save_the_other_fields(self):
        page = self.upload(b"not an image")
        self.assertEqual(page.status_code, 400)
        self.assertIsNone(self.user()["display_name"])

    def test_old_upload_route_is_gone(self):
        self.assertEqual(self.client.post("/profile/avatar", data={"csrf_token": "tok"}).status_code, 404)

    def test_upload_requires_csrf(self):
        page = self.client.post("/profile", data={"csrf_token": "bad", "display_name": "x", "email": "",
                                                  "avatar": (io.BytesIO(PNG), "me.png")},
                                content_type="multipart/form-data")
        self.assertEqual(page.status_code, 400)
        self.assertIsNone(self.user()["avatar_path"])


if __name__ == "__main__":
    unittest.main()


class ProfileDetailsTests(_ProfileCase):
    def test_details_are_saved_displayed_and_can_be_cleared(self):
        details = {"current_location": "Kolkata, India", "phone_number": "+91 98765 43210",
                   "facebook_page_link": "https://www.facebook.com/example/",
                   "instagram_page_link": "https://www.instagram.com/example/",
                   "youtube_page_link": "https://www.youtube.com/@example",
                   "tiktok_page_link": "https://www.tiktok.com/@example"}
        response = self.client.post('/profile', data={"csrf_token": "tok", **details})
        self.assertEqual(response.status_code, 200)
        for name, value in details.items():
            self.assertEqual(self.user()[name], value)
            self.assertIn(value.encode(), self.client.get('/profile').data)
        self.client.post('/profile', data={"csrf_token": "tok", **dict.fromkeys(details, "")})
        for name in details:
            self.assertIsNone(self.user()[name])

    def test_invalid_link_does_not_save_details(self):
        response = self.client.post('/profile', data={"csrf_token": "tok",
            "current_location": "Kolkata", "instagram_page_link": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.user()['current_location'])

    def test_existing_database_gets_columns_without_losing_users(self):
        with db.connect() as conn:
            for name in ('current_location', 'phone_number', 'facebook_page_link', 'instagram_page_link', 'youtube_page_link', 'tiktok_page_link'):
                conn.execute(f'ALTER TABLE dashboard_users DROP COLUMN {name}')
            conn.commit()
        db.init_db()
        self.assertEqual(self.user()['username'], 'admin')
        self.assertIsNone(self.user()['current_location'])

    def test_all_link_fields_reject_unsafe_values_without_partial_save(self):
        for name in ('facebook_page_link', 'instagram_page_link', 'youtube_page_link', 'tiktok_page_link'):
            with self.subTest(field=name):
                result = self.client.post('/profile', data={'csrf_token': 'tok',
                    'current_location': 'Should not save', name: 'javascript:alert(1)'})
                self.assertEqual(result.status_code, 400)
                self.assertIsNone(self.user()[name])
                self.assertIsNone(self.user()['current_location'])

    def test_detail_limits_and_csrf_protect_saved_profile(self):
        for name, limit in [('current_location',150), ('phone_number',40),
                            ('facebook_page_link',2048), ('instagram_page_link',2048),
                            ('youtube_page_link',2048), ('tiktok_page_link',2048)]:
            with self.subTest(field=name):
                result = self.client.post('/profile', data={'csrf_token':'tok', name:'x' * (limit + 1)})
                self.assertEqual(result.status_code,400)
                self.assertIsNone(self.user()[name])
        result = self.client.post('/profile', data={'csrf_token':'wrong','current_location':'Unauthorized'})
        self.assertEqual(result.status_code,400)
        self.assertIsNone(self.user()['current_location'])

    def test_verification_then_profile_save_uses_correct_endpoint(self):
        self.save_email('new@example.com')
        result = self.client.post('/profile/email/verify', data={'csrf_token':'tok','code':self.code_from_mail()})
        self.assertEqual(result.status_code,200)
        self.assertIn(b'action="/profile" id="profile-form"',result.data)
        result = self.client.post('/profile', data={'csrf_token':'tok','email':'new@example.com',
            'current_location':'Kolkata','facebook_page_link':'https://facebook.com/example'})
        self.assertEqual(result.status_code,200)
        self.assertIsNotNone(self.user()['email_verified_at'])
        self.assertEqual(self.user()['current_location'],'Kolkata')
        self.assertEqual(len(self.sent),1)
