import argparse
import time

from app import config
from app.fetch import poll_and_draft
from app.post import post_approved
from app.redraft import redraft_pending
from app.review import review_loop
from app.social_fetch import poll_facebook_and_draft, poll_instagram_and_draft


def cmd_poll(_args) -> None:
    n = poll_and_draft()
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


def cmd_sanitize_drafts(_args) -> None:
    from app.sanitize import sanitize_stored_drafts

    n = sanitize_stored_drafts(platform="youtube")
    print(f"Sanitized {n} YouTube draft(s).")


def cmd_review(_args) -> None:
    review_loop()


def cmd_post(args) -> None:
    n = post_approved(
        platform=args.platform,
        video_id=args.video_id,
        limit=args.limit,
        include_pending=args.pending,
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


def cmd_webhook(_args) -> None:
    from app.webhook import run as run_webhook

    run_webhook()


def cmd_dashboard(_args) -> None:
    from app.dashboard import run as run_dashboard

    run_dashboard()


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
    post_p.set_defaults(func=cmd_post)
    post_p.add_argument(
        "--like-comments",
        action="store_true",
        help="Like each original comment after replying (Facebook or Instagram)",
    )
    sub.add_parser(
        "run", help="Continuously poll on an interval (drafting only, no posting)"
    ).set_defaults(func=cmd_run)
    sub.add_parser(
        "webhook", help="Run the Facebook and Instagram webhook receiver"
    ).set_defaults(func=cmd_webhook)
    sub.add_parser(
        "dashboard", help="Open the localhost admin dashboard for comment review"
    ).set_defaults(func=cmd_dashboard)
    sub.add_parser(
        "redraft",
        help="Regenerate drafts for all pending_review comments with the current REPLY_PERSONA",
    ).set_defaults(func=cmd_redraft)
    sub.add_parser(
        "sanitize-drafts",
        help="Strip thanks-for-watching filler from stored YouTube drafts",
    ).set_defaults(func=cmd_sanitize_drafts)

    args = parser.parse_args()
    if getattr(args, "like_comments", False) and args.platform not in ("facebook", "instagram"):
        parser.error("--like-comments requires --platform facebook or instagram")
    args.func(args)


if __name__ == "__main__":
    main()
