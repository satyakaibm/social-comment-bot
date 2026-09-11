import unittest
from unittest.mock import patch

from app import config, youtube_client
from app.youtube_client import is_quota_exceeded


class YouTubeClientTests(unittest.TestCase):
    def test_detects_quota_reason_from_error_details(self):
        error = Exception("request failed")
        error.error_details = [{"reason": "quotaExceeded"}]
        self.assertTrue(is_quota_exceeded(error))

    def test_does_not_treat_other_errors_as_quota_exhaustion(self):
        error = Exception("request failed")
        error.error_details = [{"reason": "forbidden"}]
        self.assertFalse(is_quota_exceeded(error))


class GetClientTests(unittest.TestCase):
    def _second_page(self, **overrides):
        fields = dict(
            key='second', label='Second', facebook_page_id='', facebook_page_access_token='',
            meta_user_access_token='', instagram_user_id='', facebook_post_ids=[],
            instagram_media_ids=[], facebook_daily_reply_limit=0, instagram_daily_reply_limit=0,
            youtube_oauth_client_id='client-2', youtube_oauth_client_secret='secret-2',
            youtube_refresh_token='refresh-2', youtube_video_ids=[], youtube_daily_reply_limit=0,
            persona='', persona_dir=config.REPLY_EXAMPLES_DIR,
        )
        fields.update(overrides)
        return config.PageConfig(**fields)

    def test_default_page_reads_live_top_level_config(self):
        # Regression: config.PAGES[DEFAULT_PAGE_KEY] is frozen at import time
        # and would not see this patch -- the default page must keep reading
        # the live top-level YOUTUBE_* constants directly.
        with patch.object(config, 'YOUTUBE_OAUTH_CLIENT_ID', 'live-id'), \
             patch.object(config, 'YOUTUBE_OAUTH_CLIENT_SECRET', 'live-secret'), \
             patch.object(config, 'YOUTUBE_REFRESH_TOKEN', 'live-refresh'), \
             patch.object(youtube_client, 'Credentials') as creds, \
             patch.object(youtube_client, 'build') as build:
            youtube_client.get_client()

        creds.assert_called_once_with(
            token=None, refresh_token='live-refresh', token_uri=youtube_client.TOKEN_URI,
            client_id='live-id', client_secret='live-secret', scopes=youtube_client.SCOPES,
        )
        build.assert_called_once()

    def test_second_channel_reads_its_own_page_config_not_the_default(self):
        second = self._second_page()
        with patch.object(config, 'PAGES', {**config.PAGES, 'second': second}), \
             patch.object(youtube_client, 'Credentials') as creds, \
             patch.object(youtube_client, 'build') as build:
            youtube_client.get_client(page_key='second')

        creds.assert_called_once_with(
            token=None, refresh_token='refresh-2', token_uri=youtube_client.TOKEN_URI,
            client_id='client-2', client_secret='secret-2', scopes=youtube_client.SCOPES,
        )
        build.assert_called_once()

    def test_second_channel_missing_oauth_config_raises_clearly(self):
        second = self._second_page(youtube_refresh_token='')
        with patch.object(config, 'PAGES', {**config.PAGES, 'second': second}):
            with self.assertRaisesRegex(RuntimeError, 'second'):
                youtube_client.get_client(page_key='second')
