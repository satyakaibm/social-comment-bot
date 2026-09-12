import os
import unittest
from unittest.mock import patch

from app import config


class BuildPageConfigTests(unittest.TestCase):
    def test_youtube_only_page_builds_when_refresh_token_and_key_are_set(self):
        env = {
            "YOUTUBE_REFRESH_TOKEN_2": "refresh-2",
            "YOUTUBE_OAUTH_CLIENT_ID_2": "client-2",
            "YOUTUBE_OAUTH_CLIENT_SECRET_2": "secret-2",
            "PAGE_KEY_2": "travel",
            "PAGE_LABEL_2": "Travel Explorer",
            "YOUTUBE_VIDEO_IDS_2": "vid-a, vid-b",
            "YOUTUBE_DAILY_REPLY_LIMIT_2": "12",
        }
        with patch.dict(os.environ, env, clear=False):
            page = config._build_page_config("_2")

        self.assertIsNotNone(page)
        self.assertEqual(page.key, "travel")
        self.assertEqual(page.label, "Travel Explorer")
        self.assertEqual(page.youtube_refresh_token, "refresh-2")
        self.assertEqual(page.youtube_video_ids, ["vid-a", "vid-b"])
        self.assertEqual(page.youtube_daily_reply_limit, 12)
        self.assertEqual(page.facebook_page_id, "")
        self.assertEqual(page.persona_dir, config.REPLY_EXAMPLES_DIR / "travel")

    def test_unconfigured_suffix_returns_none(self):
        with patch.dict(
            os.environ,
            {"YOUTUBE_REFRESH_TOKEN_5": "", "FACEBOOK_PAGE_ID_5": "", "INSTAGRAM_USER_ID_5": ""},
            clear=False,
        ):
            self.assertIsNone(config._build_page_config("_5"))

    def test_suffixed_page_requires_page_key(self):
        with patch.dict(
            os.environ,
            {"YOUTUBE_REFRESH_TOKEN_2": "refresh-2", "PAGE_KEY_2": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "PAGE_KEY_2"):
                config._build_page_config("_2")


class PageKeyLookupTests(unittest.TestCase):
    def test_platform_key_helpers_only_include_configured_identities(self):
        from tests.helpers import make_page_config

        pages = {
            "hindolroad": make_page_config(
                key="hindolroad",
                label="Hindolroad",
                facebook_page_id="fb-1",
                instagram_user_id="ig-1",
                youtube_refresh_token="yt-1",
            ),
            "travel": make_page_config(
                key="travel",
                label="Travel",
                youtube_refresh_token="yt-2",
            ),
            "meta_only": make_page_config(
                key="meta_only",
                label="Meta Only",
                facebook_page_id="fb-2",
                instagram_user_id="ig-2",
            ),
        }
        with patch.object(config, "PAGES", pages):
            self.assertEqual(config.youtube_page_keys(), ["hindolroad", "travel"])
            self.assertEqual(config.facebook_page_keys(), ["hindolroad", "meta_only"])
            self.assertEqual(config.instagram_page_keys(), ["hindolroad", "meta_only"])

    def test_resolve_page_key_matches_facebook_entry_id(self):
        with patch.object(config, "PAGES_BY_FACEBOOK_ID", {"111": "travel"}):
            self.assertEqual(config.resolve_page_key("facebook", "111"), "travel")

    def test_resolve_page_key_returns_none_when_unmatched_and_multiple_pages(self):
        from tests.helpers import make_page_config

        pages = {
            "hindolroad": make_page_config(key="hindolroad"),
            "travel": make_page_config(key="travel"),
        }
        with patch.object(config, "PAGES", pages), \
             patch.object(config, "PAGES_BY_FACEBOOK_ID", {}):
            self.assertIsNone(config.resolve_page_key("facebook", "unknown"))

    def test_resolve_page_key_falls_back_when_only_one_page_exists(self):
        from tests.helpers import make_page_config

        pages = {"hindolroad": make_page_config(key="hindolroad")}
        with patch.object(config, "PAGES", pages), \
             patch.object(config, "PAGES_BY_FACEBOOK_ID", {}), \
             patch.object(config, "DEFAULT_PAGE_KEY", "hindolroad"):
            self.assertEqual(config.resolve_page_key("facebook", ""), "hindolroad")
