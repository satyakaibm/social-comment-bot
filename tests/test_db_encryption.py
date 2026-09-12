import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db

try:
    from sqlcipher3 import dbapi2 as sqlcipher
except ImportError:
    sqlcipher = None


class DbEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "comments.db"
        patcher = patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plaintext_roundtrip_without_key(self):
        with patch.object(config, "DB_ENCRYPTION_KEY", ""):
            db.init_db()
            with db.connect() as conn:
                db.insert_comment(
                    conn,
                    comment_id="c1",
                    video_id="v",
                    video_title="t",
                    author="a",
                    text="hello",
                    published_at="",
                    draft_reply="reply",
                )
            self.assertTrue(db.is_plaintext_sqlite(self.db_path))
            self.assertEqual(stat_mode(self.db_path) & 0o777, 0o600)

    @unittest.skipUnless(sqlcipher, "sqlcipher3-binary is published for Linux (Docker/CI)")
    def test_encrypts_existing_plaintext_and_reads_rows(self):
        with patch.object(config, "DB_ENCRYPTION_KEY", ""):
            db.init_db()
            with db.connect() as conn:
                db.insert_comment(
                    conn,
                    comment_id="c1",
                    video_id="v",
                    video_title="t",
                    author="a",
                    text="secret comment",
                    published_at="",
                    draft_reply="secret reply",
                )
        self.assertTrue(db.is_plaintext_sqlite(self.db_path))

        key = "test-db-encryption-key-32chars!!"
        with patch.object(config, "DB_ENCRYPTION_KEY", key):
            db.init_db()
            self.assertFalse(db.is_plaintext_sqlite(self.db_path))
            with db.connect() as conn:
                row = db.get_comment(conn, "c1")
            self.assertEqual(row["text"], "secret comment")
            self.assertEqual(row["draft_reply"], "secret reply")

        with patch.object(config, "DB_ENCRYPTION_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "encrypted"):
                db.open_connection()

        with patch.object(config, "DB_ENCRYPTION_KEY", "wrong-key-that-is-32-chars-long"):
            with self.assertRaisesRegex(RuntimeError, "Check DB_ENCRYPTION_KEY"):
                db.open_connection()


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode
