import fcntl
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.config import DB_PATH, DEFAULT_PAGE_KEY

SQLITE_HEADER = b"SQLite format 3\x00"


def _page_key_filter(page_key: str | None) -> tuple[str, list]:
    """Build a `page_key` SQL filter fragment, treating '' as the default page.

    insert_comment()'s page_key defaults to '' and many rows (especially
    YouTube, pre-multi-channel) were written that way rather than with an
    explicit DEFAULT_PAGE_KEY -- so filtering *for* the default page must
    match both, the same equivalence already used when normalizing a row's
    page_key elsewhere (e.g. post.py's `row_page_key or DEFAULT_PAGE_KEY`).
    """
    if not page_key:
        return "", []
    if page_key == DEFAULT_PAGE_KEY:
        return " AND (page_key = ? OR page_key = '')", [page_key]
    return " AND page_key = ?", [page_key]


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
    reply_checked_at TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS dashboard_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT,
    email TEXT,
    password_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_comments (
    comment_id TEXT PRIMARY KEY,
    platform TEXT,
    status TEXT,
    recorded_at TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS api_quota_usage (
    platform TEXT NOT NULL,
    period_key TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    limit_value INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform, period_key)
);

CREATE TABLE IF NOT EXISTS video_stats (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    page_key TEXT NOT NULL DEFAULT '',
    video_title TEXT,
    like_count INTEGER,
    share_count INTEGER,
    comment_count INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform, video_id)
);

"""

PRUNEABLE_COMMENT_STATUSES = ("posted", "already_replied", "rejected")
PRUNEABLE_WEBHOOK_STATUSES = ("processed", "failed")


def _restrict_db_file(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def is_plaintext_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == SQLITE_HEADER
    except OSError:
        return False


def _sqlcipher_module():
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError as exc:
        raise RuntimeError(
            "DB_ENCRYPTION_KEY is set but sqlcipher3 is not installed. "
            "Run: pip install sqlcipher3"
        ) from exc
    return sqlcipher


def _pragma_key_sql(key: str) -> str:
    if "'" in key or "\x00" in key:
        raise RuntimeError("DB_ENCRYPTION_KEY must not contain quotes or NUL bytes.")
    return f"PRAGMA key = '{key}'"


def _apply_key(conn, key: str) -> None:
    setter = getattr(conn, "set_key", None)
    if setter is not None:
        setter(key)
    else:
        conn.execute(_pragma_key_sql(key))
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()


def _encrypt_plaintext_file(path: Path, key: str) -> None:
    sqlcipher = _sqlcipher_module()
    _pragma_key_sql(key)
    tmp = path.with_name(path.name + ".encrypting")
    if tmp.exists():
        tmp.unlink()
    escaped_tmp = str(tmp).replace("'", "''")
    source = sqlcipher.connect(str(path), timeout=30)
    try:
        source.execute(f"ATTACH DATABASE '{escaped_tmp}' AS encrypted KEY '{key}'")
        source.execute("SELECT sqlcipher_export('encrypted')")
        source.execute("DETACH DATABASE encrypted")
    finally:
        source.close()
    os.replace(tmp, path)
    _restrict_db_file(path)


def _ensure_encrypted(path: Path, key: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    if not is_plaintext_sqlite(path):
        return
    lock_path = path.with_name(path.name + ".encrypt.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and is_plaintext_sqlite(path):
            _encrypt_plaintext_file(path, key)


def open_connection(path: Path | str | None = None, *, migrate_plaintext: bool = True):
    """Open comments.db, encrypting a legacy plaintext file when a key is set."""
    db_path = Path(path) if path is not None else Path(DB_PATH)
    key = config.DB_ENCRYPTION_KEY
    if not key:
        if db_path.exists() and db_path.stat().st_size > 0 and not is_plaintext_sqlite(db_path):
            raise RuntimeError(
                f"{db_path} is encrypted. Set DB_ENCRYPTION_KEY to the same "
                "value used when it was encrypted."
            )
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        _restrict_db_file(db_path)
        return conn

    if not migrate_plaintext and db_path.exists() and is_plaintext_sqlite(db_path):
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    _ensure_encrypted(db_path, key)
    sqlcipher = _sqlcipher_module()
    conn = sqlcipher.connect(str(db_path), timeout=30)
    conn.row_factory = sqlcipher.Row
    try:
        _apply_key(conn, key)
    except Exception as exc:
        conn.close()
        raise RuntimeError(
            f"Could not open encrypted database {db_path}. Check DB_ENCRYPTION_KEY."
        ) from exc
    _restrict_db_file(db_path)
    return conn


@contextmanager
def connect():
    conn = open_connection()
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
        if "reply_checked_at" not in columns:
            conn.execute("ALTER TABLE comments ADD COLUMN reply_checked_at TEXT")
        if "retry_count" not in columns:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
        if "page_key" not in columns:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN page_key TEXT NOT NULL DEFAULT ''"
            )
            # At migration time only one page (Hindolroad) has ever existed
            # across every platform, so every pre-existing row -- YouTube
            # included -- unambiguously belongs to it.
            conn.execute(
                "UPDATE comments SET page_key = ? WHERE page_key = ''",
                (DEFAULT_PAGE_KEY,),
            )
        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(dashboard_users)")
        }
        if "display_name" not in user_columns:
            conn.execute("ALTER TABLE dashboard_users ADD COLUMN display_name TEXT")
        if "email" not in user_columns:
            conn.execute("ALTER TABLE dashboard_users ADD COLUMN email TEXT")
        # Older builds allowed duplicate profile emails. Preserve the earliest
        # account as the owner and clear the duplicate copies before adding the
        # constraint; no user account is removed.
        conn.execute(
            """
            UPDATE dashboard_users
            SET email = NULL, updated_at = ?
            WHERE email IS NOT NULL AND trim(email) <> ''
              AND id NOT IN (
                  SELECT MIN(id) FROM dashboard_users
                  WHERE email IS NOT NULL AND trim(email) <> ''
                  GROUP BY lower(trim(email))
              )
            """,
            (now(),),
        )
        conn.execute(
            "UPDATE dashboard_users SET email = trim(email) WHERE email IS NOT NULL"
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS dashboard_users_email_unique
               ON dashboard_users(email COLLATE NOCASE)
               WHERE email IS NOT NULL AND email <> ''"""
        )
        _migrate_seen_comments(conn)
        sync_seen_stats_from_comments(conn)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_quota_usage(platform: str, period_key: str, amount: int, limit_value: int) -> None:
    """Add locally observed API usage without coupling callers to a DB connection."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO api_quota_usage
                   (platform, period_key, used, limit_value, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(platform, period_key) DO UPDATE SET
                   used = used + excluded.used,
                   limit_value = excluded.limit_value,
                   updated_at = excluded.updated_at""",
            (platform, period_key, amount, limit_value, now()),
        )


def set_quota_usage(platform: str, period_key: str, used: int, limit_value: int) -> None:
    """Store the latest provider-reported rolling usage percentage."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO api_quota_usage
                   (platform, period_key, used, limit_value, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(platform, period_key) DO UPDATE SET
                   used = excluded.used,
                   limit_value = excluded.limit_value,
                   updated_at = excluded.updated_at""",
            (platform, period_key, used, limit_value, now()),
        )


def get_quota_usage(conn: sqlite3.Connection, platform: str, period_key: str):
    return conn.execute(
        """SELECT used, limit_value, updated_at FROM api_quota_usage
           WHERE platform = ? AND period_key = ?""",
        (platform, period_key),
    ).fetchone()


def distinct_containers(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    page_key: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return the most recently active videos/posts, one row each.

    Used to pick which containers a stats refresh should spend its (bounded)
    API calls on -- the ones with recent comment activity, not every
    container the bot has ever seen.
    """
    sql = """
        SELECT platform, video_id,
               COALESCE(NULLIF(page_key, ''), ?) AS page_key,
               MAX(video_title) AS video_title,
               MAX(created_at) AS last_comment_at
        FROM comments WHERE video_id != ''
    """
    params: list = [DEFAULT_PAGE_KEY]
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    clause, extra = _page_key_filter(page_key)
    sql += clause
    params.extend(extra)
    sql += """
        GROUP BY platform, video_id
        ORDER BY last_comment_at DESC
        LIMIT ?
    """
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def upsert_video_stats(
    conn: sqlite3.Connection,
    *,
    platform: str,
    video_id: str,
    page_key: str,
    video_title: str,
    like_count: int | None,
    share_count: int | None,
    comment_count: int | None,
) -> None:
    conn.execute(
        """INSERT INTO video_stats
               (platform, video_id, page_key, video_title, like_count,
                share_count, comment_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(platform, video_id) DO UPDATE SET
               page_key = excluded.page_key,
               video_title = excluded.video_title,
               like_count = excluded.like_count,
               share_count = excluded.share_count,
               comment_count = excluded.comment_count,
               updated_at = excluded.updated_at""",
        (
            platform,
            video_id,
            page_key,
            video_title,
            like_count,
            share_count,
            comment_count,
            now(),
        ),
    )


def list_video_stats(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    page_key: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return cached engagement counts, most recently commented-on first."""
    sql = """
        SELECT vs.*,
               MAX(c.created_at) AS last_comment_at,
               datetime(vs.updated_at, '+5 hours', '+30 minutes') AS updated_at_ist
        FROM video_stats vs
        LEFT JOIN comments c
            ON c.platform = vs.platform AND c.video_id = vs.video_id
        WHERE 1=1
    """
    params: list = []
    if platform:
        sql += " AND vs.platform = ?"
        params.append(platform)
    if page_key:
        if page_key == DEFAULT_PAGE_KEY:
            sql += " AND (vs.page_key = ? OR vs.page_key = '')"
        else:
            sql += " AND vs.page_key = ?"
        params.append(page_key)
    sql += """
        GROUP BY vs.platform, vs.video_id
        ORDER BY last_comment_at DESC, vs.updated_at DESC
        LIMIT ?
    """
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def _migrate_seen_comments(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(seen_comments)")}
    if "created_at" not in columns:
        conn.execute("ALTER TABLE seen_comments ADD COLUMN created_at TEXT")
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE seen_comments ADD COLUMN updated_at TEXT")
    if "page_key" not in columns:
        conn.execute("ALTER TABLE seen_comments ADD COLUMN page_key TEXT")
        conn.execute(
            "UPDATE seen_comments SET page_key = ? WHERE page_key IS NULL",
            (DEFAULT_PAGE_KEY,),
        )
    conn.execute(
        """
        UPDATE seen_comments
        SET created_at = COALESCE(created_at, recorded_at),
            updated_at = COALESCE(updated_at, recorded_at)
        WHERE created_at IS NULL OR updated_at IS NULL
        """
    )


def sync_seen_stats_from_comments(conn: sqlite3.Connection) -> None:
    """Copy ids, statuses, and timestamps from live comment rows into seen_comments."""
    conn.execute(
        """
        INSERT INTO seen_comments
            (comment_id, platform, status, page_key, recorded_at, created_at, updated_at)
        SELECT comment_id, platform, status, page_key,
               COALESCE(updated_at, created_at), created_at, updated_at
        FROM comments
        WHERE true
        ON CONFLICT(comment_id) DO UPDATE SET
            platform = excluded.platform,
            status = excluded.status,
            page_key = excluded.page_key,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
        """
    )


def import_seen_stats_from_backup(backup_path) -> int:
    """Restore dashboard count timestamps from a comments.db backup without text."""
    path = Path(backup_path)
    if not path.exists():
        raise FileNotFoundError(f"Backup not found: {path}")
    init_db()
    imported = 0
    bak = open_connection(path, migrate_plaintext=False)
    try:
        tables = {
            row[0] for row in bak.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "comments" not in tables:
            raise RuntimeError(f"No comments table in {path}")
        rows = bak.execute(
            """
            SELECT comment_id, platform, status, created_at, updated_at
            FROM comments
            """
        ).fetchall()
        with connect() as conn:
            for row in rows:
                remember_seen_comment(
                    conn,
                    row["comment_id"],
                    platform=row["platform"],
                    status=row["status"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                imported += 1
    finally:
        bak.close()
    return imported


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


def initialize_dashboard_user(
    conn: sqlite3.Connection, username: str, password_hash: str
) -> None:
    """Migrate the original environment-backed account into the users table."""
    if username and password_hash:
        ts = now()
        conn.execute(
            """
            INSERT OR IGNORE INTO dashboard_users
                (username, password_hash, version, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (username.strip(), password_hash, ts, ts),
        )


def create_dashboard_user(
    conn: sqlite3.Connection, username: str, password_hash: str
) -> bool:
    ts = now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO dashboard_users
            (username, password_hash, version, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (username.strip(), password_hash, ts, ts),
    )
    return cursor.rowcount == 1


def get_dashboard_user(conn: sqlite3.Connection, username: str):
    return conn.execute(
        """SELECT id, username, display_name, email, password_hash, version,
                  created_at, updated_at
           FROM dashboard_users WHERE username = ? COLLATE NOCASE""",
        (username.strip(),),
    ).fetchone()


def update_dashboard_user_password(
    conn: sqlite3.Connection, username: str, password_hash: str
) -> int:
    conn.execute(
        """
        UPDATE dashboard_users
        SET password_hash = ?, version = version + 1, updated_at = ?
        WHERE username = ? COLLATE NOCASE
        """,
        (password_hash, now(), username.strip()),
    )
    return int(get_dashboard_user(conn, username)["version"])


def update_dashboard_user_profile(
    conn: sqlite3.Connection, username: str, *, display_name: str, email: str
) -> bool:
    try:
        conn.execute(
            """
            UPDATE dashboard_users
            SET display_name = ?, email = ?, updated_at = ?
            WHERE username = ? COLLATE NOCASE
            """,
            (display_name or None, email or None, now(), username.strip()),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def dashboard_email_registered(
    conn: sqlite3.Connection, email: str, *, excluding_username: str
) -> bool:
    if not email:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM dashboard_users
        WHERE email = ? COLLATE NOCASE
          AND username <> ? COLLATE NOCASE
        """,
        (email.strip(), excluding_username.strip()),
    ).fetchone()
    return row is not None


def remember_seen_comment(
    conn: sqlite3.Connection,
    comment_id: str,
    *,
    platform: str | None = None,
    status: str | None = None,
    page_key: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> None:
    """Keep a tiny fingerprint so pruned comments are not drafted or posted again.

    created_at / updated_at power dashboard 1 Hour–365 Day counts after prune.
    page_key keeps page attribution alive across a prune, so a Facebook/
    Instagram comment's account doesn't become unknown once its full
    comments row is deleted.
    """
    ts = now()
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_comments
            (comment_id, platform, status, page_key, recorded_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (comment_id, platform, status, page_key, ts, created_at or ts, updated_at or ts),
    )
    fields = []
    params: list = []
    if platform is not None:
        fields.append("platform = ?")
        params.append(platform)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if page_key is not None:
        fields.append("page_key = ?")
        params.append(page_key)
    if created_at is not None:
        fields.append("created_at = ?")
        params.append(created_at)
    if updated_at is not None:
        fields.append("updated_at = ?")
        params.append(updated_at)
    if fields:
        params.append(comment_id)
        conn.execute(
            f"UPDATE seen_comments SET {', '.join(fields)} WHERE comment_id = ?",
            params,
        )


def comment_exists(conn: sqlite3.Connection, comment_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM comments WHERE comment_id = ?
        UNION ALL
        SELECT 1 FROM seen_comments WHERE comment_id = ?
        LIMIT 1
        """,
        (comment_id, comment_id),
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
    page_key: str = "",
    reply_checked_at: str | None = None,
) -> None:
    # video_id/video_title double as the generic "container" id/title for
    # non-YouTube platforms (Facebook post id/message, Instagram media id/caption).
    # page_key identifies which configured page/channel (see config.PAGES)
    # this comment belongs to, across all three platforms.
    ts = now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO comments (
            comment_id, platform, page_key, video_id, video_title, author, text,
            published_at, status, draft_reply, reply_checked_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)
        """,
        (
            comment_id,
            platform,
            page_key,
            video_id,
            video_title,
            author,
            text,
            published_at,
            draft_reply,
            reply_checked_at,
            ts,
            ts,
        ),
    )
    remember_seen_comment(
        conn,
        comment_id,
        platform=platform,
        page_key=page_key or None,
        status="pending_review" if cursor.rowcount == 1 else None,
        created_at=ts if cursor.rowcount == 1 else None,
        updated_at=ts if cursor.rowcount == 1 else None,
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


def record_reply_check(conn: sqlite3.Connection, comment_id: str) -> None:
    """Remember a completed remote check without changing the comment status."""
    conn.execute(
        "UPDATE comments SET reply_checked_at = ? WHERE comment_id = ?",
        (now(), comment_id),
    )


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


def count_posted_today(
    conn: sqlite3.Connection, platform: str, page_key: str | None = None
) -> int:
    """Count replies posted so far in the current IST calendar day.

    Used to enforce a configurable daily reply cap per platform and page --
    two pages/channels sharing the same platform string must not share one
    counter -- separate from the per-cycle publish limit. `updated_at` is
    the time a row moved to 'posted', which is the only status change that
    represents an actual reply going out.
    """
    sql = """
        SELECT COUNT(*) AS n FROM comments
        WHERE platform = ? AND status = 'posted'
          AND date(updated_at, '+5 hours', '+30 minutes')
              = date('now', '+5 hours', '+30 minutes')
    """
    params: list = [platform]
    clause, extra = _page_key_filter(page_key)
    sql += clause
    params.extend(extra)
    row = conn.execute(sql, params).fetchone()
    return row["n"]


def count_drafted_today(conn: sqlite3.Connection) -> int:
    """Count comments drafted so far in the current IST calendar day.

    A row with a non-empty draft_reply corresponds to one Gemini draft call
    (all platforms share the same billed API key); a comment found already
    replied to gets inserted with an empty draft_reply and costs nothing, so
    it is excluded here. Doubles as a cross-platform Gemini spend counter for
    GEMINI_DAILY_DRAFT_LIMIT.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM comments
        WHERE draft_reply IS NOT NULL AND draft_reply != ''
          AND date(created_at, '+5 hours', '+30 minutes')
              = date('now', '+5 hours', '+30 minutes')
        """
    ).fetchone()
    return row["n"]


def count_by_status(
    conn: sqlite3.Connection, *, platform: str | None = None, page_key: str | None = None
) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM comments WHERE 1=1"
    params = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    clause, extra = _page_key_filter(page_key)
    sql += clause
    params.extend(extra)
    sql += " GROUP BY status"
    rows = conn.execute(sql, params).fetchall()
    return {row["status"]: row["n"] for row in rows}


def activity_summary(
    conn: sqlite3.Connection,
    *,
    reference_time: datetime | None = None,
    platform: str | None = None,
    page_key: str | None = None,
) -> list[dict]:
    """Return received and handled comment totals for dashboard time windows."""
    reference_time = reference_time or datetime.now(timezone.utc)
    windows = (
        ("1 Hour", timedelta(hours=1)),
        ("24 Hours", timedelta(hours=24)),
        ("7 Day", timedelta(days=7)),
        ("365 Days", timedelta(days=365)),
    )
    # Named parameters (not positional "?") so the same :platform value can
    # be bound once and reused everywhere it appears in the query, instead
    # of needing its position in a params list kept in lockstep with every
    # "?" -- that positional scheme previously required hand-duplicating
    # `platform` 3-6 times per query and was one query edit away from a
    # silently wrong (not erroring) dashboard count.
    platform_filter = (
        "AND COALESCE(c.platform, s.platform) = :platform" if platform else ""
    )
    detailed_filter = "AND c.platform = :platform" if platform else ""
    # '' and DEFAULT_PAGE_KEY are the same page (see _page_key_filter) --
    # many rows, especially YouTube's pre-multi-channel, were written with
    # page_key='' rather than an explicit default key.
    if page_key == DEFAULT_PAGE_KEY:
        page_key_filter = "AND COALESCE(c.page_key, s.page_key) IN (:page_key, '')"
        detailed_page_key_filter = "AND c.page_key IN (:page_key, '')"
    elif page_key:
        page_key_filter = "AND COALESCE(c.page_key, s.page_key) = :page_key"
        detailed_page_key_filter = "AND c.page_key = :page_key"
    else:
        page_key_filter = ""
        detailed_page_key_filter = ""
    sql = f"""
        SELECT
            SUM(CASE WHEN datetime(COALESCE(c.created_at, s.created_at)) >= datetime(:cutoff)
                      {platform_filter} {page_key_filter} THEN 1 ELSE 0 END)
                AS received,
            SUM(CASE WHEN COALESCE(c.status, s.status) = 'posted'
                      AND datetime(COALESCE(c.updated_at, s.updated_at)) >= datetime(:cutoff)
                      {platform_filter} {page_key_filter} THEN 1 ELSE 0 END)
                AS posted,
            SUM(CASE WHEN COALESCE(c.status, s.status) = 'already_replied'
                      AND datetime(COALESCE(c.updated_at, s.updated_at)) >= datetime(:cutoff)
                      {platform_filter} {page_key_filter} THEN 1 ELSE 0 END)
                AS already_replied,
            SUM(CASE WHEN c.status = 'posted'
                      AND datetime(c.updated_at) >= datetime(:cutoff)
                      {detailed_filter} {detailed_page_key_filter} THEN 1 ELSE 0 END)
                AS detailed_posted
        FROM seen_comments s
        LEFT JOIN comments c ON c.comment_id = s.comment_id
    """
    summaries = []
    for label, duration in windows:
        cutoff = (reference_time - duration).isoformat()
        params = {"cutoff": cutoff}
        if platform:
            params["platform"] = platform
        if page_key:
            params["page_key"] = page_key
        row = conn.execute(sql, params).fetchone()
        posted = int(row["posted"] or 0)
        already_replied = int(row["already_replied"] or 0)
        summaries.append(
            {
                "label": label,
                "received": int(row["received"] or 0),
                "posted": posted,
                "already_replied": already_replied,
                "handled": posted + already_replied,
                "detailed_posted": int(row["detailed_posted"] or 0),
            }
        )
    return summaries


def list_comments(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    platform: str | None = None,
    page_key: str | None = None,
    query: str | None = None,
    sort_order: str = "desc",
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
    clause, extra = _page_key_filter(page_key)
    sql += clause
    params.extend(extra)
    if query:
        like = f"%{query}%"
        sql += " AND (author LIKE ? OR text LIKE ? OR draft_reply LIKE ? OR video_title LIKE ?)"
        params.extend([like, like, like, like])
    # posted_at_ist is the IST rendering of updated_at, so this keeps the most
    # recently posted or otherwise handled comments at the top of the table.
    direction = "ASC" if sort_order == "asc" else "DESC"
    sql += f" ORDER BY updated_at {direction}, created_at {direction} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def count_comments(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    platform: str | None = None,
    page_key: str | None = None,
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
    clause, extra = _page_key_filter(page_key)
    sql += clause
    params.extend(extra)
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
    page_key: str | None = None,
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
    clause, extra = _page_key_filter(page_key)
    sql += clause
    params.extend(extra)
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
    remember_seen_comment(conn, comment_id, status=status, updated_at=now())


def record_publish_failure(
    conn: sqlite3.Connection, comment_id: str, error: str, *, max_attempts: int
) -> str:
    """Record a failed publish attempt, giving up after `max_attempts`.

    A comment that fails the same way on every attempt (Meta hides it,
    the commenter blocked the Page, the comment was deleted, ...) would
    otherwise be retried forever by `--retry-failed`, repeatedly tripping
    PUBLISH_ERROR_LIMIT and crowding out comments that could still succeed.
    Returns the status the row was set to ('failed' or 'rejected').
    """
    row = conn.execute(
        "SELECT retry_count FROM comments WHERE comment_id = ?", (comment_id,)
    ).fetchone()
    attempts = (row["retry_count"] if row else 0) + 1
    if attempts >= max_attempts:
        status = "rejected"
        error = f"Gave up after {attempts} failed attempts: {error}"
    else:
        status = "failed"
    conn.execute(
        """UPDATE comments
           SET status = ?, error = ?, retry_count = ?, updated_at = ?
           WHERE comment_id = ?""",
        (status, error[:1000], attempts, now(), comment_id),
    )
    remember_seen_comment(conn, comment_id, status=status, updated_at=now())
    return status


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


def prune_storage(conn: sqlite3.Connection, *, older_than_days: int = 0) -> dict[str, int]:
    """Remove bulky handled rows while keeping comment_id fingerprints.

    Dashboard lists come from `comments`, so pruned history goes blank.
    Polling and webhooks still skip those IDs via `seen_comments`.
    Pending, approved, failed, and posting rows are left in place.
    """
    sync_seen_stats_from_comments(conn)
    placeholders = ",".join("?" * len(PRUNEABLE_COMMENT_STATUSES))
    comment_sql = f"DELETE FROM comments WHERE status IN ({placeholders})"
    comment_params: list = list(PRUNEABLE_COMMENT_STATUSES)
    if older_than_days > 0:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()
        comment_sql += " AND updated_at < ?"
        comment_params.append(cutoff)
    comments_deleted = conn.execute(comment_sql, comment_params).rowcount
    webhook_placeholders = ",".join("?" * len(PRUNEABLE_WEBHOOK_STATUSES))
    webhooks_deleted = conn.execute(
        f"DELETE FROM webhook_events WHERE status IN ({webhook_placeholders})",
        PRUNEABLE_WEBHOOK_STATUSES,
    ).rowcount
    return {
        "comments_deleted": comments_deleted,
        "webhooks_deleted": webhooks_deleted,
    }


def vacuum_db() -> None:
    conn = open_connection()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
