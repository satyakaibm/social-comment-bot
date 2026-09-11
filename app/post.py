from builtins import print as console_print
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from googleapiclient.errors import HttpError
from requests import RequestException

from app import config, db, meta_client
from app.comment_age import is_within_comment_age_limit
from app.sanitize import remove_leading_mention, sanitize_draft
from app.youtube_client import (
    find_own_reply,
    get_client,
    get_my_channel_id,
    get_video_channel_ids,
    is_quota_exceeded,
    execute,
)


def _has_recent_reply_check(row) -> bool:
    """Return whether a fresh Meta check can be reused for the first post.

    Facebook fast mode bypasses the remote check, but only for a comment's
    first attempt. A row that previously errored (status 'failed', or an
    `error` already on record) has a possible prior write that must be
    reconciled with a real remote check before it is retried — skipping
    that check on a retry is what let a single Facebook comment receive
    several duplicate replies whenever "retry failed" was used.
    """
    if row["platform"] not in ("facebook", "instagram"):
        return False
    if row["status"] not in ("pending_review", "approved") or row["error"]:
        return False
    if row["platform"] == "facebook" and not config.FACEBOOK_VERIFY_EXISTING_REPLIES:
        return True
    checked_at = row["reply_checked_at"]
    if not checked_at or config.META_RECENT_REPLY_CHECK_SECONDS <= 0:
        return False
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - checked <= timedelta(
        seconds=config.META_RECENT_REPLY_CHECK_SECONDS
    )


def _daily_limit_for(platform: str, page_key: str) -> int:
    """Daily reply cap for one (platform, page_key) combination.

    An empty/unrecognized page_key (legacy rows inserted before multi-page
    support, or the configured default page itself) reads the live
    top-level FACEBOOK_DAILY_REPLY_LIMIT/INSTAGRAM_DAILY_REPLY_LIMIT/
    YOUTUBE_DAILY_REPLY_LIMIT constants -- not config.PAGES[...], which is
    frozen at import time and wouldn't see a test's
    patch.object(config, "FACEBOOK_DAILY_REPLY_LIMIT", ...). Any other real
    page reads its own config.PAGES entry.
    """
    if platform not in ("facebook", "instagram", "youtube"):
        return 0
    if not page_key or page_key == config.DEFAULT_PAGE_KEY:
        if platform == "facebook":
            return config.FACEBOOK_DAILY_REPLY_LIMIT
        if platform == "instagram":
            return config.INSTAGRAM_DAILY_REPLY_LIMIT
        return config.YOUTUBE_DAILY_REPLY_LIMIT
    page = config.PAGES.get(page_key)
    if page is None:
        return 0
    if platform == "facebook":
        return page.facebook_daily_reply_limit
    if platform == "instagram":
        return page.instagram_daily_reply_limit
    return page.youtube_daily_reply_limit


def post_approved(
    *,
    platform: str | None = None,
    page_key: str | None = None,
    video_id: str | None = None,
    limit: int | None = None,
    include_pending: bool = False,
    include_failed: bool = False,
    only_failed: bool = False,
    like_comments: bool = False,
    comment_id: str | None = None,
    activity_log_path: str | Path | None = None,
) -> int:
    """Post draft replies to YouTube, Facebook, and Instagram.

    By default only `approved` rows are posted. Pending and failed rows can be
    included explicitly. Every row is checked remotely before posting, so a
    retry cannot duplicate a reply that succeeded before an interruption.

    Returns the number posted.
    """
    def emit(message: str) -> None:
        console_print(message, flush=True)
        if activity_log_path is not None:
            path = Path(activity_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as activity_log:
                activity_log.write(f"{message}\n")

    db.init_db()
    posted = 0
    consecutive_platform_errors = 0
    youtube_like_notice_printed = False
    likes_to_send: list[tuple[str, str, str]] = []
    # Keyed by page_key -- each configured YouTube channel authenticates
    # with its own OAuth client and has its own "my channel" identity, so a
    # client/identity built for one channel must never be reused for
    # another's comments.
    youtube_clients: dict[str, object] = {}
    youtube_channel_ids: dict[str, str] = {}
    youtube_page_ready: dict[str, bool] = {}
    # Keyed by (platform, page_key) rather than bare platform -- two pages
    # sharing the same "facebook"/"instagram" platform string must not
    # share one daily counter (see _daily_limit_for).
    daily_posted: dict[tuple[str, str], int] = {}
    daily_limit_notice_printed: set[tuple[str, str]] = set()
    statuses = ["failed"] if only_failed else ["approved"]
    if not only_failed and include_pending:
        statuses.append("pending_review")
    if not only_failed and include_failed:
        statuses.append("failed")

    with db.connect() as conn:
        recovered = db.reset_stale_posting(conn)
        if recovered:
            emit(f"Recovered {recovered} interrupted publishing claim(s).")
        rows = db.list_for_post(
            conn,
            statuses=statuses,
            platform=platform,
            page_key=page_key,
            video_id=video_id,
            comment_id=comment_id,
            # Load all candidates so stale rows can be rejected without consuming
            # this cycle's publish allowance.
            limit=None,
        )
        current_rows = []
        for row in rows:
            if is_within_comment_age_limit(row["published_at"]):
                current_rows.append(row)
                continue
            db.update_status(
                conn,
                row["comment_id"],
                "rejected",
                error=f"Comment is older than {config.COMMENT_MAX_AGE_DAYS} days.",
            )
            emit(
                f"Skipped {row['platform']} comment {row['comment_id']}: older than "
                f"{config.COMMENT_MAX_AGE_DAYS} days."
            )
        if len(current_rows) != len(rows):
            conn.commit()
        rows = current_rows[:limit] if limit is not None else current_rows
        youtube_rows = [row for row in rows if row["platform"] == "youtube"]
        video_channel_ids: dict[str, str] = {}
        if youtube_rows:
            # reset_stale_posting() can open a SQLite write transaction even
            # when it does not recover any rows. Release it before the
            # YouTube helpers record API quota usage through another SQLite
            # connection, otherwise the identity check can lock itself.
            conn.commit()
            rows_by_yt_page: dict[str, list] = {}
            for row in youtube_rows:
                rows_by_yt_page.setdefault(row["page_key"] or config.DEFAULT_PAGE_KEY, []).append(row)
            for yt_page_key, page_rows in rows_by_yt_page.items():
                try:
                    client = get_client(page_key=yt_page_key)
                    youtube_clients[yt_page_key] = client
                    youtube_channel_ids[yt_page_key] = get_my_channel_id(client)
                    video_channel_ids.update(
                        get_video_channel_ids(client, (row["video_id"] for row in page_rows))
                    )
                    youtube_page_ready[yt_page_key] = True
                except Exception as exc:
                    label = (
                        config.PAGES[yt_page_key].label
                        if yt_page_key != config.DEFAULT_PAGE_KEY
                        else "Hindolroad"
                    )
                    emit(
                        f"Could not verify the {label} YouTube channel identity; "
                        f"skipping YouTube publishing: {exc}"
                    )
                    youtube_page_ready[yt_page_key] = False

        for row in rows:
            platform = row["platform"]
            row_page_key = row["page_key"]
            # Legacy rows (any platform, written before this column existed)
            # store page_key='' -- meta_client/youtube_clients both index
            # config.PAGES directly for a non-default key, so this must
            # never be passed through empty (KeyError). count_posted_today
            # above intentionally still receives the raw row_page_key, since
            # its own falsy-check is what makes an empty value mean
            # "unfiltered", matching legacy rows that predate this column.
            meta_page_key = row_page_key or config.DEFAULT_PAGE_KEY
            daily_key = (platform, row_page_key)
            daily_limit = _daily_limit_for(platform, row_page_key)
            if daily_limit > 0:
                if daily_key not in daily_posted:
                    daily_posted[daily_key] = db.count_posted_today(
                        conn, platform, page_key=row_page_key
                    )
                if daily_posted[daily_key] >= daily_limit:
                    if daily_key not in daily_limit_notice_printed:
                        unit = "reply" if daily_limit == 1 else "replies"
                        page_label = (
                            f" ({config.PAGES[row_page_key].label})"
                            if row_page_key and row_page_key in config.PAGES
                            and row_page_key != config.DEFAULT_PAGE_KEY
                            else ""
                        )
                        emit(
                            f"{platform.title()}{page_label} daily limit of "
                            f"{daily_limit} {unit} reached; remaining comments "
                            "are left for the next day."
                        )
                        daily_limit_notice_printed.add(daily_key)
                    continue
            original_status = row["status"]
            if not db.claim_comment_for_post(
                conn, row["comment_id"], original_status
            ):
                emit(
                    f"Skipped {platform} comment {row['comment_id']}: "
                    "another process is already handling it."
                )
                continue
            conn.commit()
            try:
                if _has_recent_reply_check(row):
                    existing_reply = None
                elif platform == "youtube":
                    video_owner_id = video_channel_ids.get(row["video_id"], "")
                    if not youtube_page_ready.get(meta_page_key) or not video_owner_id:
                        emit(
                            "Could not confirm the video owner for YouTube comment "
                            f"{row['comment_id']}; skipping to prevent a duplicate reply."
                        )
                        db.update_status(conn, row["comment_id"], original_status)
                        conn.commit()
                        continue
                    youtube = youtube_clients[meta_page_key]
                    own_channel_ids = {
                        youtube_channel_ids[meta_page_key],
                        video_owner_id,
                    }
                    existing_reply = find_own_reply(
                        youtube, row["comment_id"], own_channel_ids
                    )
                elif platform in ("facebook", "instagram"):
                    check_started = monotonic()
                    existing_reply = meta_client.find_own_reply(
                        row["comment_id"], platform=platform, page_key=meta_page_key
                    )
                    db.record_reply_check(conn, row["comment_id"])
                    conn.commit()
                    check_seconds = monotonic() - check_started
                    if check_seconds >= 5:
                        emit(
                            f"{platform.title()} duplicate check for comment "
                            f"{row['comment_id']} took {check_seconds:.1f}s."
                        )
                else:
                    emit(f"Unknown platform {platform!r}, skipping.")
                    db.update_status(conn, row["comment_id"], original_status)
                    conn.commit()
                    continue
            except Exception:
                db.update_status(conn, row["comment_id"], original_status)
                conn.commit()
                emit(f"Could not verify existing replies for {platform} comment {row['comment_id']}; skipping this run.")
                continue
            if existing_reply:
                db.update_status(
                    conn,
                    row["comment_id"],
                    "already_replied",
                    reply_comment_id=existing_reply,
                    error="",
                )
                conn.commit()
                emit(f"Skipped {platform} comment {row['comment_id']}: your account already replied.")
                continue
            reply_text = sanitize_draft(row["draft_reply"] or "")
            if platform == "youtube":
                # Existing queued YouTube drafts may still contain the old
                # automated @username prefix. Replies are already nested below
                # the comment, so post them as plain text.
                reply_text = remove_leading_mention(reply_text)
            if reply_text != (row["draft_reply"] or ""):
                db.update_status(
                    conn,
                    row["comment_id"],
                    "posting",
                    draft_reply=reply_text,
                )
                conn.commit()
            try:
                if platform == "youtube":
                    youtube = youtube_clients[meta_page_key]
                    resp = execute(
                        youtube.comments().insert(
                            part="snippet",
                            body={
                                "snippet": {
                                    "parentId": row["comment_id"],
                                    "textOriginal": reply_text,
                                }
                            },
                        ),
                        units=50,
                    )
                    reply_id = resp["id"]
                elif platform in ("facebook", "instagram"):
                    reply_id = meta_client.reply_to_comment(
                        row["comment_id"], reply_text, platform=platform,
                        page_key=meta_page_key,
                    )
                else:
                    emit(f"Unknown platform {platform!r} for comment {row['comment_id']}, skipping.")
                    continue

                db.update_status(
                    conn,
                    row["comment_id"],
                    "posted",
                    reply_comment_id=reply_id,
                    error="",
                )
                conn.commit()
                posted += 1
                daily_posted[daily_key] = daily_posted.get(daily_key, 0) + 1
                consecutive_platform_errors = 0
                emit(f"Posted reply to {platform} comment {row['comment_id']}.")
                if like_comments and platform in ("facebook", "instagram"):
                    # Replies are time-sensitive. Queue likes until every
                    # reply in this batch has been sent so a slow like cannot
                    # delay later commenters receiving their reply.
                    likes_to_send.append((platform, row["comment_id"], meta_page_key))
                elif like_comments and platform == "youtube":
                    if not youtube_like_notice_printed:
                        emit(
                            "YouTube comments cannot be liked by this automation; "
                            "the YouTube Data API has no comment-like endpoint."
                        )
                        youtube_like_notice_printed = True
            except HttpError as e:
                if is_quota_exceeded(e):
                    emit(
                        "YouTube quota is exhausted; leaving remaining comments "
                        "pending for the next cycle."
                    )
                    break
                db.update_status(conn, row["comment_id"], "failed", error=str(e)[:1000])
                conn.commit()
                emit(f"Failed to post reply to YouTube comment {row['comment_id']}: {e}")
            except meta_client.GraphAPIError as e:
                result_status = db.record_publish_failure(
                    conn,
                    row["comment_id"],
                    str(e),
                    max_attempts=config.META_MAX_POST_ATTEMPTS,
                )
                conn.commit()
                if result_status == "rejected":
                    emit(
                        f"Gave up on {platform} comment {row['comment_id']} after "
                        f"{config.META_MAX_POST_ATTEMPTS} failed attempts; it will "
                        f"no longer be retried automatically: {e}"
                    )
                else:
                    emit(f"Failed to post reply to {platform} comment {row['comment_id']}: {e}")
                consecutive_platform_errors += 1
                if consecutive_platform_errors >= config.PUBLISH_ERROR_LIMIT:
                    emit(
                        f"Stopped {platform} publishing after "
                        f"{consecutive_platform_errors} consecutive API errors; "
                        "remaining comments were left unchanged."
                    )
                    break
            except RequestException as e:
                # A timed-out write may still have reached Meta. Keep the row
                # pending so the next run checks for that reply before retrying.
                emit(
                    f"Meta request timed out for {platform} comment "
                    f"{row['comment_id']}; left retryable for verification: {e}"
                )
                db.update_status(
                    conn, row["comment_id"], original_status, error=str(e)[:1000]
                )
                conn.commit()

        if likes_to_send:
            emit(
                f"Reply posting finished; liking {len(likes_to_send)} "
                "Facebook/Instagram comment(s)."
            )
        for like_platform, like_comment_id, like_page_key in likes_to_send:
            try:
                meta_client.like_comment(
                    like_comment_id, platform=like_platform, page_key=like_page_key
                )
                emit(f"Liked {like_platform} comment {like_comment_id}.")
            except (meta_client.GraphAPIError, RequestException, ValueError) as exc:
                emit(
                    f"Reply was posted, but liking {like_platform} comment "
                    f"{like_comment_id} failed: {exc}. "
                    "Check Page permissions "
                    "(instagram_manage_engagement for Instagram) "
                    "or like it manually."
                )

    return posted


if __name__ == "__main__":
    post_approved()
