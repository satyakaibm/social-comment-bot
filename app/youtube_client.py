from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from app import config

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def get_client() -> Resource:
    config.require(
        "YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"
    )
    creds = Credentials(
        token=None,
        refresh_token=config.YOUTUBE_REFRESH_TOKEN,
        token_uri=TOKEN_URI,
        client_id=config.YOUTUBE_OAUTH_CLIENT_ID,
        client_secret=config.YOUTUBE_OAUTH_CLIENT_SECRET,
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def get_my_channel_id(youtube: Resource) -> str:
    resp = youtube.channels().list(part="id", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("No channel found for the authenticated account.")
    return items[0]["id"]


def get_uploads_playlist_id(youtube: Resource) -> str:
    resp = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("No channel found for the authenticated account.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def iter_uploaded_video_ids(youtube: Resource, playlist_id: str):
    request = youtube.playlistItems().list(
        part="contentDetails", playlistId=playlist_id, maxResults=50
    )
    while request is not None:
        response = request.execute()
        for item in response.get("items", []):
            yield item["contentDetails"]["videoId"]
        request = youtube.playlistItems().list_next(request, response)
