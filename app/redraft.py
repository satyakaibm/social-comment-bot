from app import db
from app.generate import draft_reply


def redraft_pending() -> int:
    """Regenerate draft_reply for every pending_review comment using the
    current REPLY_PERSONA. Useful after changing the persona without wanting
    to re-fetch from YouTube. Returns the number of comments redrafted.
    """
    db.init_db()
    redrafted = 0

    with db.connect() as conn:
        rows = db.list_by_status(conn, "pending_review")
        for row in rows:
            try:
                new_reply = draft_reply(
                    video_title=row["video_title"],
                    author=row["author"],
                    comment_text=row["text"],
                )
            except Exception as e:
                print(f"Failed to redraft comment {row['comment_id']}: {e}")
                continue

            db.update_status(conn, row["comment_id"], "pending_review", draft_reply=new_reply)
            conn.commit()
            redrafted += 1

    return redrafted


if __name__ == "__main__":
    n = redraft_pending()
    print(f"Redrafted {n} comment(s).")
