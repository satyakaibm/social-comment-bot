"""Long-running YouTube comment polling and publishing worker.

YouTube does not provide comment webhooks, so each configured channel is
polled on an interval. Channel failures are deliberately isolated: an expired
token or API error for one channel must not prevent later channels from being
processed.
"""

import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from app import config, db
from app.fetch import poll_and_draft
from app.post import post_approved


class _Tee:
    """Mirror worker output to Docker logs and the persistent activity log."""

    def __init__(self, stream, log_path: Path):
        self.stream = stream
        self.log_path = log_path

    def write(self, value: str) -> int:
        self.stream.write(value)
        self.stream.flush()
        if value:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as log:
                log.write(value)
        return len(value)

    def flush(self) -> None:
        self.stream.flush()


def run_cycle() -> int:
    """Poll and publish every configured channel, returning failed step count."""
    page_keys = config.youtube_page_keys()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n==== YouTube worker cycle started: {started} ====")
    if not page_keys:
        print("No YouTube channels are configured; cycle skipped.")
        with db.connect() as conn:
            db.record_heartbeat(conn, "youtube_poller", detail="no channels configured")
        return 0

    failures = 0
    for page_key in page_keys:
        label = config.PAGES[page_key].label
        print(f"[{label}] Polling latest comments...")
        try:
            queued = poll_and_draft(page_key=page_key)
            print(f"[{label}] Queued {queued} new comment(s).")
        except Exception as exc:
            failures += 1
            print(f"[{label}] Polling failed; publishing skipped: {exc}")
            continue

        print(
            f"[{label}] Publishing up to {config.YOUTUBE_PUBLISH_LIMIT} "
            "pending replies..."
        )
        try:
            posted = post_approved(
                platform="youtube",
                page_key=page_key,
                include_pending=True,
                include_failed=True,
                limit=config.YOUTUBE_PUBLISH_LIMIT,
            )
            print(f"[{label}] Posted {posted} reply/replies.")
        except Exception as exc:
            failures += 1
            print(f"[{label}] Publishing failed: {exc}")

    finished = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"==== YouTube worker cycle finished: {finished}; failed steps: {failures} ====")
    with db.connect() as conn:
        db.record_heartbeat(
            conn, "youtube_poller",
            detail=f"{len(page_keys)} channel(s), {failures} failure(s)",
        )
    return failures


def main() -> None:
    stop = threading.Event()

    def request_stop(_signum, _frame) -> None:
        print("Stop requested; YouTube worker will exit after the active operation.")
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log_path = Path(config.POLLING_LOG_FILE)
    if not log_path.is_absolute():
        log_path = config.BASE_DIR / log_path
    sys.stdout = _Tee(sys.stdout, log_path)
    sys.stderr = _Tee(sys.stderr, log_path)

    print(
        "YouTube comments worker started; "
        f"poll interval {config.YOUTUBE_POLL_INTERVAL_SECONDS} second(s)."
    )
    while not stop.is_set():
        run_cycle()
        stop.wait(config.YOUTUBE_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
