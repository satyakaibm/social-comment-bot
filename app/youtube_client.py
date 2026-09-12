from google.oauth2.credentials import Credentials
from datetime import datetime
from zoneinfo import ZoneInfo

from googleapiclient.discovery import Resource, build

from app import config, db

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def execute(request, *, units: int = 1):
    """Execute a YouTube request and record quota consumed by this bot."""
    try:
        return request.execute()
    finally:
        period = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        db.add_quota_usage("youtube", period, units, config.YOUTUBE_DAILY_QUOTA_LIMIT)


def is_quota_exceeded(error: Exception) -> bool:
    """Return whether a YouTube API error reports exhausted daily quota."""
    quota_reasons = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
    for detail in getattr(error, "error_details", []) or []:
        if isinstance(detail, dict) and detail.get("reason") in quota_reasons:
            return True
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return any(reason in str(content) for reason in quota_reasons)


def get_client(page_key: str = config.DEFAULT_PAGE_KEY) -> Resource:
    """Build an authenticated client for one configured YouTube channel.

    The default page reads the live top-level YOUTUBE_* constants (not
    config.PAGES[...], which is frozen at import time and wouldn't see a
    test's patch.object(config, "YOUTUBE_REFRESH_TOKEN", ...)); any other
    configured channel reads its own config.PAGES entry.
    """
    if page_key == config.DEFAULT_PAGE_KEY:
        config.require(
            "YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"
        )
        client_id = config.YOUTUBE_OAUTH_CLIENT_ID
        client_secret = config.YOUTUBE_OAUTH_CLIENT_SECRET
        refresh_token = config.YOUTUBE_REFRESH_TOKEN
    else:
        page = config.PAGES[page_key]
        if not (page.youtube_oauth_client_id and page.youtube_oauth_client_secret and page.youtube_refresh_token):
            raise RuntimeError(
                f"Missing YouTube OAuth config for page '{page_key}': set "
                "YOUTUBE_OAUTH_CLIENT_ID/YOUTUBE_OAUTH_CLIENT_SECRET/"
                f"YOUTUBE_REFRESH_TOKEN with this page's suffix in .env."
            )
        client_id = page.youtube_oauth_client_id
        client_secret = page.youtube_oauth_client_secret
        refresh_token = page.youtube_refresh_token
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def get_my_channel_id(youtube: Resource) -> str:
    resp = execute(youtube.channels().list(part="id", mine=True))
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("No channel found for the authenticated account.")
    return items[0]["id"]


def get_video_channel_ids(youtube: Resource, video_ids) -> dict[str, str]:
    """Return each video's owner channel ID, batching lookups to save quota."""
    unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    owners = {}
    for start in range(0, len(unique_ids), 50):
        response = execute(youtube.videos().list(
            part="snippet", id=",".join(unique_ids[start:start + 50])
        ))
        for item in response.get("items", []):
            owners[item["id"]] = item.get("snippet", {}).get("channelId", "")
    return owners


def get_video_stats(youtube: Resource, video_ids) -> dict[str, dict]:
    """Return each video's like/comment counts, batching lookups to save quota.

    YouTube's Data API has no share-count field, so callers only get likes
    and comments back. A video with likes or comments hidden by its owner
    omits that field entirely, which comes through here as None.
    """
    unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    stats: dict[str, dict] = {}
    for start in range(0, len(unique_ids), 50):
        response = execute(youtube.videos().list(
            part="statistics", id=",".join(unique_ids[start:start + 50])
        ))
        for item in response.get("items", []):
            counts = item.get("statistics", {})
            stats[item["id"]] = {
                "like_count": int(counts["likeCount"]) if "likeCount" in counts else None,
                "comment_count": int(counts["commentCount"]) if "commentCount" in counts else None,
            }
    return stats


def find_own_reply(
    youtube: Resource,
    comment_id: str,
    channel_ids: str | set[str],
) -> str | None:
    """Search every reply page for a reply from any known channel identity."""
    own_ids = {channel_ids} if isinstance(channel_ids, str) else set(channel_ids)
    own_ids.discard("")
    request = youtube.comments().list(
        part="snippet", parentId=comment_id, maxResults=100, textFormat="plainText"
    )
    while request is not None:
        response = execute(request)
        for reply in response["items"]:
            author_channel = reply.get("snippet", {}).get("authorChannelId", {})
            author_channel_id = (
                author_channel.get("value")
                if isinstance(author_channel, dict)
                else author_channel
            )
            if author_channel_id in own_ids:
                return reply["id"]
        request = youtube.comments().list_next(request, response)
    return None


def get_uploads_playlist_id(youtube: Resource) -> str:
    resp = execute(youtube.channels().list(part="contentDetails", mine=True))
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("No channel found for the authenticated account.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def iter_uploaded_video_ids(youtube: Resource, playlist_id: str):
    request = youtube.playlistItems().list(
        part="contentDetails", playlistId=playlist_id, maxResults=50
    )
    while request is not None:
        response = execute(request)
        for item in response.get("items", []):
            yield item["contentDetails"]["videoId"]
        request = youtube.playlistItems().list_next(request, response)
