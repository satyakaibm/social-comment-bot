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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        # Migrate DBs created before multi-platform support.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
        if "platform" not in columns:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN platform TEXT NOT NULL DEFAULT 'youtube'"
            )


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


def list_for_post(
    conn: sqlite3.Connection,
    *,
    statuses: list[str],
    platform: str | None = None,
    video_id: str | None = None,
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
) -> None:
    fields = ["status = ?", "updated_at = ?"]
    params: list = [status, now()]
    if draft_reply is not None:
        fields.append("draft_reply = ?")
        params.append(draft_reply)
    if reply_comment_id is not None:
        fields.append("reply_comment_id = ?")
        params.append(reply_comment_id)
    params.append(comment_id)
    conn.execute(f"UPDATE comments SET {', '.join(fields)} WHERE comment_id = ?", params)
