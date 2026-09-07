from urllib.parse import urlencode

from flask import Flask, flash, redirect, render_template, request

from app import config, db
from app.post import post_approved

STATUSES = (
    "pending_review",
    "approved",
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
    status = request.args.get("status", "pending_review")
    if status not in STATUSES:
        status = "pending_review"
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

    def _missing(comment_id: str) -> bool:
        with db.connect() as conn:
            return db.get_comment(conn, comment_id) is None

    def _save_draft_if_present(comment_id: str) -> str | None:
        if "draft_reply" not in request.form:
            return None
        draft = request.form.get("draft_reply", "").strip()
        with db.connect() as conn:
            row = db.get_comment(conn, comment_id)
            if row is None:
                return None
            db.update_status(conn, comment_id, row["status"], draft_reply=draft)
        return draft

    @app.post("/comments/<comment_id>/save")
    def save_draft(comment_id: str):
        if _missing(comment_id):
            flash("Comment not found.", "error")
            return redirect(_index_url())
        _save_draft_if_present(comment_id)
        flash("Draft saved.", "ok")
        return redirect(_index_url())

    @app.post("/comments/<comment_id>/approve")
    def approve(comment_id: str):
        if _missing(comment_id):
            flash("Comment not found.", "error")
            return redirect(_index_url())
        draft = request.form.get("draft_reply", "").strip() or None
        with db.connect() as conn:
            db.update_status(conn, comment_id, "approved", draft_reply=draft, error="")
        flash("Approved. Use Post reply here or `python -m app.cli post`.", "ok")
        return redirect(_index_url(status="approved"))

    @app.post("/comments/<comment_id>/reject")
    def reject(comment_id: str):
        if _missing(comment_id):
            flash("Comment not found.", "error")
            return redirect(_index_url())
        with db.connect() as conn:
            db.update_status(conn, comment_id, "rejected")
        flash("Rejected.", "ok")
        return redirect(_index_url(status="rejected"))

    @app.post("/comments/<comment_id>/publish")
    def publish(comment_id: str):
        if _missing(comment_id):
            flash("Comment not found.", "error")
            return redirect(_index_url())
        draft = _save_draft_if_present(comment_id)
        with db.connect() as conn:
            current = db.get_comment(conn, comment_id)
            next_status = current["status"]
            if next_status in ("pending_review", "failed", "rejected"):
                next_status = "approved"
            db.update_status(
                conn, comment_id, next_status, draft_reply=draft, error=""
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

    return app


app = create_app()


def run() -> None:
    print(
        f"Admin dashboard: http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}/ "
        "(localhost only)",
        flush=True,
    )
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False)
