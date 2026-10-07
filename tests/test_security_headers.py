import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, render_template_string

from app import config, dashboard, db, security_headers

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


class SecurityHeadersModuleTests(unittest.TestCase):
    def _app(self, **kwargs) -> Flask:
        app = Flask(__name__)
        app.config["TESTING"] = True
        security_headers.install(app, **kwargs)

        @app.get("/page")
        def page():
            return render_template_string('<script nonce="{{ csp_nonce() }}">1</script>')

        @app.get("/custom")
        def custom():
            return "x", 200, {"X-Frame-Options": "SAMEORIGIN"}

        return app

    def test_nonce_in_header_matches_nonce_in_page(self):
        client = self._app(hsts=False, inline_scripts=True).test_client()
        first = client.get("/page")
        second = client.get("/page")
        for response in (first, second):
            csp = response.headers["Content-Security-Policy"]
            nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
            self.assertIn(f'nonce="{nonce}"', response.get_data(as_text=True))
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("form-action 'self'", csp)
            self.assertIn("object-src 'none'", csp)
        self.assertNotEqual(
            first.headers["Content-Security-Policy"],
            second.headers["Content-Security-Policy"],
            "nonce must be fresh per request",
        )

    def test_without_inline_scripts_script_src_is_self_only(self):
        client = self._app(hsts=False, inline_scripts=False).test_client()
        csp = client.get("/page").headers["Content-Security-Policy"]
        self.assertIn("script-src 'self';", csp)
        self.assertNotIn("nonce", csp)

    def test_hsts_only_when_requested(self):
        off = self._app(hsts=False, inline_scripts=True).test_client().get("/page")
        on = self._app(hsts=True, inline_scripts=True).test_client().get("/page")
        self.assertNotIn("Strict-Transport-Security", off.headers)
        self.assertEqual(on.headers["Strict-Transport-Security"], security_headers.HSTS_VALUE)

    def test_blanket_headers_present_and_route_values_win(self):
        client = self._app(hsts=False, inline_scripts=True).test_client()
        response = client.get("/page")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertIn("camera=()", response.headers["Permissions-Policy"])
        self.assertEqual(client.get("/custom").headers["X-Frame-Options"], "SAMEORIGIN")

    def test_every_inline_script_in_templates_carries_the_nonce(self):
        offenders = []
        for template in sorted(TEMPLATES.glob("*.html")):
            for lineno, line in enumerate(template.read_text().splitlines(), 1):
                for tag in re.findall(r"<script\b[^>]*>", line):
                    if "src=" not in tag and "csp_nonce()" not in tag:
                        offenders.append(f"{template.name}:{lineno}: {tag}")
        self.assertEqual(offenders, [], "inline <script> without nonce would be blocked by CSP")


class DashboardSecurityHeadersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for patcher in (
            patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db"),
            patch.object(config, "DATA_DIR", Path(self.temp.name)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_login_page_and_static_file_are_hardened(self):
        with patch.object(config, "DASHBOARD_COOKIE_SECURE", True):
            client = dashboard.create_app().test_client()
        for path in ("/login", "/static/js/page-navigation.js", "/api/health"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertLess(response.status_code, 400)
                self.assertIn("'nonce-", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertEqual(response.headers["Strict-Transport-Security"], security_headers.HSTS_VALUE)

    def test_insecure_local_instance_sends_no_hsts(self):
        with patch.object(config, "DASHBOARD_COOKIE_SECURE", False):
            client = dashboard.create_app().test_client()
        self.assertNotIn("Strict-Transport-Security", client.get("/login").headers)


if __name__ == "__main__":
    unittest.main()
