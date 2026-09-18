import fcntl
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.config import DB_PATH, DEFAULT_PAGE_KEY

SQLITE_HEADER = b"SQLite format 3\x00"


def _published_at_utc_expr(column: str) -> str:
    """SQL expression converting a platform `published_at` column to UTC.

    YouTube's publishedAt and the Facebook/Instagram Graph API's
    created_time/timestamp are all ISO 8601. But Meta's real-time webhook
    delivers Facebook's `created_time` as Unix epoch seconds instead (see
    app/webhook.py), so digit-only values are parsed as epoch seconds rather
    than being handed to the ISO 8601 branch, where SQLite's datetime()
    treats them as an out-of-range Julian day and silently returns NULL.
    """
    trimmed = f"trim({column})"
    iso = (
        f"datetime(replace(replace(replace({trimmed}, 'T', ' '), "
        "'Z', ''), '+0000', ''))"
    )
    epoch = f"datetime(CAST({trimmed} AS INTEGER), 'unixepoch')"
    return (
        f"CASE WHEN {trimmed} GLOB '[0-9]*' AND {trimmed} NOT GLOB '*[^0-9]*' "
        f"THEN {epoch} ELSE {iso} END"
    )


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
    author_id TEXT NOT NULL DEFAULT '',
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
    is_admin INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS video_stats (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    page_key TEXT NOT NULL DEFAULT '',
    video_title TEXT,
    view_count INTEGER,
    like_count INTEGER,
    share_count INTEGER,
    comment_count INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform, video_id)
);

CREATE TABLE IF NOT EXISTS video_stats_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    page_key TEXT NOT NULL DEFAULT '',
    video_title TEXT,
    view_count INTEGER,
    like_count INTEGER,
    share_count INTEGER,
    comment_count INTEGER,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_key TEXT NOT NULL UNIQUE,
    recommendation_type TEXT NOT NULL,
    platform TEXT NOT NULL,
    page_key TEXT NOT NULL DEFAULT '',
    video_id TEXT,
    title TEXT,
    recommendation TEXT NOT NULL,
    test_dimension TEXT,
    variant_label TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    baseline_views INTEGER,
    baseline_likes INTEGER,
    baseline_comments INTEGER,
    baseline_shares INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
        conn.execute("PRAGMA journal_mode=WAL")
        _restrict_db_file(db_path)
        return conn

    if not migrate_plaintext and db_path.exists() and is_plaintext_sqlite(db_path):
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
    # Rollback-journal mode takes an exclusive lock for the whole commit,
    # blocking every reader (dashboard queries included) until it releases.
    # WAL lets readers proceed concurrently with a writer.
    conn.execute("PRAGMA journal_mode=WAL")
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
        if "author_id" not in columns:
            conn.execute(
                "ALTER TABLE comments ADD COLUMN author_id TEXT NOT NULL DEFAULT ''"
            )
        user_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(dashboard_users)")
        }
        if "display_name" not in user_columns:
            conn.execute("ALTER TABLE dashboard_users ADD COLUMN display_name TEXT")
        if "email" not in user_columns:
            conn.execute("ALTER TABLE dashboard_users ADD COLUMN email TEXT")
        if "is_admin" not in user_columns:
            conn.execute(
                "ALTER TABLE dashboard_users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
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
        experiment_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(recommendation_experiments)")
        }
        for column in ("test_dimension", "variant_label", "notes"):
            if column not in experiment_columns:
                conn.execute(f"ALTER TABLE recommendation_experiments ADD COLUMN {column} TEXT")
        conn.execute(
            "UPDATE dashboard_users SET email = trim(email) WHERE email IS NOT NULL"
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS dashboard_users_email_unique
               ON dashboard_users(email COLLATE NOCASE)
               WHERE email IS NOT NULL AND email <> ''"""
        )
        # Speeds up the dashboard's status/platform/channel filters and any
        # per-video lookup (e.g. list_for_post, video_stats' join) now that
        # comments has grown past what a full table scan handles quickly.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(platform, video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_platform_page_key ON comments(platform, page_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_updated_at ON comments(updated_at)")
        # Momentum's audience_activity/comment_text_sample filter on a 90-day
        # created_at cutoff on every page load; without this, created_at was
        # only ever compared through datetime(), which can't use an index and
        # forced a full table scan (83k+ rows in seen_comments) each time.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_created_at ON comments(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_comments_created_at ON seen_comments(created_at)")
        stats_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(video_stats)")
        }
        if "view_count" not in stats_columns:
            conn.execute("ALTER TABLE video_stats ADD COLUMN view_count INTEGER")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_video_stats_history_lookup
               ON video_stats_history(platform, page_key, video_id, captured_at)"""
        )
        _migrate_seen_comments(conn)
        sync_seen_stats_from_comments(conn)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_quota_usage(
    platform: str,
    period_key: str,
    amount: int,
    limit_value: int,
    *,
    conn=None,
) -> None:
    """Add locally observed API usage without coupling callers to a DB connection."""
    if conn is not None:
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
        conn.commit()
        return
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


def set_quota_usage(
    platform: str,
    period_key: str,
    used: int,
    limit_value: int,
    *,
    conn=None,
) -> None:
    """Store the latest provider-reported rolling usage percentage."""
    if conn is not None:
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
        conn.commit()
        return
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


def record_heartbeat(conn: sqlite3.Connection, worker: str, *, detail: str = "") -> None:
    """Record that a background worker completed a cycle just now.

    Purely observational -- nothing reads this to make decisions, it just
    lets the status page show whether each worker is actually still
    running, since none of them have an HTTP endpoint of their own to ask.
    """
    conn.execute(
        """INSERT INTO worker_heartbeats (worker, last_run_at, detail)
               VALUES (?, ?, ?)
           ON CONFLICT(worker) DO UPDATE SET
               last_run_at = excluded.last_run_at, detail = excluded.detail""",
        (worker, now(), detail),
    )
    conn.commit()


def list_heartbeats(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            "SELECT worker, last_run_at, detail FROM worker_heartbeats ORDER BY worker"
        )
    ]


def webhook_event_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {status: 0 for status in ("pending", "processing", "processed", "failed")}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM webhook_events GROUP BY status"
    ):
        counts[row["status"]] = row["n"]
    return counts


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
    view_count: int | None = None,
) -> None:
    captured_at = now()
    conn.execute(
        """INSERT INTO video_stats
               (platform, video_id, page_key, video_title, view_count,
                like_count, share_count, comment_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(platform, video_id) DO UPDATE SET
               page_key = excluded.page_key,
               video_title = excluded.video_title,
               view_count = excluded.view_count,
               like_count = excluded.like_count,
               share_count = excluded.share_count,
               comment_count = excluded.comment_count,
               updated_at = excluded.updated_at""",
        (
            platform,
            video_id,
            page_key,
            video_title,
            view_count,
            like_count,
            share_count,
            comment_count,
            captured_at,
        ),
    )
    conn.execute(
        """INSERT INTO video_stats_history
               (platform, video_id, page_key, video_title, view_count,
                like_count, share_count, comment_count, captured_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            platform,
            video_id,
            page_key,
            video_title,
            view_count,
            like_count,
            share_count,
            comment_count,
            captured_at,
        ),
    )


def prune_video_stats_history(
    conn: sqlite3.Connection, *, retention_days: int
) -> int:
    """Delete raw engagement snapshots older than the configured retention."""
    cursor = conn.execute(
        """DELETE FROM video_stats_history
           WHERE datetime(captured_at) < datetime('now', ?)""",
        (f"-{max(1, retention_days)} days",),
    )
    return cursor.rowcount


def list_video_stats_history(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    page_key: str | None = None,
    video_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM video_stats_history WHERE 1=1"
    params: list = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if page_key:
        clause, extra = _page_key_filter(page_key)
        sql += clause
        params.extend(extra)
    if video_id:
        sql += " AND video_id = ?"
        params.append(video_id)
    sql += " ORDER BY captured_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def video_stats_growth(
    conn: sqlite3.Connection,
    *,
    horizon: timedelta,
    platform: str | None = None,
    page_key: str | None = None,
    reference_time: datetime | None = None,
) -> list[dict]:
    """Return first-to-last metric deltas inside a real historical window.

    Two real costs stacked here and caused Momentum's original outage: the
    date filter used to be wrapped in datetime(), which can't use an index
    and forces a full scan; and "eligible" was referenced three times
    (bounds, oldest, newest) with no MATERIALIZED hint, so SQLite silently
    re-ran that full scan three times per call. Measured on production
    data: ~2.15s per call before, ~0.0002s after -- fine in isolation
    either way, but under real concurrent requests on a 2-vCPU box (each
    with its own SQLCipher connection independently re-decrypting the same
    pages) that difference compounded into requests taking 100+ seconds.
    """
    reference_time = reference_time or datetime.now(timezone.utc)
    cutoff = (reference_time - horizon).isoformat()
    sql = """
        WITH eligible AS MATERIALIZED (
            SELECT * FROM video_stats_history
            WHERE captured_at >= ?
    """
    params: list = [cutoff]
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if page_key:
        if page_key == DEFAULT_PAGE_KEY:
            sql += " AND page_key IN (?, '')"
        else:
            sql += " AND page_key = ?"
        params.append(page_key)
    sql += """
        ), bounds AS (
            SELECT platform, video_id, MIN(id) AS first_id, MAX(id) AS last_id,
                   COUNT(*) AS sample_count
            FROM eligible
            GROUP BY platform, video_id
            HAVING COUNT(*) >= 2
        )
        SELECT newest.platform, newest.video_id, newest.page_key,
               newest.video_title, bounds.sample_count,
               oldest.captured_at AS period_start,
               newest.captured_at AS period_end,
               (julianday(newest.captured_at) - julianday(oldest.captured_at)) * 24
                   AS elapsed_hours,
               CASE WHEN oldest.view_count IS NOT NULL AND newest.view_count IS NOT NULL
                    THEN MAX(0, newest.view_count - oldest.view_count) END AS view_growth,
               CASE WHEN oldest.like_count IS NOT NULL AND newest.like_count IS NOT NULL
                    THEN MAX(0, newest.like_count - oldest.like_count) END AS like_growth,
               CASE WHEN oldest.comment_count IS NOT NULL AND newest.comment_count IS NOT NULL
                    THEN MAX(0, newest.comment_count - oldest.comment_count) END AS comment_growth,
               CASE WHEN oldest.share_count IS NOT NULL AND newest.share_count IS NOT NULL
                    THEN MAX(0, newest.share_count - oldest.share_count) END AS share_growth
        FROM bounds
        JOIN eligible oldest ON oldest.id = bounds.first_id
        JOIN eligible newest ON newest.id = bounds.last_id
        ORDER BY newest.platform, newest.page_key, newest.video_id
    """
    return [dict(row) for row in conn.execute(sql, params)]


def audience_activity(
    conn: sqlite3.Connection,
    *,
    horizon: timedelta,
    platform: str | None = None,
    page_key: str | None = None,
    reference_time: datetime | None = None,
) -> list[dict]:
    """Aggregate audience comments by weekday/hour in IST per channel.

    Prefer the platform `published_at` so the window reflects when viewers
    actually commented, not when the bot ingested the row. Invalid or missing
    publish timestamps fall back to `created_at`.
    """
    reference_time = reference_time or datetime.now(timezone.utc)
    cutoff = (reference_time - horizon).isoformat()
    platform_expr = "COALESCE(NULLIF(c.platform, ''), s.platform)"
    page_expr = "COALESCE(NULLIF(c.page_key, ''), NULLIF(s.page_key, ''), '')"
    published_utc = _published_at_utc_expr("c.published_at")
    timestamp_expr = (
        f"COALESCE(CASE WHEN trim(COALESCE(c.published_at, '')) <> '' "
        f"AND {published_utc} IS NOT NULL THEN {published_utc} END, "
        "c.created_at, s.created_at)"
    )
    sql = f"""
        SELECT {platform_expr} AS platform, {page_expr} AS page_key,
               CAST(strftime('%w', datetime({timestamp_expr}, '+5 hours', '+30 minutes')) AS INTEGER)
                   AS weekday_ist,
               CAST(strftime('%H', datetime({timestamp_expr}, '+5 hours', '+30 minutes')) AS INTEGER)
                   AS hour_ist,
               COUNT(*) AS comment_count,
               COUNT(DISTINCT CASE
                   WHEN trim(COALESCE(c.author, '')) = '' THEN c.comment_id
                   ELSE c.author
               END) AS unique_authors
        FROM seen_comments s
        LEFT JOIN comments c ON c.comment_id = s.comment_id
        WHERE {timestamp_expr} >= ?
    """
    params: list = [cutoff]
    if platform:
        sql += f" AND {platform_expr} = ?"
        params.append(platform)
    if page_key:
        if page_key == DEFAULT_PAGE_KEY:
            sql += f" AND {page_expr} IN (?, '')"
        else:
            sql += f" AND {page_expr} = ?"
        params.append(page_key)
    sql += " GROUP BY 1, 2, 3, 4"
    return [dict(row) for row in conn.execute(sql, params)]


def engagement_activity(
    conn: sqlite3.Connection,
    *,
    horizon: timedelta,
    platform: str | None = None,
    page_key: str | None = None,
    reference_time: datetime | None = None,
) -> list[dict]:
    """Attribute snapshot-to-snapshot growth to the later capture hour in IST.

    Consecutive stats fetches (typically ~30 minutes) show when views, likes,
    comments, and shares actually moved. Gaps longer than 4 hours are ignored
    so an outage does not dump a multi-day total into one hour.
    """
    reference_time = reference_time or datetime.now(timezone.utc)
    cutoff = (reference_time - horizon).isoformat()
    sql = """
        WITH eligible AS MATERIALIZED (
            SELECT id, platform, page_key, video_id, view_count, like_count,
                   share_count, comment_count, captured_at
            FROM video_stats_history
            WHERE captured_at >= ?
    """
    params: list = [cutoff]
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if page_key:
        if page_key == DEFAULT_PAGE_KEY:
            sql += " AND page_key IN (?, '')"
        else:
            sql += " AND page_key = ?"
        params.append(page_key)
    sql += """
        ), deltas AS (
            SELECT platform,
                   COALESCE(NULLIF(page_key, ''), '') AS page_key,
                   captured_at,
                   CASE WHEN view_count IS NOT NULL
                             AND LAG(view_count) OVER w IS NOT NULL
                        THEN MAX(0, view_count - LAG(view_count) OVER w)
                        ELSE 0 END AS view_growth,
                   CASE WHEN like_count IS NOT NULL
                             AND LAG(like_count) OVER w IS NOT NULL
                        THEN MAX(0, like_count - LAG(like_count) OVER w)
                        ELSE 0 END AS like_growth,
                   CASE WHEN comment_count IS NOT NULL
                             AND LAG(comment_count) OVER w IS NOT NULL
                        THEN MAX(0, comment_count - LAG(comment_count) OVER w)
                        ELSE 0 END AS comment_growth,
                   CASE WHEN share_count IS NOT NULL
                             AND LAG(share_count) OVER w IS NOT NULL
                        THEN MAX(0, share_count - LAG(share_count) OVER w)
                        ELSE 0 END AS share_growth,
                   (julianday(captured_at) - julianday(LAG(captured_at) OVER w)) * 24
                       AS elapsed_hours
            FROM eligible
            WINDOW w AS (PARTITION BY platform, video_id ORDER BY captured_at, id)
        )
        SELECT platform, page_key,
               CAST(strftime('%w', datetime(captured_at, '+5 hours', '+30 minutes'))
                    AS INTEGER) AS weekday_ist,
               CAST(strftime('%H', datetime(captured_at, '+5 hours', '+30 minutes'))
                    AS INTEGER) AS hour_ist,
               SUM(view_growth) AS view_growth,
               SUM(like_growth) AS like_growth,
               SUM(comment_growth) AS comment_growth,
               SUM(share_growth) AS share_growth,
               COUNT(*) AS snapshot_deltas
        FROM deltas
        WHERE elapsed_hours IS NOT NULL
          AND elapsed_hours >= 0.15
          AND elapsed_hours <= 4.0
        GROUP BY 1, 2, 3, 4
    """
    return [dict(row) for row in conn.execute(sql, params)]


def comment_text_sample(
    conn: sqlite3.Connection,
    *,
    horizon: timedelta,
    platform: str | None = None,
    page_key: str | None = None,
    reference_time: datetime | None = None,
    limit: int = 5000,
) -> list[dict]:
    """Return recent retained comment text for local intent classification."""
    reference_time = reference_time or datetime.now(timezone.utc)
    cutoff = (reference_time - horizon).isoformat()
    published_utc = _published_at_utc_expr("published_at")
    timestamp_expr = (
        f"COALESCE(CASE WHEN trim(COALESCE(published_at, '')) <> '' "
        f"AND {published_utc} IS NOT NULL THEN {published_utc} END, created_at)"
    )
    sql = f"""SELECT platform, page_key, text, author, created_at,
               CAST(strftime('%w', datetime({timestamp_expr}, '+5 hours', '+30 minutes'))
                    AS INTEGER) AS weekday_ist,
               CAST(strftime('%H', datetime({timestamp_expr}, '+5 hours', '+30 minutes'))
                    AS INTEGER) AS hour_ist
             FROM comments
             WHERE created_at >= ? AND trim(text) <> ''"""
    params: list = [cutoff]
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if page_key:
        if page_key == DEFAULT_PAGE_KEY:
            sql += " AND page_key IN (?, '')"
        else:
            sql += " AND page_key = ?"
        params.append(page_key)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def recently_active_video_ids(
    conn: sqlite3.Connection,
    *,
    platform: str,
    horizon: timedelta,
    page_key: str | None = None,
    reference_time: datetime | None = None,
) -> list[str]:
    """Video/container IDs with at least one comment seen within `horizon`.

    Polling only ever scans a channel's most recent uploads (see
    app/fetch.py), so a video that's still actively receiving comments long
    after newer videos have been published silently falls out of scope --
    it ages out of the "latest N uploads" window with no way back in short
    of someone manually adding it to YOUTUBE_VIDEO_IDS. This lets polling
    find such videos itself: anything with a comment we've recorded
    recently is still worth re-checking, regardless of upload date.
    """
    reference_time = reference_time or datetime.now(timezone.utc)
    cutoff = (reference_time - horizon).isoformat()
    clause, extra = _page_key_filter(page_key)
    sql = "SELECT DISTINCT video_id FROM comments WHERE platform = ? AND created_at >= ?" + clause
    params: list = [platform, cutoff, *extra]
    return [row["video_id"] for row in conn.execute(sql, params)]


def engagement_totals(
    conn: sqlite3.Connection, *, platform: str, page_key: str, video_id: str | None = None
) -> dict:
    sql = """SELECT SUM(view_count) AS views, SUM(like_count) AS likes,
                    SUM(comment_count) AS comments, SUM(share_count) AS shares
             FROM video_stats WHERE platform = ?"""
    params: list = [platform]
    if page_key == DEFAULT_PAGE_KEY:
        sql += " AND page_key IN (?, '')"
    else:
        sql += " AND page_key = ?"
    params.append(page_key)
    if video_id:
        sql += " AND video_id = ?"
        params.append(video_id)
    row = conn.execute(sql, params).fetchone()
    return {name: row[name] for name in ("views", "likes", "comments", "shares")}


def create_recommendation_experiment(
    conn: sqlite3.Connection,
    *,
    recommendation_key: str,
    recommendation_type: str,
    platform: str,
    page_key: str,
    video_id: str | None,
    title: str,
    recommendation: str,
    test_dimension: str = "",
    variant_label: str = "",
    notes: str = "",
) -> bool:
    baseline = engagement_totals(
        conn, platform=platform, page_key=page_key, video_id=video_id
    )
    timestamp = now()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO recommendation_experiments
               (recommendation_key, recommendation_type, platform, page_key,
                video_id, title, recommendation, test_dimension, variant_label,
                notes, status, baseline_views,
                baseline_likes, baseline_comments, baseline_shares, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?)""",
        (
            recommendation_key, recommendation_type, platform, page_key,
            video_id, title, recommendation, test_dimension or None,
            variant_label or None, notes or None, baseline["views"], baseline["likes"],
            baseline["comments"], baseline["shares"], timestamp, timestamp,
        ),
    )
    return cursor.rowcount == 1


def update_recommendation_experiment_status(
    conn: sqlite3.Connection, experiment_id: int, status: str
) -> bool:
    if status not in {"accepted", "ignored", "completed"}:
        return False
    cursor = conn.execute(
        "UPDATE recommendation_experiments SET status = ?, updated_at = ? WHERE id = ?",
        (status, now(), experiment_id),
    )
    return cursor.rowcount == 1


def list_recommendation_experiments(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    page_key: str | None = None,
    limit: int = 50,
) -> list[dict]:
    sql = "SELECT * FROM recommendation_experiments WHERE 1=1"
    params: list = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if page_key:
        if page_key == DEFAULT_PAGE_KEY:
            sql += " AND page_key IN (?, '')"
        else:
            sql += " AND page_key = ?"
        params.append(page_key)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    experiments = []
    for row in conn.execute(sql, params):
        item = dict(row)
        current = engagement_totals(
            conn, platform=item["platform"], page_key=item["page_key"],
            video_id=item["video_id"],
        )
        for metric in ("views", "likes", "comments", "shares"):
            baseline = item[f"baseline_{metric}"]
            value = current[metric]
            item[f"current_{metric}"] = value
            item[f"delta_{metric}"] = (
                max(0, value - baseline) if value is not None and baseline is not None else None
            )
        experiments.append(item)
    return experiments


VIDEO_STATS_SORT_COLUMNS = {
    "recent": "last_comment_at",
    "views": "vs.view_count",
    "likes": "vs.like_count",
    "comments": "vs.comment_count",
    "shares": "vs.share_count",
    "updated": "vs.updated_at",
}


VIDEO_STATS_WINDOWS = {
    "1h": ("1 Hour", timedelta(hours=1)),
    "24h": ("24 Hours", timedelta(hours=24)),
    "7d": ("7 Day", timedelta(days=7)),
    "365d": ("365 Days", timedelta(days=365)),
}


def get_cached_video_title(
    conn: sqlite3.Connection, *, platform: str, video_id: str
) -> str | None:
    """Look up a video's title from our own storage before spending YouTube
    API quota on it -- a video's title essentially never changes once
    published, so re-fetching it every poll cycle forever is pure waste."""
    row = conn.execute(
        "SELECT video_title FROM video_stats WHERE platform = ? AND video_id = ? "
        "AND video_title IS NOT NULL AND video_title <> ''",
        (platform, video_id),
    ).fetchone()
    if row:
        return row["video_title"]
    row = conn.execute(
        "SELECT video_title FROM comments WHERE platform = ? AND video_id = ? "
        "AND video_title IS NOT NULL AND video_title <> '' LIMIT 1",
        (platform, video_id),
    ).fetchone()
    return row["video_title"] if row else None


def list_video_stats(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    page_key: str | None = None,
    sort_by: str = "recent",
    sort_dir: str = "desc",
    updated_within: timedelta | None = None,
    reference_time: datetime | None = None,
    limit: int = 200,
) -> list[dict]:
    """Return cached engagement counts, sorted by sort_by/sort_dir (most
    recently commented-on first by default). NULLS LAST so videos with no
    count yet (not first, not last, always yet to be measured) don't crowd
    out ones with real numbers when sorting by likes/comments/shares.

    updated_within restricts to rows whose stats were last refreshed inside
    that window -- a freshness filter, not real historical engagement
    growth, since video_stats only ever stores each video's latest snapshot.
    """
    column = VIDEO_STATS_SORT_COLUMNS.get(sort_by, VIDEO_STATS_SORT_COLUMNS["recent"])
    direction = "ASC" if sort_dir == "asc" else "DESC"
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
    if updated_within is not None:
        reference_time = reference_time or datetime.now(timezone.utc)
        cutoff = reference_time - updated_within
        sql += " AND datetime(vs.updated_at) >= datetime(?)"
        params.append(cutoff.isoformat())
    sql += f"""
        GROUP BY vs.platform, vs.video_id
        ORDER BY {column} IS NULL, {column} {direction}, vs.updated_at DESC
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
                  is_admin, created_at, updated_at
           FROM dashboard_users WHERE username = ? COLLATE NOCASE""",
        (username.strip(),),
    ).fetchone()


def list_dashboard_users(conn: sqlite3.Connection):
    return conn.execute(
        """SELECT id, username, display_name, is_admin
           FROM dashboard_users ORDER BY username COLLATE NOCASE"""
    ).fetchall()


def ensure_dashboard_admin(conn: sqlite3.Connection, username: str) -> None:
    """Idempotently flag `username`'s account as the portal admin.

    Called on every startup so the original bootstrap account (from
    DASHBOARD_USERNAME) keeps admin rights even after upgrading an
    existing install where the is_admin column didn't exist yet.
    """
    if not username:
        return
    conn.execute(
        "UPDATE dashboard_users SET is_admin = 1 WHERE username = ? COLLATE NOCASE",
        (username.strip(),),
    )


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
    author_id: str = "",
) -> None:
    # video_id/video_title double as the generic "container" id/title for
    # non-YouTube platforms (Facebook post id/message, Instagram media id/caption).
    # page_key identifies which configured page/channel (see config.PAGES)
    # this comment belongs to, across all three platforms.
    # author_id is the platform's stable commenter id (Meta from.id / YouTube
    # authorChannelId) -- used to group a commenter's activity even if they
    # later change their display name. It's '' for older rows and any
    # source that doesn't expose one.
    ts = now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO comments (
            comment_id, platform, page_key, video_id, video_title, author,
            author_id, text,
            published_at, status, draft_reply, reply_checked_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)
        """,
        (
            comment_id,
            platform,
            page_key,
            video_id,
            video_title,
            author,
            author_id,
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


def top_fans(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    page_key: str | None = None,
    reference_time: datetime | None = None,
    limit: int = 5,
) -> dict[str, list[dict]]:
    """Return each window's most active commenters, per platform/page.

    Grouped by author_id (the platform's stable commenter id) so a
    commenter's count survives a display-name change; rows written before
    author_id was captured fall back to grouping by author name. week/month/
    year windows are all computed together so the dashboard can switch
    between them without a reload, matching activity_summary().
    """
    reference_time = reference_time or datetime.now(timezone.utc)
    windows = (
        ("week", timedelta(days=7)),
        ("month", timedelta(days=30)),
        ("year", timedelta(days=365)),
    )
    platform_filter = "AND platform = :platform" if platform else ""
    if page_key == DEFAULT_PAGE_KEY:
        page_key_filter = "AND page_key IN (:page_key, '')"
    elif page_key:
        page_key_filter = "AND page_key = :page_key"
    else:
        page_key_filter = ""
    published_utc = _published_at_utc_expr("published_at")
    sql = f"""
        WITH filtered AS (
            SELECT
                COALESCE(NULLIF(author_id, ''), 'name:' || author) AS fan_key,
                author, platform, page_key,
                {published_utc} AS published_utc
            FROM comments
            WHERE trim(COALESCE(author, '')) <> ''
              AND {published_utc} >= datetime(:cutoff)
              {platform_filter} {page_key_filter}
        ),
        ranked AS (
            SELECT
                author, platform, page_key,
                COUNT(*) OVER (PARTITION BY fan_key, platform, page_key)
                    AS comment_count,
                MAX(published_utc) OVER (PARTITION BY fan_key, platform, page_key)
                    AS last_comment_at,
                ROW_NUMBER() OVER (
                    PARTITION BY fan_key, platform, page_key
                    ORDER BY published_utc DESC
                ) AS rn
            FROM filtered
        )
        SELECT author, platform, page_key, comment_count,
               strftime('%Y-%m-%d %H:%M', last_comment_at, '+5 hours', '+30 minutes')
                   AS last_comment_at_ist
        FROM ranked
        WHERE rn = 1
        ORDER BY comment_count DESC, last_comment_at DESC
        LIMIT :limit
    """
    results: dict[str, list[dict]] = {}
    for label, duration in windows:
        params = {"cutoff": (reference_time - duration).isoformat(), "limit": limit}
        if platform:
            params["platform"] = platform
        if page_key:
            params["page_key"] = page_key
        rows = conn.execute(sql, params).fetchall()
        results[label] = [
            {
                "rank": i + 1,
                "author": row["author"],
                "platform": row["platform"],
                "page_key": row["page_key"],
                "comment_count": row["comment_count"],
                "last_comment_at": row["last_comment_at_ist"],
            }
            for i, row in enumerate(rows)
        ]
    return results


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
