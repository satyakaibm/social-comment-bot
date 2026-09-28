import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from billing import config

STATUSES = ("created", "pending_provisioning", "active", "inactive")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    razorpay_customer_id TEXT,
    razorpay_subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    compose_project TEXT NOT NULL,
    env_path TEXT NOT NULL,
    port INTEGER NOT NULL,
    provisioned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)


def create_tenant(
    conn: sqlite3.Connection,
    *,
    tenant_key: str,
    name: str,
    email: str,
    port: int,
    razorpay_customer_id: str = "",
    razorpay_subscription_id: str = "",
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO tenants (
            tenant_key, name, email, razorpay_customer_id,
            razorpay_subscription_id, status, compose_project, env_path,
            port, provisioned, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, 0, ?, ?)
        """,
        (
            tenant_key,
            name,
            email,
            razorpay_customer_id,
            razorpay_subscription_id,
            tenant_key,
            str(config.TENANTS_DIR / f"{tenant_key}.env"),
            port,
            now,
            now,
        ),
    )


def get_tenant(conn: sqlite3.Connection, tenant_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tenants WHERE tenant_key = ?", (tenant_key,)
    ).fetchone()


def get_tenant_by_subscription(
    conn: sqlite3.Connection, razorpay_subscription_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tenants WHERE razorpay_subscription_id = ?",
        (razorpay_subscription_id,),
    ).fetchone()


def list_tenants(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tenants ORDER BY created_at DESC"
    ).fetchall()


def next_free_port(conn: sqlite3.Connection) -> int:
    used = {row["port"] for row in conn.execute("SELECT port FROM tenants")}
    port = config.TENANT_PORT_RANGE_START
    while port in used:
        port += 1
    return port


def set_status(conn: sqlite3.Connection, tenant_key: str, status: str) -> None:
    assert status in STATUSES, f"unknown status {status!r}"
    conn.execute(
        "UPDATE tenants SET status = ?, updated_at = ? WHERE tenant_key = ?",
        (status, _now(), tenant_key),
    )


def set_provisioned(conn: sqlite3.Connection, tenant_key: str) -> None:
    conn.execute(
        "UPDATE tenants SET provisioned = 1, updated_at = ? WHERE tenant_key = ?",
        (_now(), tenant_key),
    )
