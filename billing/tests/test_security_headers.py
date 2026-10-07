from billing import app as billing_app
from billing import config


def _client(monkeypatch, isolated_db, secure: bool):
    monkeypatch.setattr(config, "BILLING_SECRET", "x" * 32)
    monkeypatch.setattr(config, "BILLING_COOKIE_SECURE", secure)
    flask_app = billing_app.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_public_pages_carry_security_headers(isolated_db, monkeypatch):
    client = _client(monkeypatch, isolated_db, secure=True)
    for path in ("/api/health", "/signup", "/admin/login"):
        response = client.get(path)
        assert response.status_code < 400, path
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self';" in csp and "nonce" not in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Strict-Transport-Security"].startswith("max-age=")


def test_insecure_local_billing_sends_no_hsts(isolated_db, monkeypatch):
    client = _client(monkeypatch, isolated_db, secure=False)
    assert "Strict-Transport-Security" not in client.get("/api/health").headers
