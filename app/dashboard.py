import socket
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

from app import config, db
from app.password_policy import PASSWORD_HINT, password_meets_policy
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
PAGE_SIZE = 100
CONTAINER_LABELS = {
    "youtube": "Video",
    "facebook": "Facebook post",
    "instagram": "Instagram post",
}
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
FAILED_RETRY_LIMIT = 50
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()
_failed_retry_lock = threading.Lock()
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _retry_failed_batch(platforms: tuple[str, ...]) -> None:
    log_path = config.DATA_DIR / "polling.log"
    started = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
    with log_path.open("a", encoding="utf-8") as activity_log:
        activity_log.write(
            f"\n==== Dashboard failed-reply retry started: {started}; "
            f"platforms: {', '.join(platforms)} ====\n"
        )
    try:
        for platform in platforms:
            post_approved(
                platform=platform,
                only_failed=True,
                like_comments=platform in ("facebook", "instagram"),
                limit=FAILED_RETRY_LIMIT,
                activity_log_path=log_path,
            )
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as activity_log:
            activity_log.write(f"Dashboard failed-reply retry stopped: {exc}\n")
    finally:
        finished = datetime.now(ZoneInfo("Asia/Kolkata")).strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )
        with log_path.open("a", encoding="utf-8") as activity_log:
            activity_log.write(
                f"==== Dashboard failed-reply retry finished: {finished} ====\n"
            )
        _failed_retry_lock.release()


def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _safe_next(target: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else "/"


def _client_ip() -> str:
    # Behind the Cloudflare Tunnel, request.remote_addr is always the Docker
    # bridge gateway, so every visitor would otherwise share one rate-limit
    # bucket. CF-Connecting-IP is set by Cloudflare's edge and cannot be
    # spoofed by the client, so it reflects the real visitor IP.
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"


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
    sort_order = request.args.get("sort", "desc").lower()
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"
    return status, platform, query, page, sort_order


def _index_url(**overrides) -> str:
    status, platform, query, page, sort_order = _filters()
    params = {
        "status": overrides.get("status", status),
        "platform": overrides.get("platform", platform),
        "q": overrides.get("q", query),
        "page": str(overrides.get("page", page)),
        "sort": overrides.get("sort", sort_order),
    }
    cleaned = {
        key: value
        for key, value in params.items()
        if value
        and not (key == "page" and value == "1")
        and not (key == "sort" and value == "desc")
    }
    return "/?" + urlencode(cleaned) if cleaned else "/"


def _row_dict(row) -> dict:
    item = dict(row)
    item["container_label"] = CONTAINER_LABELS.get(item["platform"], "Post")
    item["video_title"] = (item.get("video_title") or "")[:50]
    return item


def _quota_cards(conn, platform: str) -> list[dict]:
    selected = (platform,) if platform else ()
    cards = []
    youtube_period = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    for name in selected:
        period = youtube_period if name == "youtube" else "rolling"
        row = db.get_quota_usage(conn, name, period)
        used = int(row["used"]) if row else 0
        limit_value = int(row["limit_value"]) if row else (
            config.YOUTUBE_DAILY_QUOTA_LIMIT if name == "youtube" else 100
        )
        cards.append({
            "platform": name,
            "used": used if row else None,
            "remaining": max(0, limit_value - used) if row else None,
            "limit": limit_value,
            "unit": "units" if name == "youtube" else "%",
            "description": (
                "Tracked today by this bot; YouTube resets at midnight Pacific Time."
                if name == "youtube" else
                "Latest rolling usage reported by Meta; Facebook and Instagram limits are dynamic."
            ),
        })
    return cards


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
        legacy_auth = db.get_dashboard_auth(conn)
        db.initialize_dashboard_user(
            conn,
            config.DASHBOARD_USERNAME,
            legacy_auth["password_hash"] if legacy_auth else config.DASHBOARD_PASSWORD_HASH,
        )

    def dashboard_auth(username: str | None = None):
        username = username or session.get("dashboard_username", "")
        with db.connect() as conn:
            return db.get_dashboard_user(conn, username) if username else None

    @app.route("/favicon.ico")
    def favicon():
        # Browsers auto-fetch this on every page load, including the login
        # page. Left unauthenticated, it used to hit the 401 branch below,
        # which cleared the session and rotated the CSRF token out from
        # under the already-rendered login form, breaking every login
        # attempt with "Invalid username or password" regardless of
        # whether the credentials were correct.
        return "", 204

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", updated_at="September 10, 2026")

    @app.get("/terms")
    def terms():
        return render_template("terms.html", updated_at="September 10, 2026")

    @app.get("/data-deletion")
    @app.get("/datadeletion")
    def data_deletion():
        return render_template("data_deletion.html", updated_at="September 10, 2026")

    @app.before_request
    def require_dashboard_login():
        public = request.endpoint in {
            "login", "signup", "static", "health_api", "favicon",
            "privacy", "terms", "data_deletion",
        }
        if public or request.path.startswith("/webhooks/meta"):
            return None
        auth = dashboard_auth()
        if (
            not session.get("dashboard_authenticated")
            or auth is None
            or session.get("dashboard_auth_version") != auth["version"]
        ):
            session.clear()
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return None

    @app.route("/login", methods=("GET", "POST"))
    def login():
        client = _client_ip()
        if request.method == "POST":
            if _login_blocked(client):
                return render_template(
                    "login.html", error="Too many attempts. Try again in 15 minutes."
                ), 429
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            username = request.form.get("username", "").strip()
            auth = dashboard_auth(username)
            password_valid = bool(auth) and check_password_hash(
                auth["password_hash"], request.form.get("password", "")
            )
            if csrf_valid and password_valid:
                session.clear()
                session["dashboard_authenticated"] = True
                session["dashboard_auth_version"] = auth["version"]
                session["dashboard_username"] = auth["username"]
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
            registered=request.args.get("registered") == "1",
        )

    @app.route("/signup", methods=("GET", "POST"))
    def signup():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            if not csrf_valid:
                return render_template(
                    "signup.html", error="Your session expired. Please try again.", username=username
                ), 400
            if not USERNAME_PATTERN.fullmatch(username):
                return render_template(
                    "signup.html",
                    error="User ID must be 3–50 characters using letters, numbers, dots, hyphens, or underscores.",
                    username=username,
                ), 400
            if not password_meets_policy(password):
                return render_template(
                    "signup.html", error=PASSWORD_HINT, username=username
                ), 400
            if password != confirmation:
                return render_template(
                    "signup.html", error="Passwords do not match.", username=username
                ), 400
            with db.connect() as conn:
                created = db.create_dashboard_user(
                    conn, username, generate_password_hash(password)
                )
            if not created:
                return render_template(
                    "signup.html", error="That User ID is already registered.", username=username
                ), 409
            return redirect(url_for("login", registered="1"))
        return render_template("signup.html", username="")

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
            if not password_meets_policy(new):
                return render_template(
                    "reset_password.html",
                    error=PASSWORD_HINT,
                    password_hint=PASSWORD_HINT,
                ), 400
            if new != confirmation:
                return render_template(
                    "reset_password.html", error="New passwords do not match."
                ), 400
            with db.connect() as conn:
                db.update_dashboard_user_password(
                    conn, session["dashboard_username"], generate_password_hash(new)
                )
            session.clear()
            return redirect(url_for("login", password_changed="1"))
        return render_template("reset_password.html", password_hint=PASSWORD_HINT)

    @app.route("/profile", methods=("GET", "POST"))
    def profile():
        auth = dashboard_auth()
        if request.method == "POST":
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            display_name = request.form.get("display_name", "").strip()
            email = request.form.get("email", "").strip()
            submitted_user = {
                **dict(auth),
                "display_name": display_name,
                "email": email,
            }
            if not csrf_valid:
                return render_template(
                    "profile.html", user=submitted_user, error="Your session expired. Please try again."
                ), 400
            if len(display_name) > 100:
                return render_template(
                    "profile.html", user=submitted_user, error="Display name cannot exceed 100 characters."
                ), 400
            if email and (len(email) > 254 or not EMAIL_PATTERN.fullmatch(email)):
                return render_template(
                    "profile.html", user=submitted_user, error="Enter a valid email address."
                ), 400
            with db.connect() as conn:
                if db.dashboard_email_registered(
                    conn, email, excluding_username=session["dashboard_username"]
                ):
                    return render_template(
                        "profile.html",
                        user=submitted_user,
                        error="This email address is already registered to another account.",
                    ), 409
                updated = db.update_dashboard_user_profile(
                    conn,
                    session["dashboard_username"],
                    display_name=display_name,
                    email=email,
                )
                if not updated:
                    return render_template(
                        "profile.html",
                        user=submitted_user,
                        error="This email address is already registered to another account.",
                    ), 409
                auth = db.get_dashboard_user(conn, session["dashboard_username"])
            return render_template(
                "profile.html", user=auth, success="Profile details updated."
            )
        return render_template("profile.html", user=auth)

    @app.post("/logout")
    def logout():
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return "Invalid request", 400
        session.clear()
        return redirect(url_for("login"))

    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.jinja_env.globals["password_hint"] = PASSWORD_HINT

    @app.get("/")
    def index():
        status, platform, query, page, sort_order = _filters()
        offset = (page - 1) * PAGE_SIZE
        with db.connect() as conn:
            counts = db.count_by_status(conn, platform=platform or None)
            activity = db.activity_summary(conn, platform=platform or None)
            quota_cards = _quota_cards(conn, platform)
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
                    sort_order=sort_order,
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
            quota_cards=quota_cards,
            statuses=STATUSES,
            platforms=PLATFORMS,
            status=status,
            platform=platform,
            query=query,
            page=page,
            pages=pages,
            total=total,
            sort_order=sort_order,
            next_sort_order="asc" if sort_order == "desc" else "desc",
            query_string=request.query_string.decode(),
            dashboard_username=session["dashboard_username"],
            profile_initial=session["dashboard_username"][:1].upper(),
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

    @app.post("/comments/retry-failed")
    def retry_failed():
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return "Invalid request", 400
        selected_platform = request.form.get("platform", "").strip()
        platforms = (
            (selected_platform,) if selected_platform in PLATFORMS else PLATFORMS
        )
        with db.connect() as conn:
            failed_count = sum(
                db.count_by_status(conn, platform=name).get("failed", 0)
                for name in platforms
            )
        if not failed_count:
            flash("There are no failed replies to retry.", "ok")
            return redirect(_index_url(status="failed"))
        if not _failed_retry_lock.acquire(blocking=False):
            flash("A failed-reply check is already running.", "ok")
            return redirect(_index_url(status="failed"))
        try:
            threading.Thread(
                target=_retry_failed_batch,
                args=(platforms,),
                name="dashboard-failed-retry",
                daemon=True,
            ).start()
        except Exception:
            _failed_retry_lock.release()
            raise
        scope = selected_platform.title() if selected_platform in PLATFORMS else "all platforms"
        flash(
            f"Retry started for {scope}. Refresh this page to see updated counts.",
            "ok",
        )
        return redirect(_index_url(status="failed"))

    @app.get("/faq")
    def faq():
        return render_template("faq.html")

    @app.get("/settings")
    def settings():
        return render_template("settings.html")

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
