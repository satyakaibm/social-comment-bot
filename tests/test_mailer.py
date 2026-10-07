"""The SMTP path itself, against an in-process fake server: no credentials,
no network, but the real smtplib conversation (EHLO, optional STARTTLS
refused, AUTH, DATA)."""

import socket
import threading
import unittest
from unittest.mock import patch

from app import config, mailer


class _FakeSMTP(threading.Thread):
    """Minimal SMTP server: accepts one session, records what arrives."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.commands: list[str] = []
        self.data = b""

    def run(self):
        conn, _ = self.sock.accept()
        f = conn.makefile("rb")
        conn.sendall(b"220 fake ESMTP\r\n")
        while True:
            line = f.readline()
            if not line:
                break
            cmd = line.decode(errors="replace").strip()
            self.commands.append(cmd)
            verb = cmd.split(" ", 1)[0].upper()
            if verb == "EHLO":
                conn.sendall(b"250-fake\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
            elif verb == "AUTH":
                conn.sendall(b"235 ok\r\n")
            elif verb == "DATA":
                conn.sendall(b"354 go\r\n")
                buf = b""
                while not buf.endswith(b"\r\n.\r\n"):
                    buf += f.readline()
                self.data = buf
                conn.sendall(b"250 queued\r\n")
            elif verb == "QUIT":
                conn.sendall(b"221 bye\r\n")
                break
            else:
                conn.sendall(b"250 ok\r\n")
        conn.close()
        self.sock.close()


class MailerTests(unittest.TestCase):
    def test_sends_through_a_real_smtp_conversation(self):
        server = _FakeSMTP(); server.start()
        with patch.multiple(config, SMTP_HOST="127.0.0.1", SMTP_PORT=server.port, SMTP_USE_TLS=False,
                            SMTP_USE_SSL=False, SMTP_USERNAME="user", SMTP_PASSWORD="pw",
                            MAIL_FROM="portal@example.test"):
            mailer.send(to="person@example.test", subject="Your verification code", body="code is: 123456\n")
        server.join(timeout=5)
        verbs = [c.split(" ", 1)[0].upper() for c in server.commands]
        self.assertIn("EHLO", verbs)
        self.assertIn("AUTH", verbs)
        self.assertIn("MAIL", verbs)
        self.assertIn("RCPT", verbs)
        self.assertIn("DATA", verbs)
        self.assertIn(b"To: person@example.test", server.data)
        self.assertIn(b"Subject: Your verification code", server.data)
        self.assertIn(b"code is: 123456", server.data)

    def test_flask_mail_names_are_accepted_and_display_name_gets_the_mailbox(self):
        import importlib
        from app import config as cfg
        env = {
            "MAIL_SERVER": "smtp.gmail.com", "MAIL_PORT": "587",
            "MAIL_USERNAME": "contentmytrip@gmail.com", "MAIL_PASSWORD": "app-pw",
            "MAIL_FROM": "Content My Trip", "MAIL_USE_TLS": "true", "MAIL_USE_SSL": "false",
        }
        # Empty strings rather than pops: config re-reads .env on reload and a
        # popped key would quietly come back from the developer's own file.
        env.update({name: "" for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
                                          "SMTP_PASSWORD", "SMTP_USE_TLS", "SMTP_USE_SSL")})
        with patch.dict("os.environ", env, clear=False):
            importlib.reload(cfg)
            try:
                self.assertEqual(cfg.SMTP_HOST, "smtp.gmail.com")
                self.assertEqual(cfg.SMTP_PORT, 587)
                self.assertEqual(cfg.SMTP_USERNAME, "contentmytrip@gmail.com")
                self.assertEqual(cfg.SMTP_PASSWORD, "app-pw")
                self.assertTrue(cfg.SMTP_USE_TLS)
                self.assertFalse(cfg.SMTP_USE_SSL)
                self.assertEqual(cfg.MAIL_FROM, "Content My Trip <contentmytrip@gmail.com>")
                self.assertTrue(mailer.configured())
            finally:
                pass
        importlib.reload(cfg)

    def test_mail_names_are_canonical_and_win_over_the_smtp_aliases(self):
        import importlib
        from app import config as cfg
        with patch.dict("os.environ", {"SMTP_HOST": "relay.example.test", "MAIL_SERVER": "smtp.gmail.com",
                                       "MAIL_FROM": "portal@example.test"}):
            importlib.reload(cfg)
            self.assertEqual(cfg.SMTP_HOST, "smtp.gmail.com")
            self.assertEqual(cfg.MAIL_FROM, "portal@example.test")
        # SMTP_* alone still works for anyone who configured it that way.
        with patch.dict("os.environ", {"SMTP_HOST": "relay.example.test", "MAIL_FROM": "portal@example.test",
                                       "MAIL_SERVER": ""}):
            importlib.reload(cfg)
            self.assertEqual(cfg.SMTP_HOST, "relay.example.test")
        importlib.reload(cfg)

    def test_unconfigured_is_a_clear_error_not_a_connection_attempt(self):
        with patch.multiple(config, SMTP_HOST="", MAIL_FROM=""):
            self.assertFalse(mailer.configured())
            with self.assertRaises(mailer.MailError) as caught:
                mailer.send(to="a@b.c", subject="s", body="b")
        self.assertIn("SMTP_HOST", str(caught.exception))

    def test_connection_failure_becomes_mail_error(self):
        dead = socket.socket(); dead.bind(("127.0.0.1", 0)); port = dead.getsockname()[1]; dead.close()
        with patch.multiple(config, SMTP_HOST="127.0.0.1", SMTP_PORT=port, SMTP_USE_TLS=False,
                            SMTP_USE_SSL=False, MAIL_FROM="portal@example.test", SMTP_TIMEOUT_SECONDS=2):
            with self.assertRaises(mailer.MailError):
                mailer.send(to="a@b.c", subject="s", body="b")


if __name__ == "__main__":
    unittest.main()
