import socket
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

from app import config, db
from app.post import post_approved
from app.webhook import register_meta_routes, start_event_worker

STATUSES = (
    "pending_review",
    "posting",
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
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


def _login_blocked(client: str) -> bool:
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    with _login_lock:
        attempts = [value for value in _login_failures.get(client, []) if value > cutoff]
        _login_failures[client] = attempts
        return len(attempts) >= LOGIN_ATTEMPTS


def _record_login_failure(client: str) -> None:
    with _login_lock:
        _login_failures.setdefault(client, []).append(time.monotonic())


def _clear_login_failures(client: str) -> None:
    with _login_lock:
        _login_failures.pop(client, None)


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
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.DASHBOARD_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.DASHBOARD_SESSION_HOURS),
    )
    db.init_db()
    with db.connect() as conn:
        db.initialize_dashboard_auth(conn, config.DASHBOARD_PASSWORD_HASH)

    def dashboard_auth():
        with db.connect() as conn:
            return db.get_dashboard_auth(conn)

    @app.before_request
    def require_dashboard_login():
        public = request.endpoint in {"login", "static", "health_api"}
        if public or request.path.startswith("/webhooks/meta"):
            return None
        auth = dashboard_auth()
        if not config.DASHBOARD_USERNAME or auth is None:
            return render_template("login.html", configuration_missing=True), 503
        if (
            not session.get("dashboard_authenticated")
            or session.get("dashboard_auth_version") != auth["version"]
        ):
            session.clear()
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return None

    @app.route("/login", methods=("GET", "POST"))
    def login():
        auth = dashboard_auth()
        if not config.DASHBOARD_USERNAME or auth is None:
            return render_template("login.html", configuration_missing=True), 503
        client = request.remote_addr or "unknown"
        if request.method == "POST":
            if _login_blocked(client):
                return render_template(
                    "login.html", error="Too many attempts. Try again in 15 minutes."
                ), 429
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            username_valid = hmac.compare_digest(
                request.form.get("username", ""), config.DASHBOARD_USERNAME
            )
            password_valid = check_password_hash(
                auth["password_hash"], request.form.get("password", "")
            )
            if csrf_valid and username_valid and password_valid:
                session.clear()
                session["dashboard_authenticated"] = True
                session["dashboard_auth_version"] = auth["version"]
                session.permanent = True
                _clear_login_failures(client)
                return redirect(_safe_next(request.form.get("next", "/")))
            _record_login_failure(client)
            return render_template(
                "login.html", error="Invalid username or password."
            ), 401
        return render_template(
            "login.html",
            next=_safe_next(request.args.get("next", "/")),
            password_changed=request.args.get("password_changed") == "1",
        )

    @app.route("/profile/password", methods=("GET", "POST"))
    def reset_password():
        if request.method == "POST":
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            if not csrf_valid:
                return render_template(
                    "reset_password.html", error="Your session expired. Please try again."
                ), 400
            auth = dashboard_auth()
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            confirmation = request.form.get("confirm_password", "")
            if auth is None or not check_password_hash(auth["password_hash"], current):
                return render_template(
                    "reset_password.html", error="Current password is incorrect."
                ), 400
            if len(new) < 12:
                return render_template(
                    "reset_password.html",
                    error="New password must contain at least 12 characters.",
                ), 400
            if new != confirmation:
                return render_template(
                    "reset_password.html", error="New passwords do not match."
                ), 400
            with db.connect() as conn:
                db.update_dashboard_password(conn, generate_password_hash(new))
            session.clear()
            return redirect(url_for("login", password_changed="1"))
        return render_template("reset_password.html")

    @app.post("/logout")
    def logout():
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return "Invalid request", 400
        session.clear()
        return redirect(url_for("login"))

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.get("/")
    def index():
        status, platform, query, page = _filters()
        offset = (page - 1) * PAGE_SIZE
        with db.connect() as conn:
            counts = db.count_by_status(conn, platform=platform or None)
            activity = db.activity_summary(conn, platform=platform or None)
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
            activity=activity,
            statuses=STATUSES,
            platforms=PLATFORMS,
            status=status,
            platform=platform,
            query=query,
            page=page,
            pages=pages,
            total=total,
            query_string=request.query_string.decode(),
            dashboard_username=config.DASHBOARD_USERNAME,
            profile_initial=config.DASHBOARD_USERNAME.strip()[:1].upper(),
            current_year=datetime.now().year,
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
        return render_template("health.html")

    @app.get("/api/health")
    def health_api():
        return {"status": "ok"}

    register_meta_routes(app)

    return app


app = create_app()


def create_serving_app() -> Flask:
    """Build the combined dashboard and Meta webhook service for Gunicorn."""
    config.require(
        "META_APP_SECRET",
        "META_WEBHOOK_VERIFY_TOKEN",
        "DASHBOARD_USERNAME",
        "DASHBOARD_PASSWORD_HASH",
    )
    serving_app = create_app()
    start_event_worker(serving_app)
    return serving_app


def require_port(host: str, port: int) -> None:
    """Fail if the configured dashboard port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Permit an immediate restart while macOS is releasing the old socket.
        # An active listener still prevents this bind and raises the error below.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
