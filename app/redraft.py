from app import config, db
from app.generate import draft_reply


def redraft_pending(
    *,
    platform: str | None = None,
    page_key: str | None = None,
    error_contains: str | None = None,
    missing_drafts_only: bool = False,
    limit: int | None = None,
) -> int:
    """Regenerate draft_reply for every pending_review comment using its
    page's current persona. Useful after changing a persona without wanting
    to re-fetch from YouTube/Facebook/Instagram. Optional filters allow a
    bounded retry of a specific set of comments. Returns the number of
    comments redrafted.
    """
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")

    db.init_db()
    redrafted = 0

    with db.connect() as conn:
        rows = [
            row
            for row in db.list_by_status(conn, "pending_review")
            if (platform is None or row["platform"] == platform)
            and (page_key is None or row["page_key"] == page_key)
            and (error_contains is None or error_contains in (row["error"] or ""))
            and (
                not missing_drafts_only
                or not (row["draft_reply"] or "").strip()
            )
        ]
        if limit is not None:
            rows = rows[:limit]

        for row in rows:
            try:
                new_reply = draft_reply(
                    platform=row["platform"],
                    page_key=row["page_key"] or config.DEFAULT_PAGE_KEY,
                    context_title=row["video_title"],
                    author=row["author"],
                    comment_text=row["text"],
                )
            except Exception as e:
                print(f"Failed to redraft comment {row['comment_id']}: {e}")
                continue
            if not (new_reply or "").strip():
                print(f"Failed to redraft comment {row['comment_id']}: empty draft")
                continue

            db.update_status(
                conn,
                row["comment_id"],
                "pending_review",
                draft_reply=new_reply,
                error="",
            )
            conn.commit()
            redrafted += 1

    return redrafted


if __name__ == "__main__":
    n = redraft_pending()
    print(f"Redrafted {n} comment(s).")
