import argparse
import time

from app import config
from app.fetch import poll_and_draft
from app.post import post_approved
from app.redraft import redraft_pending
from app.review import review_loop


def cmd_poll(_args) -> None:
    n = poll_and_draft()
    print(f"Queued {n} new comment(s) for review.")


def cmd_redraft(_args) -> None:
    n = redraft_pending()
    print(f"Redrafted {n} comment(s) using the current REPLY_PERSONA.")


def cmd_review(_args) -> None:
    review_loop()


def cmd_post(_args) -> None:
    n = post_approved()
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="social-comment-bot")
    sub = parser.add_subparsers(required=True)

    sub.add_parser("poll", help="Fetch new comments and draft replies").set_defaults(
        func=cmd_poll
    )
    sub.add_parser("review", help="Interactively approve/reject/edit drafts").set_defaults(
        func=cmd_review
    )
    sub.add_parser("post", help="Post all approved replies to YouTube").set_defaults(
        func=cmd_post
    )
    sub.add_parser(
        "run", help="Continuously poll on an interval (drafting only, no posting)"
    ).set_defaults(func=cmd_run)
    sub.add_parser(
        "redraft",
        help="Regenerate drafts for all pending_review comments with the current REPLY_PERSONA",
    ).set_defaults(func=cmd_redraft)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
