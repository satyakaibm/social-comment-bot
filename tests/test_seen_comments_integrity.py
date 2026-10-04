"""Guards against id-less seen_comments rows inflating dashboard counts.

On 2026-09-15 a bulk insert put 90,728 rows with a NULL comment_id into the
production database (their `status` values were a video's like and comment
counts, not status names). Nothing rejected them: `comment_id` was a bare
TEXT PRIMARY KEY, and SQLite's UNIQUE treats every NULL as distinct, so they
accumulated instead of colliding. Because activity_summary() counts rows in
seen_comments, the dashboard's "Comments found" over 30 days read 92,987
against 1,484 real replies.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db

# The production table, as it stands before this sweep: `comment_id` is a
# bare TEXT PRIMARY KEY and `page_key` was bolted on by ALTER TABLE. The
# SCHEMA now declares NOT NULL, but CREATE TABLE IF NOT EXISTS never alters
# an existing table, so every database created earlier still looks like
# this -- which is exactly the shape the sweep has to cope with.
LEGACY_SEEN_COMMENTS = """
DROP TABLE seen_comments;
CREATE TABLE seen_comments (
    comment_id TEXT PRIMARY KEY,
    platform TEXT,
    status TEXT,
    recorded_at TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
, page_key TEXT);
"""

JUNK_INSERT = """
    INSERT INTO seen_comments
        (comment_id, platform, status, page_key,
         recorded_at, created_at, updated_at)
    VALUES (NULL, 'youtube', ?, '', ?, ?, ?)
"""


class SeenCommentIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_remember_seen_comment_refuses_a_missing_id(self):
        with db.connect() as conn:
            for empty in (None, ""):
                with self.assertRaises(ValueError):
                    db.remember_seen_comment(conn, empty, platform="youtube")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM seen_comments").fetchone()[0], 0
            )

    def test_fresh_schema_rejects_a_row_with_no_comment_id(self):
        ts = db.now()
        with db.connect() as conn:
            with self.assertRaises(Exception) as caught:
                conn.execute(JUNK_INSERT, ("1697", ts, ts, ts))
        self.assertIn("NOT NULL", str(caught.exception))

    def test_init_db_sweeps_rows_that_have_no_comment_id(self):
        ts = db.now()
        with db.connect() as conn:
            conn.executescript(LEGACY_SEEN_COMMENTS)
            # Reproduce the 2026-09-15 shape: no comment_id, and a numeric
            # "status" that is really a video like/comment count.
            conn.executemany(
                JUNK_INSERT, [("1697", ts, ts, ts), ("11680", ts, ts, ts)]
            )
            db.remember_seen_comment(conn, "keep-me", platform="youtube", status="posted")

        with db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM seen_comments").fetchone()[0], 3
            )

        db.init_db()

        with db.connect() as conn:
            rows = conn.execute("SELECT comment_id FROM seen_comments").fetchall()
        # The sweep removes only the id-less rows; real fingerprints survive,
        # which matters because losing one would re-draft a handled comment.
        self.assertEqual([row["comment_id"] for row in rows], ["keep-me"])

    def test_id_less_rows_do_not_inflate_activity_counts(self):
        ts = db.now()
        with db.connect() as conn:
            conn.executescript(LEGACY_SEEN_COMMENTS)
            db.insert_comment(
                conn,
                comment_id="real",
                platform="youtube",
                video_id="video",
                video_title="Title",
                author="viewer",
                text="Jai Maa",
                published_at="",
                draft_reply="Reply",
            )
            conn.executemany(JUNK_INSERT, [(str(n), ts, ts, ts) for n in range(50)])

        reference = datetime.now(timezone.utc) + timedelta(seconds=1)
        with db.connect() as conn:
            before = {
                row["label"]: row["received"]
                for row in db.activity_summary(
                    conn, reference_time=reference, platform="youtube"
                )
            }
        # Without the sweep every window over-reports by the 50 junk rows.
        self.assertEqual(before["24 Hours"], 51)

        db.init_db()

        with db.connect() as conn:
            after = {
                row["label"]: row["received"]
                for row in db.activity_summary(
                    conn, reference_time=reference, platform="youtube"
                )
            }
        self.assertEqual(after["24 Hours"], 1)
        self.assertEqual(after["30 Days"], 1)


if __name__ == "__main__":
    unittest.main()
