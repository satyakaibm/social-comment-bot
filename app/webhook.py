import hashlib
import hmac
import json
import threading

from flask import Flask, Response, request

from app import config, db, meta_client
from app.comment_age import is_within_comment_age_limit
from app.generate import draft_reply
from app.post import post_approved


def _signature_is_valid(body: bytes, signature: str) -> bool:
    if not config.META_APP_SECRET or not signature.startswith("sha256="):
        return False
    expected = hmac.new(config.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def extract_comment_events(payload: dict) -> list[dict]:
    events = []
    source = payload.get("object")
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            if source == "page" and change.get("field") == "feed":
                if value.get("item") != "comment" or value.get("verb") != "add":
                    continue
                if (
                    value.get("parent_id")
                    and value.get("post_id")
                    and value["parent_id"] != value["post_id"]
                ):
                    continue
                author = value.get("from") or {}
                events.append({
                    "platform": "facebook",
                    "comment_id": value.get("comment_id"),
                    "container_id": value.get("post_id"),
                    "text": value.get("message", ""),
                    "author": author.get("name", "someone"),
                    "author_id": author.get("id", ""),
                    "published_at": str(value.get("created_time", "")),
                })
            elif source == "instagram" and change.get("field") == "comments":
                author = value.get("from") or {}
                media = value.get("media") or {}
                events.append({
                    "platform": "instagram",
                    "comment_id": value.get("id"),
                    "container_id": media.get("id") or value.get("media_id"),
                    "text": value.get("text", ""),
                    "author": author.get("username") or value.get("username") or "someone",
                    "author_id": author.get("id", ""),
                    "published_at": value.get("timestamp", ""),
                })
    return [event for event in events if event["comment_id"] and event["container_id"]]


def queue_payload(payload: dict) -> int:
    db.init_db()
    queued = 0
    with db.connect() as conn:
        for event in extract_comment_events(payload):
            if not is_within_comment_age_limit(event.get("published_at")):
                print(
                    f"Skipped {event['platform']} webhook comment "
                    f"{event['comment_id']}: older than {config.COMMENT_MAX_AGE_DAYS} days.",
                    flush=True,
                )
                continue
            if db.comment_exists(conn, event["comment_id"]):
                continue
            raw = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
            if db.enqueue_webhook_event(
                conn,
                event_key=f'{event["platform"]}:{event["comment_id"]}',
                platform=event["platform"],
                payload=raw,
            ):
                queued += 1
    return queued


def process_event(event: dict) -> None:
    platform = event["platform"]
    comment_id = event["comment_id"]
    if platform == "facebook" and event.get("author_id") == config.FACEBOOK_PAGE_ID:
        return
    if (
        platform == "instagram"
        and event.get("author", "").casefold()
        == meta_client.get_instagram_username().casefold()
    ):
        return

    with db.connect() as conn:
        if db.comment_exists(conn, comment_id):
            return

    existing = meta_client.find_own_reply(comment_id, platform=platform)
    if existing:
        with db.connect() as conn:
            db.insert_comment(
                conn,
                comment_id=comment_id,
                platform=platform,
                video_id=event["container_id"],
                video_title=event["container_id"],
                author=event["author"],
                text=event["text"],
                published_at=event.get("published_at", ""),
                draft_reply="",
            )
            db.update_status(conn, comment_id, "already_replied", reply_comment_id=existing)
        return

    get_title = (
        meta_client.get_facebook_post_message
        if platform == "facebook"
        else meta_client.get_instagram_media_caption
    )
    title = get_title(event["container_id"]) or event["container_id"]
    reply = draft_reply(
        platform=platform,
        context_title=title,
        author=event["author"],
        comment_text=event["text"],
    )
    with db.connect() as conn:
        db.insert_comment(
            conn,
            comment_id=comment_id,
            platform=platform,
            video_id=event["container_id"],
            video_title=title,
            author=event["author"],
            text=event["text"],
            published_at=event.get("published_at", ""),
            draft_reply=reply,
        )
    if config.META_WEBHOOK_AUTO_POST:
        posted = post_approved(
            platform=platform,
            comment_id=comment_id,
            include_pending=True,
            like_comments=True,
            limit=1,
        )
        with db.connect() as conn:
            result = db.get_comment(conn, comment_id)
        if posted:
            print(f"Posted webhook reply to {platform} comment {comment_id}.", flush=True)
        elif result and result["status"] == "already_replied":
            print(
                f"Skipped webhook reply to {platform} comment {comment_id}: "
                "the account already replied.",
                flush=True,
            )


def process_one_pending_event() -> bool:
    db.init_db()
    with db.connect() as conn:
        row = db.claim_webhook_event(conn)
    if row is None:
        return False
    try:
        process_event(json.loads(row["payload"]))
    except Exception as exc:
        with db.connect() as conn:
            db.finish_webhook_event(conn, row["event_key"], error=str(exc)[:1000])
        print(f'Webhook event {row["event_key"]} failed: {exc}', flush=True)
    else:
        with db.connect() as conn:
            db.finish_webhook_event(conn, row["event_key"])
    return True


def worker_loop(stop: threading.Event) -> None:
    db.init_db()
    with db.connect() as conn:
        db.reset_interrupted_webhook_events(conn)
    while not stop.is_set():
        if not process_one_pending_event():
            stop.wait(0.5)


def start_event_worker(app: Flask) -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=worker_loop, args=(stop,), daemon=True).start()
    app.extensions["webhook_worker_stop"] = stop
    return stop


def register_meta_routes(app: Flask) -> None:
    @app.get("/webhooks/meta")
    def verify_meta():
        if (
            request.args.get("hub.mode") == "subscribe"
            and config.META_WEBHOOK_VERIFY_TOKEN
            and request.args.get("hub.verify_token") == config.META_WEBHOOK_VERIFY_TOKEN
        ):
            return Response(request.args.get("hub.challenge", ""), mimetype="text/plain")
        return Response("verification failed", status=403)

    @app.post("/webhooks/meta")
    def receive_meta():
        body = request.get_data(cache=True)
        if not _signature_is_valid(
            body, request.headers.get("X-Hub-Signature-256", "")
        ):
            return Response("invalid signature", status=401)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return Response("invalid JSON", status=400)
        return {"received": True, "queued": queue_payload(payload)}


def create_app(*, start_worker: bool = True) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return {
            "service": "social-comment-bot webhook",
            "status": "ok",
            "health": "/health",
            "meta_callback": "/webhooks/meta",
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    register_meta_routes(app)

    if start_worker:
        start_event_worker(app)
    return app
