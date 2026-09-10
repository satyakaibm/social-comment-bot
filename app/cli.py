import argparse
import sqlite3
import sys
import time

from app import config
from app.fetch import poll_and_draft
from app.post import post_approved
from app.redraft import redraft_pending
from app.review import review_loop
from app.social_fetch import poll_facebook_and_draft, poll_instagram_and_draft

# Substrings of sqlite3.OperationalError messages that are known to be
# transient (e.g. disk I/O error from concurrent host+container access to
# the same db file, see app/webhook.py) rather than real corruption.
_TRANSIENT_DB_ERRORS = ("disk i/o error", "database is locked")


def _is_transient_db_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(
        msg in str(exc).lower() for msg in _TRANSIENT_DB_ERRORS
    )


def cmd_poll(_args) -> None:
    retries = 3
    delay = 5
    for attempt in range(1, retries + 1):
        try:
            n = poll_and_draft()
            break
        except sqlite3.OperationalError as exc:
            if not _is_transient_db_error(exc) or attempt == retries:
                raise
            print(
                f"Transient DB error ({exc}); retrying in {delay}s "
                f"(attempt {attempt}/{retries})...",
                file=sys.stderr,
            )
            time.sleep(delay)
    print(f"Queued {n} new comment(s) for review.")


def cmd_poll_facebook(_args) -> None:
    n = poll_facebook_and_draft()
    print(f"Queued {n} new Facebook comment(s) for review.")


def cmd_poll_instagram(_args) -> None:
    n = poll_instagram_and_draft()
    print(f"Queued {n} new Instagram comment(s) for review.")


def cmd_poll_all(_args) -> None:
    total = 0
    for name, fn in (
        ("YouTube", poll_and_draft),
        ("Facebook", poll_facebook_and_draft),
        ("Instagram", poll_instagram_and_draft),
    ):
        try:
            n = fn()
            print(f"{name}: queued {n} new comment(s).")
            total += n
        except Exception as e:
            print(f"{name} poll failed: {e}")
    print(f"Total queued: {total}")


def cmd_redraft(_args) -> None:
    n = redraft_pending()
    print(f"Redrafted {n} comment(s) using the current REPLY_PERSONA.")


def cmd_review(_args) -> None:
    review_loop()


def cmd_post(args) -> None:
    n = post_approved(
        platform=args.platform,
        video_id=args.video_id,
        limit=args.limit,
        include_pending=args.pending,
        include_failed=args.retry_failed,
        like_comments=args.like_comments,
    )
    print(f"Posted {n} reply(ies).")


def cmd_run(_args) -> None:
    print(
        f"Polling every {config.POLL_INTERVAL_SECONDS}s. Drafts are queued for "
        "review — run `review` and `post` separately to publish them. Ctrl+C to stop."
    )
    while True:
        try:
            n = poll_and_draft()
            if n:
                print(f"Queued {n} new comment(s) for review.")
        except Exception as e:  # keep the loop alive across transient API errors
            print(f"Poll cycle failed: {e}")
        time.sleep(config.POLL_INTERVAL_SECONDS)


def cmd_dashboard(_args) -> None:
    from app.dashboard import run as run_dashboard

    run_dashboard()


def cmd_import_stats(args) -> None:
    from app import db as database

    n = database.import_seen_stats_from_backup(args.backup)
    print(
        f"Imported timestamps for {n} comment(s) from {args.backup}. "
        "Dashboard 1 Hour–365 Day counts now include pruned history."
    )


def cmd_prune(args) -> None:
    from app import db as database

    database.init_db()
    with database.connect() as conn:
        result = database.prune_storage(
            conn, older_than_days=args.older_than_days
        )
    if not args.no_vacuum:
        database.vacuum_db()
    print(
        "Pruned "
        f"{result['comments_deleted']} comment row(s) and "
        f"{result['webhooks_deleted']} webhook event(s). "
        "Comment IDs remain in seen_comments so they will not be drafted or posted again."
    )


def cmd_reconcile_youtube(_args) -> None:
    from app.reconcile import reconcile_youtube_pending

    result = reconcile_youtube_pending()
    print(
        "YouTube reconciliation: "
        f"total={result['total']}, checked={result['checked']}, "
        f"already_replied={result['already_replied']}, "
        f"unanswered={result['unanswered']}, errors={result['errors']}, "
        f"quota_exhausted={result['quota_exhausted']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="social-comment-bot")
    sub = parser.add_subparsers(required=True)

    sub.add_parser("poll", help="Fetch new YouTube comments and draft replies").set_defaults(
        func=cmd_poll
    )
    sub.add_parser(
        "poll-facebook", help="Fetch new Facebook comments and draft replies"
    ).set_defaults(func=cmd_poll_facebook)
    sub.add_parser(
        "poll-instagram", help="Fetch new Instagram comments and draft replies"
    ).set_defaults(func=cmd_poll_instagram)
    sub.add_parser(
        "poll-all", help="Fetch new comments from YouTube, Facebook, and Instagram"
    ).set_defaults(func=cmd_poll_all)
    sub.add_parser("review", help="Interactively approve/reject/edit drafts").set_defaults(
        func=cmd_review
    )
    post_p = sub.add_parser(
        "post",
        help="Post approved replies (optionally from pending poll drafts)",
    )
    post_p.add_argument(
        "--platform",
        choices=("youtube", "facebook", "instagram"),
        help="Only post replies for this platform",
    )
    post_p.add_argument(
        "--video-id",
        help="Only post replies for this YouTube video / Facebook post / Instagram media id",
    )
    post_p.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Post at most N replies (oldest first)",
    )
    post_p.add_argument(
        "--pending",
        action="store_true",
        help="Also post pending_review drafts (skip interactive review)",
    )
    post_p.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry failed replies after checking that no reply already exists",
    )
    post_p.set_defaults(func=cmd_post)
    post_p.add_argument(
        "--like-comments",
        action="store_true",
        help="Like each original Facebook or Instagram comment after posting its reply",
    )
    sub.add_parser(
        "run", help="Continuously poll on an interval (drafting only, no posting)"
    ).set_defaults(func=cmd_run)
    sub.add_parser(
        "dashboard", help="Open the dashboard with the Meta webhook receiver"
    ).set_defaults(func=cmd_dashboard)
    sub.add_parser(
        "reconcile-youtube",
        help="Remove comments already answered by Hindolroad from YouTube pending review",
    ).set_defaults(func=cmd_reconcile_youtube)
    prune_p = sub.add_parser(
        "prune",
        help=(
            "Delete handled comment text and processed webhooks to reclaim disk; "
            "keep comment IDs so they are not replied to again"
        ),
    )
    prune_p.add_argument(
        "--older-than-days",
        type=int,
        default=0,
        metavar="N",
        help="Only prune handled comments older than N days (default: all handled)",
    )
    prune_p.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip SQLite VACUUM (file size will not shrink until vacuum runs)",
    )
    prune_p.set_defaults(func=cmd_prune)
    import_p = sub.add_parser(
        "import-stats",
        help=(
            "Copy comment IDs, statuses, and timestamps from a comments.db backup "
            "into seen_comments so dashboard time-window counts stay accurate"
        ),
    )
    import_p.add_argument(
        "--backup",
        default="data/comments.db.bak",
        help="Path to the backup database (default: data/comments.db.bak)",
    )
    import_p.set_defaults(func=cmd_import_stats)
    sub.add_parser(
        "redraft",
        help="Regenerate drafts for all pending_review comments with the current REPLY_PERSONA",
    ).set_defaults(func=cmd_redraft)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("Interrupted; current operation stopped cleanly.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
