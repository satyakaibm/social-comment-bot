import pytest

from app import config, db, mailer


@pytest.fixture(autouse=True)
def no_real_email(request, monkeypatch):
    """Nothing in the suite may send real email, ever.

    app.mailer reads SMTP settings from the developer's own .env, so once
    that file holds real credentials any test that saves a profile email
    would push a genuine verification code through the real mailbox to a
    placeholder address like admin@example.com -- which happened, and
    bounced. Every test gets a recording stub instead; the one module that
    tests the SMTP conversation itself opts out with the `real_mail` marker
    and talks to an in-process fake server.
    """
    if request.node.get_closest_marker("real_mail"):
        yield None
        return
    sent = []
    monkeypatch.setattr(mailer, "send", lambda **kwargs: sent.append(kwargs))
    yield sent


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point SQLite at a throwaway file so tests never touch data/comments.db."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "comments.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    return db
