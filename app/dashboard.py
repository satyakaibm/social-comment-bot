import socket
import threading
from urllib.parse import urlencode

from flask import Flask, flash, redirect, render_template, request
from werkzeug.serving import make_server

from app import config, db
from app.post import post_approved
from app.webhook import register_meta_routes, start_event_worker

STATUSES = (
    "pending_review",
    "posted",
    "failed",
    "already_replied",
    "rejected",
)
PLATFORMS = ("youtube", "facebook", "instagram")
PAGE_SIZE = 25
CONTAINER_LABELS = {
    "youtube": "Video",
    "facebook": "Facebook post",
    "instagram": "Instagram post",
}


def _filters():
    status = request.args.get("status", "posted")
    if status not in STATUSES:
        status = "posted"
    platform = request.args.get("platform", "").strip()
    if platform not in PLATFORMS:
        platform = ""
    query = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    return status, platform, query, page


def _index_url(**overrides) -> str:
    status, platform, query, page = _filters()
    params = {
        "status": overrides.get("status", status),
        "platform": overrides.get("platform", platform),
        "q": overrides.get("q", query),
        "page": str(overrides.get("page", page)),
    }
    cleaned = {
        key: value
        for key, value in params.items()
        if value and not (key == "page" and value == "1")
    }
    return "/?" + urlencode(cleaned) if cleaned else "/"


def _row_dict(row) -> dict:
    item = dict(row)
    item["container_label"] = CONTAINER_LABELS.get(item["platform"], "Post")
    item["video_title"] = (item.get("video_title") or "")[:50]
    return item


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.DASHBOARD_SECRET
    db.init_db()

    @app.get("/")
    def index():
        status, platform, query, page = _filters()
        offset = (page - 1) * PAGE_SIZE
        with db.connect() as conn:
            counts = db.count_by_status(conn)
            total = db.count_comments(
                conn,
                status=status,
                platform=platform or None,
                query=query or None,
            )
            rows = [
                _row_dict(row)
                for row in db.list_comments(
                    conn,
                    status=status,
                    platform=platform or None,
                    query=query or None,
                    limit=PAGE_SIZE,
                    offset=offset,
                )
            ]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        return render_template(
            "dashboard.html",
            rows=rows,
            counts=counts,
            statuses=STATUSES,
            platforms=PLATFORMS,
            status=status,
            platform=platform,
            query=query,
            page=page,
            pages=pages,
            total=total,
            query_string=request.query_string.decode(),
        )

    @app.post("/comments/<comment_id>/publish")
    def publish(comment_id: str):
        with db.connect() as conn:
            current = db.get_comment(conn, comment_id)
        if current is None:
            flash("Comment not found.", "error")
            return redirect(_index_url())
        if current["status"] != "failed":
            flash("Only failed comments can be retried. New replies post automatically.", "error")
            return redirect(_index_url())
        draft = request.form.get("draft_reply", "").strip() or None
        with db.connect() as conn:
            db.update_status(
                conn, comment_id, "approved", draft_reply=draft, error=""
            )
        posted = post_approved(comment_id=comment_id, include_pending=True, limit=1)
        with db.connect() as conn:
            updated = db.get_comment(conn, comment_id)
        if posted:
            flash("Reply posted.", "ok")
            return redirect(_index_url(status="posted"))
        if updated and updated["status"] == "already_replied":
            flash("Skipped: this account already replied.", "ok")
            return redirect(_index_url(status="already_replied"))
        if updated and updated["status"] == "failed":
            flash(updated["error"] or "Posting failed.", "error")
            return redirect(_index_url(status="failed"))
        flash("Nothing was posted. The comment may have been skipped this run.", "error")
        return redirect(_index_url())

    @app.get("/health")
    def health():
        return {"status": "ok"}

    register_meta_routes(app)

    return app


app = create_app()


def require_port(host: str, port: int) -> None:
    """Fail if the configured dashboard port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"Dashboard port {port} is already in use on {host}. "
                "Stop the other process; this URL does not change ports."
            ) from exc


def run() -> None:
    host = config.DASHBOARD_HOST
    port = config.DASHBOARD_PORT
    require_port(host, port)
    config.require("META_APP_SECRET", "META_WEBHOOK_VERIFY_TOKEN")
    start_event_worker(app)
    print(f"Admin dashboard: http://127.0.0.1:{port}/", flush=True)
    if host in ("127.0.0.1", "localhost"):
        print(f"Chrome: http://127.0.0.1:{port}/  or  http://localhost:{port}/", flush=True)
    ipv4 = make_server(host, port, app, threaded=True)
    if host in ("127.0.0.1", "localhost"):
        try:
            ipv6 = make_server("::1", port, app, threaded=True)
            threading.Thread(target=ipv6.serve_forever, daemon=True).start()
        except OSError:
            pass
    ipv4.serve_forever()
