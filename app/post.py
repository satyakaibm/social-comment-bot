from googleapiclient.errors import HttpError

from app import db, meta_client
from app.youtube_client import get_client


def post_approved() -> int:
    """Post all approved draft replies to YouTube, Facebook, and Instagram.

    Returns the number posted.
    """
    db.init_db()
    posted = 0
    youtube = None  # lazily created only if a YouTube reply needs posting

    with db.connect() as conn:
        rows = db.list_by_status(conn, "approved")
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
                posted += 1
                print(f"Posted reply to {platform} comment {row['comment_id']}.")
            except HttpError as e:
                print(f"Failed to post reply to YouTube comment {row['comment_id']}: {e}")
            except meta_client.GraphAPIError as e:
                print(f"Failed to post reply to {platform} comment {row['comment_id']}: {e}")

    return posted


if __name__ == "__main__":
    post_approved()
