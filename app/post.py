from googleapiclient.errors import HttpError
from requests import RequestException

from app import config, db, meta_client
from app.comment_age import is_within_comment_age_limit
from app.sanitize import sanitize_draft
from app.youtube_client import (
    find_own_reply,
    get_client,
    get_my_channel_id,
    get_video_channel_ids,
    is_quota_exceeded,
)


def post_approved(
    *,
    platform: str | None = None,
    video_id: str | None = None,
    limit: int | None = None,
    include_pending: bool = False,
    include_failed: bool = False,
    like_comments: bool = False,
    comment_id: str | None = None,
) -> int:
    """Post draft replies to YouTube, Facebook, and Instagram.

    By default only `approved` rows are posted. Pending and failed rows can be
    included explicitly. Every row is checked remotely before posting, so a
    retry cannot duplicate a reply that succeeded before an interruption.

    Returns the number posted.
    """
    db.init_db()
    posted = 0
    consecutive_platform_errors = 0
    youtube_like_notice_printed = False
    youtube = None  # lazily created only if a YouTube reply needs posting
    channel_id = None
    statuses = ["approved"]
    if include_pending:
        statuses.append("pending_review")
    if include_failed:
        statuses.append("failed")

    with db.connect() as conn:
        recovered = db.reset_stale_posting(conn)
        if recovered:
            print(f"Recovered {recovered} interrupted publishing claim(s).")
        rows = db.list_for_post(
            conn,
            statuses=statuses,
            platform=platform,
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
            print(
                f"Skipped {row['platform']} comment {row['comment_id']}: older than "
                f"{config.COMMENT_MAX_AGE_DAYS} days."
            )
        if len(current_rows) != len(rows):
            conn.commit()
        rows = current_rows[:limit] if limit is not None else current_rows
        youtube_rows = [row for row in rows if row["platform"] == "youtube"]
        video_channel_ids = {}
        youtube_identity_ready = not youtube_rows
        if youtube_rows:
            try:
                youtube = get_client()
                channel_id = get_my_channel_id(youtube)
                video_channel_ids = get_video_channel_ids(
                    youtube, (row["video_id"] for row in youtube_rows)
                )
                youtube_identity_ready = True
            except Exception as exc:
                print(
                    "Could not verify the Hindolroad YouTube channel identity; "
                    f"skipping YouTube publishing: {exc}"
                )

        for row in rows:
            platform = row["platform"]
            original_status = row["status"]
            if not db.claim_comment_for_post(
                conn, row["comment_id"], original_status
            ):
                print(
                    f"Skipped {platform} comment {row['comment_id']}: "
                    "another process is already handling it."
                )
                continue
            conn.commit()
            try:
                if platform == "youtube":
                    video_owner_id = video_channel_ids.get(row["video_id"], "")
                    if not youtube_identity_ready or not video_owner_id:
                        print(
                            "Could not confirm the video owner for YouTube comment "
                            f"{row['comment_id']}; skipping to prevent a duplicate reply."
                        )
                        db.update_status(conn, row["comment_id"], original_status)
                        conn.commit()
                        continue
                    if youtube is None:
                        youtube = get_client()
                    if channel_id is None:
                        channel_id = get_my_channel_id(youtube)
                    own_channel_ids = {
                        channel_id,
                        video_owner_id,
                    }
                    existing_reply = find_own_reply(
                        youtube, row["comment_id"], own_channel_ids
                    )
                elif platform in ("facebook", "instagram"):
                    existing_reply = meta_client.find_own_reply(row["comment_id"], platform=platform)
                else:
                    print(f"Unknown platform {platform!r}, skipping.")
                    db.update_status(conn, row["comment_id"], original_status)
                    conn.commit()
                    continue
            except Exception:
                db.update_status(conn, row["comment_id"], original_status)
                conn.commit()
                print(f"Could not verify existing replies for {platform} comment {row['comment_id']}; skipping this run.")
                continue
            if existing_reply:
                db.update_status(conn, row["comment_id"], "already_replied", reply_comment_id=existing_reply)
                conn.commit()
                print(f"Skipped {platform} comment {row['comment_id']}: your account already replied.")
                continue
            reply_text = sanitize_draft(row["draft_reply"] or "")
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
                    if youtube is None:
                        youtube = get_client()
                    resp = (
                        youtube.comments()
                        .insert(
                            part="snippet",
                            body={
                                "snippet": {
                                    "parentId": row["comment_id"],
                                    "textOriginal": reply_text,
                                }
                            },
                        )
                        .execute()
                    )
                    reply_id = resp["id"]
                elif platform in ("facebook", "instagram"):
                    reply_id = meta_client.reply_to_comment(
                        row["comment_id"], reply_text, platform=platform
                    )
                else:
                    print(f"Unknown platform {platform!r} for comment {row['comment_id']}, skipping.")
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
                consecutive_platform_errors = 0
                print(f"Posted reply to {platform} comment {row['comment_id']}.")
                if like_comments and platform in ("facebook", "instagram"):
                    try:
                        meta_client.like_comment(
                            row["comment_id"], platform=platform
                        )
                        print(f"Liked {platform} comment {row['comment_id']}.")
                    except (meta_client.GraphAPIError, RequestException, ValueError) as exc:
                        print(
                            f"Reply was posted, but liking {platform} comment "
                            f"{row['comment_id']} failed: {exc}. "
                            "Check Page permissions "
                            "(instagram_manage_engagement for Instagram) "
                            "or like it manually."
                        )
                elif like_comments and platform == "youtube":
                    if not youtube_like_notice_printed:
                        print(
                            "YouTube comments cannot be liked by this automation; "
                            "the YouTube Data API has no comment-like endpoint."
                        )
                        youtube_like_notice_printed = True
            except HttpError as e:
                if is_quota_exceeded(e):
                    print(
                        "YouTube quota is exhausted; leaving remaining comments "
                        "pending for the next cycle."
                    )
                    break
                db.update_status(conn, row["comment_id"], "failed", error=str(e)[:1000])
                conn.commit()
                print(f"Failed to post reply to YouTube comment {row['comment_id']}: {e}")
            except meta_client.GraphAPIError as e:
                db.update_status(conn, row["comment_id"], "failed", error=str(e)[:1000])
                conn.commit()
                print(f"Failed to post reply to {platform} comment {row['comment_id']}: {e}")
                consecutive_platform_errors += 1
                if consecutive_platform_errors >= config.PUBLISH_ERROR_LIMIT:
                    print(
                        f"Stopped {platform} publishing after "
                        f"{consecutive_platform_errors} consecutive API errors; "
                        "remaining comments were left unchanged."
                    )
                    break
            except RequestException as e:
                # A timed-out write may still have reached Meta. Keep the row
                # pending so the next run checks for that reply before retrying.
                print(
                    f"Meta request timed out for {platform} comment "
                    f"{row['comment_id']}; left retryable for verification: {e}"
                )
                db.update_status(
                    conn, row["comment_id"], original_status, error=str(e)[:1000]
                )
                conn.commit()

    return posted


if __name__ == "__main__":
    post_approved()
