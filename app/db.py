import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

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


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM comments GROUP BY status"
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


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
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
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
    sql += " ORDER BY created_at ASC"
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
