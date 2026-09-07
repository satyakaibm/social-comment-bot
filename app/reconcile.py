from googleapiclient.errors import HttpError

from app import db
from app.youtube_client import (
    find_own_reply,
    get_client,
    get_my_channel_id,
    get_video_channel_ids,
    is_quota_exceeded,
)


def reconcile_youtube_pending() -> dict[str, int | bool]:
    """Remove already-answered YouTube comments from the pending queue."""
    db.init_db()
    with db.connect() as conn:
        rows = db.list_for_post(
            conn,
            statuses=["pending_review"],
            platform="youtube",
        )

    result: dict[str, int | bool] = {
        "total": len(rows),
        "checked": 0,
        "already_replied": 0,
        "unanswered": 0,
        "errors": 0,
        "quota_exhausted": False,
    }
    if not rows:
        return result

    try:
        youtube = get_client()
        oauth_channel_id = get_my_channel_id(youtube)
        video_owners = get_video_channel_ids(
            youtube, (row["video_id"] for row in rows)
        )
    except HttpError as exc:
        if is_quota_exceeded(exc):
            result["quota_exhausted"] = True
            print("YouTube quota exhausted before reconciliation could start.")
            return result
        result["errors"] += 1
        print(f"Could not initialize YouTube reconciliation: {exc}")
        return result
    except Exception as exc:
        result["errors"] += 1
        print(f"Could not initialize YouTube reconciliation: {exc}")
        return result

    with db.connect() as conn:
        for row in rows:
            video_owner_id = video_owners.get(row["video_id"], "")
            if not video_owner_id:
                result["errors"] += 1
                print(
                    f"Could not confirm video owner for {row['comment_id']}; "
                    "left pending."
                )
                continue
            try:
                reply_id = find_own_reply(
                    youtube,
                    row["comment_id"],
                    {oauth_channel_id, video_owner_id},
                )
            except HttpError as exc:
                if is_quota_exceeded(exc):
                    result["quota_exhausted"] = True
                    print("YouTube quota exhausted; reconciliation stopped safely.")
                    break
                result["errors"] += 1
                print(f"Could not check {row['comment_id']}; left pending: {exc}")
                continue
            except Exception as exc:
                result["errors"] += 1
                print(f"Could not check {row['comment_id']}; left pending: {exc}")
                continue

            result["checked"] += 1
            if reply_id:
                db.update_status(
                    conn,
                    row["comment_id"],
                    "already_replied",
                    reply_comment_id=reply_id,
                    error="",
                )
                conn.commit()
                result["already_replied"] += 1
                print(f"Already replied: {row['comment_id']}")
            else:
                result["unanswered"] += 1

    return result
