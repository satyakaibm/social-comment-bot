from googleapiclient.errors import HttpError

from app import db
from app.youtube_client import get_client


def post_approved() -> int:
    """Post all approved draft replies to YouTube. Returns the number posted."""
    db.init_db()
    youtube = get_client()
    posted = 0

    with db.connect() as conn:
        rows = db.list_by_status(conn, "approved")
        for row in rows:
            try:
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
                db.update_status(
                    conn, row["comment_id"], "posted", reply_comment_id=resp["id"]
                )
                posted += 1
                print(f"Posted reply to comment {row['comment_id']}.")
            except HttpError as e:
                print(f"Failed to post reply to comment {row['comment_id']}: {e}")

    return posted


if __name__ == "__main__":
    post_approved()
