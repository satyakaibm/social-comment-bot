from googleapiclient.errors import HttpError
from requests import RequestException

from app import db, meta_client
from app.youtube_client import find_own_reply, get_client, get_my_channel_id


def post_approved(
    *,
    platform: str | None = None,
    video_id: str | None = None,
    limit: int | None = None,
    include_pending: bool = False,
    like_comments: bool = False,
    comment_id: str | None = None,
) -> int:
    """Post draft replies to YouTube, Facebook, and Instagram.

    By default only `approved` rows are posted. Pass include_pending=True to
    also post `pending_review` drafts (useful for a live trial from poll records).

    Returns the number posted.
    """
    if like_comments and platform not in ("facebook", "instagram"):
        raise ValueError("Comment likes require --platform facebook or instagram.")
    db.init_db()
    posted = 0
    youtube = None  # lazily created only if a YouTube reply needs posting
    channel_id = None
    statuses = ["approved", "pending_review"] if include_pending else ["approved"]

    with db.connect() as conn:
        rows = db.list_for_post(
            conn,
            statuses=statuses,
            platform=platform,
            video_id=video_id,
            comment_id=comment_id,
            limit=limit,
        )
        for row in rows:
            platform = row["platform"]
            try:
                if platform == "youtube":
                    if youtube is None:
                        youtube = get_client()
                    if channel_id is None:
                        channel_id = get_my_channel_id(youtube)
                    existing_reply = find_own_reply(youtube, row["comment_id"], channel_id)
                elif platform in ("facebook", "instagram"):
                    existing_reply = meta_client.find_own_reply(row["comment_id"], platform=platform)
                else:
                    print(f"Unknown platform {platform!r}, skipping.")
                    continue
            except Exception:
                print(f"Could not verify existing replies for {platform} comment {row['comment_id']}; skipping this run.")
                continue
            if existing_reply:
                db.update_status(conn, row["comment_id"], "already_replied", reply_comment_id=existing_reply)
                conn.commit()
                print(f"Skipped {platform} comment {row['comment_id']}: your account already replied.")
                continue
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
                                    "textOriginal": row["draft_reply"],
                                }
                            },
                        )
                        .execute()
                    )
                    reply_id = resp["id"]
                elif platform in ("facebook", "instagram"):
                    reply_id = meta_client.reply_to_comment(
                        row["comment_id"], row["draft_reply"], platform=platform
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
                print(f"Posted reply to {platform} comment {row['comment_id']}.")
                if like_comments:
                    try:
                        meta_client.like_comment(row["comment_id"])
                        print(f"Liked {platform} comment {row['comment_id']}.")
                    except (meta_client.GraphAPIError, RequestException, ValueError):
                        print(
                            f"Reply was posted, but liking {platform} comment "
                            f"{row['comment_id']} failed. Check Page permissions or like it manually."
                        )
            except HttpError as e:
                db.update_status(conn, row["comment_id"], "failed", error=str(e)[:1000])
                conn.commit()
                print(f"Failed to post reply to YouTube comment {row['comment_id']}: {e}")
            except meta_client.GraphAPIError as e:
                db.update_status(conn, row["comment_id"], "failed", error=str(e)[:1000])
                conn.commit()
                print(f"Failed to post reply to {platform} comment {row['comment_id']}: {e}")

    return posted


if __name__ == "__main__":
    post_approved()
