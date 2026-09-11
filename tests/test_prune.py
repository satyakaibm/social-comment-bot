import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db


def _insert(conn, comment_id: str, *, platform: str = "youtube") -> None:
    db.insert_comment(
        conn,
        comment_id=comment_id,
        platform=platform,
        video_id="video",
        video_title="Title",
        author="viewer",
        text="Jai Maa",
        published_at="",
        draft_reply="Reply",
    )


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_insert_records_seen_comment_id(self):
        with db.connect() as conn:
            _insert(conn, "c1")
            seen = conn.execute(
                "SELECT comment_id, status FROM seen_comments WHERE comment_id = 'c1'"
            ).fetchone()
        self.assertEqual(seen["comment_id"], "c1")
        self.assertEqual(seen["status"], "pending_review")

    def test_prune_removes_handled_rows_but_keeps_ids(self):
        with db.connect() as conn:
            _insert(conn, "posted")
            _insert(conn, "replied")
            _insert(conn, "rejected")
            _insert(conn, "pending")
            _insert(conn, "approved")
            _insert(conn, "failed")
            db.update_status(conn, "posted", "posted")
            db.update_status(conn, "replied", "already_replied")
            db.update_status(conn, "rejected", "rejected")
            db.update_status(conn, "approved", "approved")
            db.update_status(conn, "failed", "failed")
            conn.execute(
                """INSERT INTO webhook_events
                   (event_key, platform, payload, status, created_at, updated_at)
                   VALUES ('instagram:posted', 'instagram', '{}', 'processed', ?, ?),
                          ('instagram:open', 'instagram', '{}', 'pending', ?, ?)""",
                (db.now(), db.now(), db.now(), db.now()),
            )
            result = db.prune_storage(conn)

        self.assertEqual(result["comments_deleted"], 3)
        self.assertEqual(result["webhooks_deleted"], 1)

        with db.connect() as conn:
            remaining = {
                row["comment_id"]
                for row in conn.execute("SELECT comment_id FROM comments")
            }
            self.assertEqual(remaining, {"pending", "approved", "failed"})
            for comment_id in ("posted", "replied", "rejected", "pending"):
                self.assertTrue(db.comment_exists(conn, comment_id))
            self.assertIsNone(db.get_comment(conn, "posted"))
            webhook_statuses = {
                row["status"]
                for row in conn.execute("SELECT status FROM webhook_events")
            }
            self.assertEqual(webhook_statuses, {"pending"})

    def test_older_than_days_keeps_recent_handled_comments(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with db.connect() as conn:
            _insert(conn, "old_posted")
            _insert(conn, "new_posted")
            db.update_status(conn, "old_posted", "posted")
            db.update_status(conn, "new_posted", "posted")
            conn.execute(
                "UPDATE comments SET updated_at = ? WHERE comment_id = 'old_posted'",
                (old,),
            )
            result = db.prune_storage(conn, older_than_days=7)

        self.assertEqual(result["comments_deleted"], 1)
        with db.connect() as conn:
            self.assertIsNone(db.get_comment(conn, "old_posted"))
            self.assertIsNotNone(db.get_comment(conn, "new_posted"))
            self.assertTrue(db.comment_exists(conn, "old_posted"))

    def test_existing_db_backfills_seen_comments_on_init(self):
        with db.connect() as conn:
            _insert(conn, "legacy")
            conn.execute("DELETE FROM seen_comments")
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM seen_comments WHERE comment_id = 'legacy'"
                ).fetchone()
            )
        db.init_db()
        with db.connect() as conn:
            seen = conn.execute(
                "SELECT comment_id FROM seen_comments WHERE comment_id = 'legacy'"
            ).fetchone()
            self.assertEqual(seen["comment_id"], "legacy")
            self.assertIsNotNone(db.get_comment(conn, "legacy"))

    def test_page_key_migration_backfills_every_platform(self):
        # Simulate a DB created before multi-page support: same tables, but
        # without the page_key column at all, as every real DB predating
        # this feature looks like.
        legacy_path = Path(self.temp.name) / "legacy.db"
        raw = sqlite3.connect(legacy_path)
        raw.executescript(
            """
            CREATE TABLE comments (
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
            CREATE TABLE seen_comments (
                comment_id TEXT PRIMARY KEY,
                platform TEXT,
                status TEXT,
                recorded_at TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        ts = db.now()
        raw.executemany(
            """INSERT INTO comments
                   (comment_id, platform, video_id, video_title, author, text,
                    published_at, status, draft_reply, created_at, updated_at)
               VALUES (?, ?, 'container', 'Title', 'viewer', 'text', '',
                       'posted', '🙏', ?, ?)""",
            [
                ("fb1", "facebook", ts, ts),
                ("ig1", "instagram", ts, ts),
                ("yt1", "youtube", ts, ts),
            ],
        )
        raw.commit()
        raw.close()

        with patch.object(db, "DB_PATH", legacy_path):
            db.init_db()
            with db.connect() as conn:
                columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(comments)")
                }
                self.assertIn("page_key", columns)
                comment_page_keys = {
                    row["comment_id"]: row["page_key"]
                    for row in conn.execute("SELECT comment_id, page_key FROM comments")
                }
                seen_page_keys = {
                    row["comment_id"]: row["page_key"]
                    for row in conn.execute(
                        "SELECT comment_id, page_key FROM seen_comments"
                    )
                }
            # Backfilled to the only page/channel that could possibly have
            # existed at migration time, across every platform including
            # YouTube (now page-aware too, for multi-channel support).
            self.assertEqual(comment_page_keys["fb1"], db.DEFAULT_PAGE_KEY)
            self.assertEqual(comment_page_keys["ig1"], db.DEFAULT_PAGE_KEY)
            self.assertEqual(comment_page_keys["yt1"], db.DEFAULT_PAGE_KEY)
            # sync_seen_stats_from_comments mirrors the same page_key into
            # seen_comments so page attribution survives a later prune.
            self.assertEqual(seen_page_keys["fb1"], db.DEFAULT_PAGE_KEY)
            self.assertEqual(seen_page_keys["ig1"], db.DEFAULT_PAGE_KEY)
            self.assertEqual(seen_page_keys["yt1"], db.DEFAULT_PAGE_KEY)

            # Idempotent: a second init_db() on an already-migrated DB must
            # not error or re-run the backfill in a way that changes anything.
            db.init_db()
            with db.connect() as conn:
                unchanged = {
                    row["comment_id"]: row["page_key"]
                    for row in conn.execute("SELECT comment_id, page_key FROM comments")
                }
            self.assertEqual(unchanged, comment_page_keys)

    def test_prune_keeps_activity_counts_from_seen_timestamps(self):
        created = "2026-09-07T10:00:00+00:00"
        posted_at = "2026-09-07T11:00:00+00:00"
        with db.connect() as conn:
            _insert(conn, "posted")
            db.update_status(conn, "posted", "posted")
            conn.execute(
                "UPDATE comments SET created_at = ?, updated_at = ? WHERE comment_id = 'posted'",
                (created, posted_at),
            )
            db.sync_seen_stats_from_comments(conn)
            db.prune_storage(conn)
            activity = db.activity_summary(
                conn,
                reference_time=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(activity[1]["label"], "24 Hours")
        self.assertEqual(activity[1]["received"], 1)
        self.assertEqual(activity[1]["posted"], 1)
        self.assertEqual(activity[1]["handled"], 1)

    def test_import_stats_from_backup_restores_counts(self):
        backup = Path(self.temp.name) / "comments.db.bak"
        with sqlite3.connect(backup) as bak:
            bak.executescript(db.SCHEMA)
            bak.execute(
                """INSERT INTO comments (
                    comment_id, platform, video_id, video_title, author, text,
                    published_at, status, draft_reply, created_at, updated_at
                ) VALUES (
                    'bak1', 'instagram', 'media', 'Caption', 'viewer', 'Jai Maa',
                    '', 'posted', '🙏', '2026-09-07T10:00:00+00:00',
                    '2026-09-07T11:00:00+00:00'
                )"""
            )
            bak.commit()
        imported = db.import_seen_stats_from_backup(backup)
        self.assertEqual(imported, 1)
        with db.connect() as conn:
            self.assertTrue(db.comment_exists(conn, "bak1"))
            self.assertIsNone(db.get_comment(conn, "bak1"))
            activity = db.activity_summary(
                conn,
                reference_time=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
                platform="instagram",
            )
        self.assertEqual(activity[1]["received"], 1)
        self.assertEqual(activity[1]["posted"], 1)
