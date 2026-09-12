import pytest

from app import config, db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point SQLite at a throwaway file so tests never touch data/comments.db."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "comments.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    return db
