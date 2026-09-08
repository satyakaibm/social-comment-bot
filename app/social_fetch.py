from app import config, db, meta_client
from app.comment_age import is_within_comment_age_limit
from app.generate import draft_reply


def _title_cache(get_title):
    cache: dict[str, str] = {}

    def get(item_id: str) -> str:
        if item_id not in cache:
            try:
                cache[item_id] = get_title(item_id) or item_id
            except meta_client.GraphAPIError:
                cache[item_id] = item_id
        return cache[item_id]

    return get


def poll_facebook_and_draft() -> int:
    """Fetch new top-level comments on the Page's posts and draft replies.

    Returns the number of new comments queued for review.
    """
    db.init_db()
    config.require("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")
    post_message = _title_cache(meta_client.get_facebook_post_message)
    new_count = 0
    remaining = max(0, config.FACEBOOK_COMMENT_LIMIT)
    daily_draft_limit_reached = False
    post_ids = list(meta_client.iter_facebook_post_ids())

    with db.connect() as conn:
        drafted_today = db.count_drafted_today(conn)
        for index, post_id in enumerate(post_ids):
            if remaining <= 0:
                break
            if daily_draft_limit_reached:
                break
            containers_left = len(post_ids) - index
            container_limit = max(1, remaining // containers_left)
            try:
                for comment in meta_client.iter_facebook_post_comments(
                    post_id, limit=container_limit
                ):
                    remaining -= 1
                    comment_id = comment["id"]

                    if not is_within_comment_age_limit(comment.get("created_time", "")):
                        print(
                            f"Skipped Facebook comment {comment_id}: older than "
                            f"{config.COMMENT_MAX_AGE_DAYS} days."
                        )
                        continue

                    if db.comment_exists(conn, comment_id):
                        continue
                    if comment.get("from", {}).get("id") == config.FACEBOOK_PAGE_ID:
                        continue  # don't reply to ourselves

                    text = comment.get("message", "")
                    author = comment.get("from", {}).get("name", "someone")
                    title = post_message(post_id)

                    existing_reply = None
                    if config.FACEBOOK_VERIFY_EXISTING_REPLIES:
                        try:
                            existing_reply = meta_client.find_own_reply(
                                comment_id, platform="facebook"
                            )
                        except Exception:
                            print(
                                "Could not verify existing replies for facebook "
                                f"comment {comment_id}; skipping this run."
                            )
                            continue

                    if (
                        not existing_reply
                        and config.GEMINI_DAILY_DRAFT_LIMIT > 0
                        and drafted_today >= config.GEMINI_DAILY_DRAFT_LIMIT
                    ):
                        if not daily_draft_limit_reached:
                            print(
                                f"Gemini daily draft limit of {config.GEMINI_DAILY_DRAFT_LIMIT} "
                                "reached; leaving remaining new comments for a later cycle."
                            )
                            daily_draft_limit_reached = True
                        break

                    try:
                        reply = "" if existing_reply else draft_reply(
                            platform="facebook",
                            context_title=title,
                            author=author,
                            comment_text=text,
                        )
                    except Exception as e:
                        print(f"Failed to draft reply for Facebook comment {comment_id}: {e}")
                        continue

                    db.insert_comment(
                        conn,
                        comment_id=comment_id,
                        platform="facebook",
                        video_id=post_id,
                        video_title=title,
                        author=author,
                        text=text,
                        published_at=comment.get("created_time", ""),
                        draft_reply=reply,
                        reply_checked_at=(
                            db.now()
                            if config.FACEBOOK_VERIFY_EXISTING_REPLIES
                            and not existing_reply
                            else None
                        ),
                    )
                    if existing_reply:
                        db.update_status(conn, comment_id, "already_replied", reply_comment_id=existing_reply)
                    conn.commit()
                    if not existing_reply:
                        new_count += 1
                        drafted_today += 1
            except meta_client.GraphAPIError as e:
                print(f"Facebook Graph API error while polling post_id={post_id!r}: {e}")

    return new_count


def poll_instagram_and_draft() -> int:
    """Fetch new comments on the IG account's media and draft replies.

    Returns the number of new comments queued for review.
    """
    db.init_db()
    config.require("INSTAGRAM_USER_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")
    own_username = meta_client.get_instagram_username()
    media_caption = _title_cache(meta_client.get_instagram_media_caption)
    new_count = 0
    remaining = max(0, config.INSTAGRAM_COMMENT_LIMIT)
    daily_draft_limit_reached = False
    media_ids = list(meta_client.iter_instagram_media_ids())

    with db.connect() as conn:
        drafted_today = db.count_drafted_today(conn)
        for index, media_id in enumerate(media_ids):
            if remaining <= 0:
                break
            if daily_draft_limit_reached:
                break
            containers_left = len(media_ids) - index
            container_limit = max(1, remaining // containers_left)
            try:
                for comment in meta_client.iter_instagram_media_comments(
                    media_id, limit=container_limit
                ):
                    remaining -= 1
                    comment_id = comment["id"]

                    if not is_within_comment_age_limit(comment.get("timestamp", "")):
                        print(
                            f"Skipped Instagram comment {comment_id}: older than "
                            f"{config.COMMENT_MAX_AGE_DAYS} days."
                        )
                        continue

                    if db.comment_exists(conn, comment_id):
                        continue
                    if comment.get("username") == own_username:
                        continue  # don't reply to ourselves

                    text = comment.get("text", "")
                    author = comment.get("username", "someone")
                    title = media_caption(media_id)

                    try:
                        existing_reply = meta_client.find_own_reply(comment_id, platform="instagram")
                    except Exception:
                        print(f"Could not verify existing replies for instagram comment {comment_id}; skipping this run.")
                        continue

                    if (
                        not existing_reply
                        and config.GEMINI_DAILY_DRAFT_LIMIT > 0
                        and drafted_today >= config.GEMINI_DAILY_DRAFT_LIMIT
                    ):
                        if not daily_draft_limit_reached:
                            print(
                                f"Gemini daily draft limit of {config.GEMINI_DAILY_DRAFT_LIMIT} "
                                "reached; leaving remaining new comments for a later cycle."
                            )
                            daily_draft_limit_reached = True
                        break

                    try:
                        reply = "" if existing_reply else draft_reply(
                            platform="instagram",
                            context_title=title,
                            author=author,
                            comment_text=text,
                        )
                    except Exception as e:
                        print(f"Failed to draft reply for Instagram comment {comment_id}: {e}")
                        continue

                    db.insert_comment(
                        conn,
                        comment_id=comment_id,
                        platform="instagram",
                        video_id=media_id,
                        video_title=title,
                        author=author,
                        text=text,
                        published_at=comment.get("timestamp", ""),
                        draft_reply=reply,
                        reply_checked_at=db.now() if not existing_reply else None,
                    )
                    if existing_reply:
                        db.update_status(conn, comment_id, "already_replied", reply_comment_id=existing_reply)
                    conn.commit()
                    if not existing_reply:
                        new_count += 1
                        drafted_today += 1
            except meta_client.GraphAPIError as e:
                print(f"Instagram Graph API error while polling media_id={media_id!r}: {e}")

    return new_count
