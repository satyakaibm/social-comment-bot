from datetime import timedelta
from itertools import islice

from googleapiclient.errors import HttpError

from app import config, db
from app.comment_age import is_within_comment_age_limit
from app.generate import draft_reply
from app.youtube_client import (
    find_own_reply,
    get_client,
    get_my_channel_id,
    get_uploads_playlist_id,
    is_quota_exceeded,
    iter_uploaded_video_ids,
    execute,
)


def _iter_top_level_threads(
    youtube, *, video_id: str, limit: int | None = None,
    page_key: str = config.DEFAULT_PAGE_KEY,
):
    request = youtube.commentThreads().list(
        part="snippet",
        videoId=video_id,
        maxResults=max(1, min(limit or 100, 100)),
        order="time",
        textFormat="plainText",
    )
    count = 0
    while request is not None:
        response = execute(request, page_key=page_key)
        for item in response.get("items", []):
            if limit is not None and count >= limit:
                return
            yield item
            count += 1
            if limit is not None and count >= limit:
                return
        request = youtube.commentThreads().list_next(request, response)


def _video_title_cache(youtube, conn, page_key: str = config.DEFAULT_PAGE_KEY):
    cache: dict[str, str] = {}

    def get(video_id: str) -> str:
        if video_id in cache:
            return cache[video_id]
        # A video's title never changes once published, so check our own
        # storage before spending API quota re-fetching it every cycle --
        # each cycle used to pay 1 unit per video, forever, for a value
        # that's almost always already sitting in our database.
        title = db.get_cached_video_title(conn, platform="youtube", video_id=video_id)
        if title is None:
            resp = execute(
                youtube.videos().list(part="snippet", id=video_id), page_key=page_key
            )
            items = resp.get("items", [])
            title = items[0]["snippet"]["title"] if items else video_id
        cache[video_id] = title
        return title

    return get


def poll_and_draft(page_key: str = config.DEFAULT_PAGE_KEY) -> int:
    """Fetch new top-level comments and draft replies for page_key's channel.

    Returns the number of new comments queued for review.
    """
    db.init_db()
    youtube = get_client(page_key=page_key)
    channel_id = get_my_channel_id(youtube, page_key=page_key)

    # Configured IDs are additive. Always include the latest uploads so an old
    # fixed ID can never silently disable discovery of new video comments.
    uploads_playlist_id = get_uploads_playlist_id(youtube, page_key=page_key)
    latest_video_ids = list(islice(
        iter_uploaded_video_ids(youtube, uploads_playlist_id, page_key=page_key),
        max(0, config.YOUTUBE_VIDEO_LIMIT),
    ))
    configured_video_ids = (
        config.YOUTUBE_VIDEO_IDS
        if page_key == config.DEFAULT_PAGE_KEY
        else config.PAGES[page_key].youtube_video_ids
    )

    new_count = 0
    remaining = max(0, config.YOUTUBE_COMMENT_LIMIT)
    daily_draft_limit_reached = False

    with db.connect() as conn:
        video_title = _video_title_cache(youtube, conn, page_key)
        drafted_today = db.count_drafted_today(conn)
        # A video that keeps getting comments long after newer videos have
        # been uploaded falls out of latest_video_ids with no way back
        # short of adding it to YOUTUBE_VIDEO_IDS by hand -- this finds it
        # automatically from our own comment history instead.
        recently_active_ids = db.recently_active_video_ids(
            conn, platform="youtube", page_key=page_key,
            horizon=timedelta(days=config.YOUTUBE_ACTIVE_VIDEO_DAYS),
        )
        video_ids = list(dict.fromkeys(
            [*configured_video_ids, *recently_active_ids, *latest_video_ids]
        ))
        for index, video_id in enumerate(video_ids):
            if remaining <= 0:
                break
            if daily_draft_limit_reached:
                break
            videos_left = len(video_ids) - index
            video_limit = max(1, remaining // videos_left)
            try:
                threads = _iter_top_level_threads(
                    youtube, video_id=video_id, limit=video_limit, page_key=page_key
                )
                for thread in threads:
                    remaining -= 1
                    top = thread["snippet"]["topLevelComment"]
                    comment_id = top["id"]
                    snippet = top["snippet"]

                    if not is_within_comment_age_limit(snippet.get("publishedAt", "")):
                        print(
                            f"Skipped YouTube comment {comment_id}: older than "
                            f"{config.COMMENT_MAX_AGE_DAYS} days."
                        )
                        continue

                    if db.comment_exists(conn, comment_id):
                        continue
                    if snippet.get("authorChannelId", {}).get("value") == channel_id:
                        continue  # don't reply to ourselves

                    if (
                        config.GEMINI_DAILY_DRAFT_LIMIT > 0
                        and drafted_today >= config.GEMINI_DAILY_DRAFT_LIMIT
                    ):
                        if not daily_draft_limit_reached:
                            print(
                                f"Gemini daily draft limit of {config.GEMINI_DAILY_DRAFT_LIMIT} "
                                "reached; leaving remaining new comments for a later cycle."
                            )
                            daily_draft_limit_reached = True
                        break

                    actual_video_id = snippet["videoId"]
                    text = snippet.get("textDisplay", "")
                    author = snippet.get("authorDisplayName", "someone")
                    title = video_title(actual_video_id)

                    try:
                        own_channel_ids = {
                            channel_id,
                            thread.get("snippet", {}).get("channelId", ""),
                        }
                        existing_reply = find_own_reply(
                            youtube, comment_id, own_channel_ids, page_key=page_key
                        )
                    except Exception:
                        print(f"Could not verify existing replies for YouTube comment {comment_id}; skipping this run.")
                        continue

                    if existing_reply:
                        print(
                            f"Skipped YouTube comment {comment_id} before database insert: "
                            "this channel already replied."
                        )
                        # Without this, the comment never becomes known to
                        # comment_exists() and find_own_reply() -- an API
                        # call -- gets re-run for it on every future cycle
                        # forever, burning real YouTube quota for an answer
                        # that will never change.
                        db.remember_seen_comment(
                            conn, comment_id, platform="youtube",
                            status="already_replied", page_key=page_key,
                        )
                        conn.commit()  # see the insert_comment commit below
                        continue

                    try:
                        reply = draft_reply(
                            platform="youtube",
                            page_key=page_key,
                            context_title=title,
                            author=author,
                            comment_text=text,
                        )
                    except Exception as e:
                        print(f"Failed to draft reply for comment {comment_id}: {e}")
                        continue

                    db.insert_comment(
                        conn,
                        comment_id=comment_id,
                        platform="youtube",
                        page_key=page_key,
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
                    drafted_today += 1
            except HttpError as e:
                if is_quota_exceeded(e):
                    raise
                print(f"YouTube API error while polling video_id={video_id!r}: {e}")

    return new_count
