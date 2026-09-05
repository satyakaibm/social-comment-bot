from googleapiclient.errors import HttpError

from app import config, db
from app.generate import draft_reply
from app.youtube_client import (
    get_client,
    get_my_channel_id,
    get_uploads_playlist_id,
    iter_uploaded_video_ids,
)


def _iter_top_level_threads(youtube, *, video_id: str):
    request = youtube.commentThreads().list(
        part="snippet",
        videoId=video_id,
        maxResults=100,
        order="time",
        textFormat="plainText",
    )
    while request is not None:
        response = request.execute()
        yield from response.get("items", [])
        request = youtube.commentThreads().list_next(request, response)


def _video_title_cache(youtube):
    cache: dict[str, str] = {}

    def get(video_id: str) -> str:
        if video_id not in cache:
            resp = youtube.videos().list(part="snippet", id=video_id).execute()
            items = resp.get("items", [])
            cache[video_id] = items[0]["snippet"]["title"] if items else video_id
        return cache[video_id]

    return get


def poll_and_draft() -> int:
    """Fetch new top-level comments and draft replies for them.

    Returns the number of new comments queued for review.
    """
    db.init_db()
    youtube = get_client()
    channel_id = get_my_channel_id(youtube)
    video_title = _video_title_cache(youtube)

    video_ids = config.YOUTUBE_VIDEO_IDS
    if not video_ids:
        uploads_playlist_id = get_uploads_playlist_id(youtube)
        video_ids = list(iter_uploaded_video_ids(youtube, uploads_playlist_id))

    new_count = 0

    with db.connect() as conn:
        for video_id in video_ids:
            try:
                threads = _iter_top_level_threads(youtube, video_id=video_id)
                for thread in threads:
                    top = thread["snippet"]["topLevelComment"]
                    comment_id = top["id"]
                    snippet = top["snippet"]

                    if db.comment_exists(conn, comment_id):
                        continue
                    if snippet.get("authorChannelId", {}).get("value") == channel_id:
                        continue  # don't reply to ourselves

                    actual_video_id = snippet["videoId"]
                    text = snippet.get("textDisplay", "")
                    author = snippet.get("authorDisplayName", "someone")
                    title = video_title(actual_video_id)

                    try:
                        reply = draft_reply(
                            video_title=title, author=author, comment_text=text
                        )
                    except Exception as e:
                        print(f"Failed to draft reply for comment {comment_id}: {e}")
                        continue

                    db.insert_comment(
                        conn,
                        comment_id=comment_id,
                        video_id=actual_video_id,
                        video_title=title,
                        author=author,
                        text=text,
                        published_at=snippet.get("publishedAt", ""),
                        draft_reply=reply,
                    )
                    conn.commit()  # persist each draft immediately so a later
                    # failure in this batch can't roll back already-drafted replies
                    new_count += 1
            except HttpError as e:
                print(f"YouTube API error while polling video_id={video_id!r}: {e}")

    return new_count
