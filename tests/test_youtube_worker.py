import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from app import db, youtube_worker
from tests.helpers import make_page_config


class YouTubeWorkerTests(unittest.TestCase):
    def setUp(self):
        # run_cycle() records a heartbeat via db.connect() -- isolate it
        # from the real local comments.db like every other test does,
        # rather than writing test artifacts into real data.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        self.pages = {
            "hindolroad": make_page_config(key="hindolroad", label="Hindolroad"),
            "travel": make_page_config(key="travel", label="Travel Explorer Satya"),
        }

    def test_cycle_polls_and_publishes_each_channel(self):
        with patch.object(youtube_worker.config, "PAGES", self.pages), \
             patch.object(youtube_worker.config, "youtube_page_keys", return_value=list(self.pages)), \
             patch.object(youtube_worker, "poll_and_draft", side_effect=[2, 3]) as poll, \
             patch.object(youtube_worker, "post_approved", side_effect=[2, 3]) as post, \
             patch("sys.stdout", new=io.StringIO()):
            failures = youtube_worker.run_cycle()

        self.assertEqual(failures, 0)
        self.assertEqual(poll.call_args_list, [call(page_key="hindolroad"), call(page_key="travel")])
        self.assertEqual(
            [item.kwargs["page_key"] for item in post.call_args_list],
            ["hindolroad", "travel"],
        )

    def test_poll_failure_does_not_block_later_channel(self):
        with patch.object(youtube_worker.config, "PAGES", self.pages), \
             patch.object(youtube_worker.config, "youtube_page_keys", return_value=list(self.pages)), \
             patch.object(youtube_worker, "poll_and_draft", side_effect=[RuntimeError("bad token"), 4]) as poll, \
             patch.object(youtube_worker, "post_approved", return_value=4) as post, \
             patch("sys.stdout", new=io.StringIO()):
            failures = youtube_worker.run_cycle()

        self.assertEqual(failures, 1)
        self.assertEqual(poll.call_count, 2)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["page_key"], "travel")

    def test_publish_failure_does_not_block_later_channel(self):
        with patch.object(youtube_worker.config, "PAGES", self.pages), \
             patch.object(youtube_worker.config, "youtube_page_keys", return_value=list(self.pages)), \
             patch.object(youtube_worker, "poll_and_draft", return_value=1), \
             patch.object(youtube_worker, "post_approved", side_effect=[RuntimeError("quota"), 1]) as post, \
             patch("sys.stdout", new=io.StringIO()):
            failures = youtube_worker.run_cycle()

        self.assertEqual(failures, 1)
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
