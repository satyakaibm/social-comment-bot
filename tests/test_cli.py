import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from app import cli

from tests.helpers import make_page_config


def _pages(*keys):
    return {key: make_page_config(key=key, label=key.title()) for key in keys}


class CliPollTests(unittest.TestCase):
    def test_poll_visits_every_configured_youtube_channel(self):
        with patch.object(cli.config, "PAGES", _pages("hindolroad", "travel")), \
             patch.object(cli.config, "youtube_page_keys", return_value=["hindolroad", "travel"]), \
             patch.object(cli, "_poll_youtube_channel", return_value=1) as poll:
            cli.cmd_poll(SimpleNamespace(page=None))

        self.assertEqual(poll.call_args_list, [call("hindolroad"), call("travel")])

    def test_poll_page_flag_limits_to_one_channel(self):
        with patch.object(cli.config, "PAGES", _pages("hindolroad", "travel")), \
             patch.object(cli, "_poll_youtube_channel", return_value=2) as poll:
            cli.cmd_poll(SimpleNamespace(page="travel"))

        poll.assert_called_once_with("travel")

    def test_poll_all_visits_each_platform_page_and_continues_after_a_failure(self):
        pages = _pages("hindolroad", "travel", "meta")

        def fail_first_youtube(page_key):
            if page_key == "hindolroad":
                raise RuntimeError("quota")
            return 1

        with patch.object(cli.config, "PAGES", pages), \
             patch.object(cli.config, "youtube_page_keys", return_value=["hindolroad", "travel"]), \
             patch.object(cli.config, "facebook_page_keys", return_value=["meta"]), \
             patch.object(cli.config, "instagram_page_keys", return_value=["meta"]), \
             patch.object(cli, "_poll_youtube_channel", side_effect=fail_first_youtube) as yt, \
             patch.object(cli, "poll_facebook_and_draft", return_value=2) as fb, \
             patch.object(cli, "poll_instagram_and_draft", return_value=3) as ig:
            cli.cmd_poll_all(None)

        self.assertEqual([call.args[0] for call in yt.call_args_list], ["hindolroad", "travel"])
        fb.assert_called_once_with(page_key="meta")
        ig.assert_called_once_with(page_key="meta")
