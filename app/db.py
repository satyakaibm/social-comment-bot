import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'youtube',
    video_id TEXT NOT NULL,
    video_title TEXT,
    author TEXT,
    text TEXT NOT NULL,
    published_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    draft_reply TEXT,
    reply_comment_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_auth (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    password_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Migrate DBs created before multi-platform support.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
        if "platform" not in columns:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN platform TEXT NOT NULL DEFAULT 'youtube'"
            )
        if "error" not in columns:
            conn.execute("ALTER TABLE comments ADD COLUMN error TEXT")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_dashboard_auth(conn: sqlite3.Connection, password_hash: str) -> None:
    """Seed persistent dashboard authentication from the environment once."""
    if password_hash:
        conn.execute(
            """
            INSERT OR IGNORE INTO dashboard_auth
                (singleton, password_hash, version, updated_at)
            VALUES (1, ?, 1, ?)
            """,
            (password_hash, now()),
        )


def get_dashboard_auth(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT password_hash, version FROM dashboard_auth WHERE singleton = 1"
    ).fetchone()


def update_dashboard_password(conn: sqlite3.Connection, password_hash: str) -> int:
    conn.execute(
        """
        UPDATE dashboard_auth
        SET password_hash = ?, version = version + 1, updated_at = ?
        WHERE singleton = 1
        """,
        (password_hash, now()),
    )
    return int(get_dashboard_auth(conn)["version"])


def comment_exists(conn: sqlite3.Connection, comment_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comments WHERE comment_id = ?", (comment_id,)
    ).fetchone()
    return row is not None


def insert_comment(
    conn: sqlite3.Connection,
    *,
    comment_id: str,
    video_id: str,
    video_title: str,
    author: str,
    text: str,
    published_at: str,
    draft_reply: str,
    platform: str = "youtube",
) -> None:
    # video_id/video_title double as the generic "container" id/title for
    # non-YouTube platforms (Facebook post id/message, Instagram media id/caption).
    ts = now()
    conn.execute(
        """
        INSERT OR IGNORE INTO comments (
            comment_id, platform, video_id, video_title, author, text, published_at,
            status, draft_reply, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?)
        """,
        (
            comment_id,
            platform,
            video_id,
            video_title,
            author,
            text,
            published_at,
            draft_reply,
            ts,
            ts,
        ),
    )


def list_by_status(conn: sqlite3.Connection, status: str):
    return conn.execute(
        "SELECT * FROM comments WHERE status = ? ORDER BY created_at ASC", (status,)
    ).fetchall()


def get_comment(conn: sqlite3.Connection, comment_id: str):
    return conn.execute(
        "SELECT * FROM comments WHERE comment_id = ?", (comment_id,)
    ).fetchone()


def claim_comment_for_post(
    conn: sqlite3.Connection, comment_id: str, expected_status: str
) -> bool:
    """Atomically reserve one comment for a single publishing process."""
    result = conn.execute(
        """
        UPDATE comments
        SET status = 'posting', updated_at = ?
        WHERE comment_id = ? AND status = ?
        """,
        (now(), comment_id, expected_status),
    )
    return result.rowcount == 1


def reset_stale_posting(conn: sqlite3.Connection, *, minutes: int = 10) -> int:
    """Release claims left behind when a publisher was forcibly stopped."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    result = conn.execute(
        """
        UPDATE comments
        SET status = 'failed', error = 'Publishing was interrupted; safe to retry',
            updated_at = ?
        WHERE status = 'posting' AND updated_at < ?
        """,
        (now(), cutoff),
    )
    return result.rowcount


def count_by_status(
    conn: sqlite3.Connection, *, platform: str | None = None
) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM comments"
    params = []
    if platform:
        sql += " WHERE platform = ?"
        params.append(platform)
    sql += " GROUP BY status"
    rows = conn.execute(sql, params).fetchall()
    return {row["status"]: row["n"] for row in rows}


def activity_summary(
    conn: sqlite3.Connection,
    *,
    reference_time: datetime | None = None,
    platform: str | None = None,
) -> list[dict]:
    """Return received and handled comment totals for dashboard time windows."""
    reference_time = reference_time or datetime.now(timezone.utc)
    windows = (
        ("Last 24 hours", timedelta(hours=24)),
        ("Last 7 days", timedelta(days=7)),
        ("Last 1 year", timedelta(days=365)),
    )
    summaries = []
    for label, duration in windows:
        cutoff = (reference_time - duration).isoformat()
        platform_filter = "AND platform = ?" if platform else ""
        params = (
            [cutoff, platform, cutoff, platform, cutoff, platform]
            if platform
            else [cutoff, cutoff, cutoff]
        )
        row = conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN datetime(created_at) >= datetime(?)
                          {platform_filter} THEN 1 ELSE 0 END)
                    AS received,
                SUM(CASE WHEN status = 'posted'
                          AND datetime(updated_at) >= datetime(?)
                          {platform_filter} THEN 1 ELSE 0 END)
                    AS posted,
                SUM(CASE WHEN status = 'already_replied'
                          AND datetime(updated_at) >= datetime(?)
                          {platform_filter} THEN 1 ELSE 0 END)
                    AS already_replied
            FROM comments
            """,
            params,
        ).fetchone()
        posted = int(row["posted"] or 0)
        already_replied = int(row["already_replied"] or 0)
        summaries.append(
            {
                "label": label,
                "received": int(row["received"] or 0),
                "posted": posted,
                "already_replied": already_replied,
                "handled": posted + already_replied,
            }
        )
    return summaries


def list_comments(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    platform: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    sql = """SELECT *,
        datetime(
            replace(replace(published_at, 'T', ' '), '+0000', ''),
            '+5 hours',
            '+30 minutes'
        ) AS published_at_ist,
        datetime(created_at, '+5 hours', '+30 minutes') AS created_at_ist,
        datetime(updated_at, '+5 hours', '+30 minutes') AS posted_at_ist
        FROM comments WHERE 1=1"""
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if query:
        like = f"%{query}%"
        sql += " AND (author LIKE ? OR text LIKE ? OR draft_reply LIKE ? OR video_title LIKE ?)"
        params.extend([like, like, like, like])
    # posted_at_ist is the IST rendering of updated_at, so this keeps the most
    # recently posted or otherwise handled comments at the top of the table.
    sql += " ORDER BY updated_at DESC, created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def count_comments(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    platform: str | None = None,
    query: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) AS n FROM comments WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if query:
        like = f"%{query}%"
        sql += " AND (author LIKE ? OR text LIKE ? OR draft_reply LIKE ? OR video_title LIKE ?)"
        params.extend([like, like, like, like])
    return conn.execute(sql, params).fetchone()["n"]


def list_for_post(
    conn: sqlite3.Connection,
    *,
    statuses: list[str],
    platform: str | None = None,
    video_id: str | None = None,
    comment_id: str | None = None,
    limit: int | None = None,
):
    placeholders = ",".join("?" * len(statuses))
    sql = f"SELECT * FROM comments WHERE status IN ({placeholders})"
    params: list = list(statuses)
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if video_id:
        sql += " AND video_id = ?"
        params.append(video_id)
    if comment_id:
        sql += " AND comment_id = ?"
        params.append(comment_id)
    # Process fresh work before retries so a permanently failing old comment
    # cannot starve new replies when a per-cycle limit is used.
    sql += """ ORDER BY CASE status
        WHEN 'approved' THEN 0
        WHEN 'pending_review' THEN 1
        WHEN 'failed' THEN 2
        ELSE 3 END, created_at ASC"""
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def update_status(
    conn: sqlite3.Connection,
    comment_id: str,
    status: str,
    *,
    draft_reply: str | None = None,
    reply_comment_id: str | None = None,
    error: str | None = None,
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, now()]
    if draft_reply is not None:
        fields.append("draft_reply = ?")
        params.append(draft_reply)
    if reply_comment_id is not None:
        fields.append("reply_comment_id = ?")
        params.append(reply_comment_id)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    params.append(comment_id)
    conn.execute(f"UPDATE comments SET {', '.join(fields)} WHERE comment_id = ?", params)


def enqueue_webhook_event(conn, *, event_key: str, platform: str, payload: str) -> bool:
    ts = now()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO webhook_events
           (event_key, platform, payload, status, created_at, updated_at)
           VALUES (?, ?, ?, 'pending', ?, ?)""",
        (event_key, platform, payload, ts, ts),
    )
    return cursor.rowcount == 1


def reset_interrupted_webhook_events(conn) -> None:
    conn.execute(
        "UPDATE webhook_events SET status='pending', updated_at=? WHERE status='processing'",
        (now(),),
    )


def claim_webhook_event(conn):
    row = conn.execute(
        "SELECT * FROM webhook_events WHERE status='pending' ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE webhook_events SET status='processing', updated_at=? WHERE event_key=?",
        (now(), row["event_key"]),
    )
    conn.commit()
    return row


def finish_webhook_event(conn, event_key: str, *, error: str | None = None) -> None:
    conn.execute(
        "UPDATE webhook_events SET status=?, error=?, updated_at=? WHERE event_key=?",
        ("failed" if error else "processed", error, now(), event_key),
    )
