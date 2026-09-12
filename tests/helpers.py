"""Shared builders for tests. Keep PageConfig construction in one place."""

from app import config


def make_page_config(**overrides) -> config.PageConfig:
    fields = dict(
        key="second",
        label="Second",
        facebook_page_id="",
        facebook_page_access_token="",
        meta_user_access_token="",
        instagram_user_id="",
        facebook_post_ids=[],
        instagram_media_ids=[],
        facebook_daily_reply_limit=0,
        instagram_daily_reply_limit=0,
        youtube_oauth_client_id="",
        youtube_oauth_client_secret="",
        youtube_refresh_token="",
        youtube_video_ids=[],
        youtube_daily_reply_limit=0,
        persona="",
        persona_dir=config.REPLY_EXAMPLES_DIR,
    )
    fields.update(overrides)
    return config.PageConfig(**fields)
