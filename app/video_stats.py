import signal
import threading
import time

from app import config, db, meta_client, youtube_client


def _refresh_youtube(conn) -> int:
    containers = db.distinct_containers(
        conn, platform="youtube", limit=config.VIDEO_STATS_CONTAINER_LIMIT
    )
    if not containers:
        return 0
    # One YouTube client per channel -- videos().list() can only be called
    # with the credentials of the channel that owns the videos in the batch.
    by_page: dict[str, list[dict]] = {}
    for row in containers:
        by_page.setdefault(row["page_key"], []).append(row)
    updated = 0
    for page_key, rows in by_page.items():
        try:
            youtube = youtube_client.get_client(page_key)
            titles = {row["video_id"]: row["video_title"] for row in rows}
            stats = youtube_client.get_video_stats(
                youtube, titles.keys(), quota_conn=conn, page_key=page_key
            )
        except Exception as exc:
            print(
                f"video_stats: YouTube stats fetch failed for page '{page_key}': {exc}",
                flush=True,
            )
            continue
        for video_id, video_title in titles.items():
            counts = stats.get(video_id, {})
            db.upsert_video_stats(
                conn,
                platform="youtube",
                video_id=video_id,
                page_key=page_key,
                video_title=video_title or "",
                view_count=counts.get("view_count"),
                like_count=counts.get("like_count"),
                share_count=None,
                comment_count=counts.get("comment_count"),
            )
            conn.commit()
            updated += 1
    return updated


def _refresh_facebook(conn) -> int:
    containers = db.distinct_containers(
        conn, platform="facebook", limit=config.VIDEO_STATS_CONTAINER_LIMIT
    )
    updated = 0
    for row in containers:
        try:
            stats = meta_client.get_facebook_post_stats(
                row["video_id"], page_key=row["page_key"], quota_conn=conn
            )
        except Exception as exc:
            print(
                f"video_stats: Facebook stats fetch failed for post "
                f"{row['video_id']}: {exc}",
                flush=True,
            )
            continue
        db.upsert_video_stats(
            conn,
            platform="facebook",
            video_id=row["video_id"],
            page_key=row["page_key"],
            video_title=row.get("video_title") or "",
            view_count=stats.get("view_count"),
            like_count=stats.get("like_count"),
            share_count=stats.get("share_count"),
            comment_count=stats.get("comment_count"),
        )
        conn.commit()
        updated += 1
    return updated


def _refresh_instagram(conn) -> int:
    containers = db.distinct_containers(
        conn, platform="instagram", limit=config.VIDEO_STATS_CONTAINER_LIMIT
    )
    updated = 0
    for row in containers:
        try:
            stats = meta_client.get_instagram_media_stats(
                row["video_id"], page_key=row["page_key"], quota_conn=conn
            )
        except Exception as exc:
            print(
                f"video_stats: Instagram stats fetch failed for media "
                f"{row['video_id']}: {exc}",
                flush=True,
            )
            continue
        db.upsert_video_stats(
            conn,
            platform="instagram",
            video_id=row["video_id"],
            page_key=row["page_key"],
            video_title=row.get("video_title") or "",
            view_count=stats.get("view_count"),
            like_count=stats.get("like_count"),
            share_count=None,
            comment_count=stats.get("comment_count"),
        )
        conn.commit()
        updated += 1
    return updated


def refresh_all() -> int:
    """Refresh cached like/share/comment counts for the most recently active
    videos and posts on every configured platform.

    Each platform is capped at VIDEO_STATS_CONTAINER_LIMIT containers per
    call, so a channel with a long history can't spend a whole refresh cycle
    -- or a day's Meta rate limit -- re-fetching stats for posts nobody is
    currently viewing on the dashboard.
    """
    # Opening an encrypted SQLCipher database performs expensive key
    # derivation. Keep one connection for the cycle and commit every short
    # quota/stat update so request-serving processes are never locked across
    # a remote API call.
    with db.connect() as conn:
        return (
            _refresh_youtube(conn)
            + _refresh_facebook(conn)
            + _refresh_instagram(conn)
        )


def refresh_selected(*, youtube: bool, meta: bool) -> int:
    """Refresh only the platform groups that are due in this worker tick."""
    with db.connect() as conn:
        updated = _refresh_youtube(conn) if youtube else 0
        if meta:
            updated += _refresh_facebook(conn) + _refresh_instagram(conn)
        db.prune_video_stats_history(
            conn, retention_days=config.VIDEO_STATS_HISTORY_RETENTION_DAYS
        )
        return updated


def worker_loop(stop: threading.Event) -> None:
    db.init_db()
    youtube_interval = config.YOUTUBE_VIDEO_STATS_REFRESH_MINUTES * 60
    meta_interval = config.META_VIDEO_STATS_REFRESH_MINUTES * 60
    next_youtube = next_meta = 0.0
    while not stop.is_set():
        now = time.monotonic()
        youtube_due = now >= next_youtube
        meta_due = now >= next_meta
        try:
            refresh_selected(youtube=youtube_due, meta=meta_due)
        except Exception as exc:
            # Mirrors app/webhook.py's worker_loop: an uncaught exception here
            # would silently stop all future stats refreshes for the rest of
            # the process's uptime, with no supervisor to restart it.
            print(f"Video stats worker error: {exc}", flush=True)
        completed_at = time.monotonic()
        if youtube_due:
            next_youtube = completed_at + youtube_interval
        if meta_due:
            next_meta = completed_at + meta_interval
        stop.wait(max(0.0, min(next_youtube, next_meta) - time.monotonic()))


def start_worker(app) -> threading.Event:
    """Start an in-process worker for non-Compose callers."""
    stop = threading.Event()
    threading.Thread(target=worker_loop, args=(stop,), daemon=True, name="video-stats-worker").start()
    app.extensions["video_stats_worker_stop"] = stop
    return stop


def run() -> None:
    """Run the refresh loop as the dedicated Compose worker process."""
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(
        "Video stats worker started; YouTube refresh interval "
        f"{config.YOUTUBE_VIDEO_STATS_REFRESH_MINUTES} minute(s), Meta "
        f"{config.META_VIDEO_STATS_REFRESH_MINUTES} minute(s).",
        flush=True,
    )
    worker_loop(stop)
    print("Video stats worker stopped.", flush=True)


if __name__ == "__main__":
    run()
