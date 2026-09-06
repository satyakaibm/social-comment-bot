from googleapiclient.errors import HttpError

from app import db, meta_client
from app.youtube_client import get_client


def post_approved(
    *,
    platform: str | None = None,
    video_id: str | None = None,
    limit: int | None = None,
    include_pending: bool = False,
) -> int:
    """Post draft replies to YouTube, Facebook, and Instagram.

    By default only `approved` rows are posted. Pass include_pending=True to
    also post `pending_review` drafts (useful for a live trial from poll records).

    Returns the number posted.
    """
    db.init_db()
    posted = 0
    youtube = None  # lazily created only if a YouTube reply needs posting
    statuses = ["approved", "pending_review"] if include_pending else ["approved"]

    with db.connect() as conn:
        rows = db.list_for_post(
            conn,
            statuses=statuses,
            platform=platform,
            video_id=video_id,
            limit=limit,
        )
        for row in rows:
            platform = row["platform"]
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
                        row["comment_id"], row["draft_reply"]
                    )
                else:
                    print(f"Unknown platform {platform!r} for comment {row['comment_id']}, skipping.")
                    continue

                db.update_status(conn, row["comment_id"], "posted", reply_comment_id=reply_id)
                conn.commit()
                posted += 1
                print(f"Posted reply to {platform} comment {row['comment_id']}.")
            except HttpError as e:
                print(f"Failed to post reply to YouTube comment {row['comment_id']}: {e}")
            except meta_client.GraphAPIError as e:
                print(f"Failed to post reply to {platform} comment {row['comment_id']}: {e}")

    return posted


if __name__ == "__main__":
    post_approved()
