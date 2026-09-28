import pytest

from billing import config, db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point SQLite at a throwaway file so tests never touch billing/data/billing.db."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "billing.db")
    monkeypatch.setattr(config, "TENANTS_DIR", tmp_path / "tenants")
    db.init_db()
    return db
