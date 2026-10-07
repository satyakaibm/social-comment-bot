import socket
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlsplit

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app import mailer
from werkzeug.serving import make_server

from app import analytics, config, db, security_headers
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
# Display names for the stored status values, which are a database/URL
# contract and so stay as they are. Only statuses whose raw name misleads
# need an entry; anything else falls back to the underscores-to-spaces
# rendering in status_label().
#
# "already_replied" read as "ALREADY REPLIED" in the status strip, which
# sounds like a count of replies the bot sent -- the opposite of what it
# is. These are comments the bot deliberately left alone because your own
# account had answered them first, so no reply was sent for any of them.
STATUS_LABELS = {
    "already_replied": "Answered by you",
}
# Every Momentum analysis looks back exactly as far as we are allowed to
# retain the data behind it (YouTube Developer Policy III.E.4).
MOMENTUM_HORIZON = timedelta(days=config.YOUTUBE_STATS_MAX_RETENTION_DAYS)
PAGE_SIZE = 50
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

# Accepted profile-photo formats, identified by their leading bytes rather than
# the uploaded filename or Content-Type, both of which the client controls.
# No SVG (script-capable) and no re-encoding library in the image; the browser
# scales the stored file with object-fit.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
)
_AVATAR_MIMETYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    for signature, ext, mimetype in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext, mimetype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def email_verification_state(user) -> dict:
    """What the profile page needs to know about the user's email status."""
    if user is None or not user["email"]:
        return {"verified": False, "pending": False, "can_resend": True, "resend_in": 0}
    pending = bool(user["email_code_hash"]) and (
        (user["email_code_target"] or "").casefold() == (user["email"] or "").casefold()
    )
    expires = _parse_ts(user["email_code_expires_at"])
    if pending and expires is not None and expires < _utcnow():
        pending = False
    sent = _parse_ts(user["email_code_sent_at"])
    resend_in = 0
    if sent is not None:
        elapsed = (_utcnow() - sent).total_seconds()
        resend_in = max(0, int(config.EMAIL_CODE_RESEND_SECONDS - elapsed))
    sent_label = ""
    if pending and sent is not None:
        sent_label = sent.astimezone(IST).strftime("%H:%M IST on %d %b")
    return {
        "verified": bool(user["email_verified_at"]),
        "pending": pending,
        "can_resend": resend_in == 0,
        "resend_in": resend_in,
        "sent_label": sent_label,
        "sent_to": user["email_code_target"] if pending else "",
    }



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
    """Return ``target`` only if it is a same-origin absolute path, else "/".

    The old check only refused a leading "//". Browsers normalise backslashes
    in a Location header to forward slashes, so ``/\\evil.com`` is followed as
    ``//evil.com``, a scheme-relative off-site redirect, even though urlsplit
    sees no netloc in it. Reject backslashes and control characters outright,
    then confirm the remainder still parses with no scheme or host.
    """
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    if "\\" in target or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


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
    page_key = request.args.get("page_key", "").strip()
    if page_key not in config.PAGES:
        page_key = ""
    query = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    sort_order = request.args.get("sort", "desc").lower()
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"
    return status, platform, page_key, query, page, sort_order


def _index_url(**overrides) -> str:
    status, platform, page_key, query, page, sort_order = _filters()
    params = {
        "status": overrides.get("status", status),
        "platform": overrides.get("platform", platform),
        "page_key": overrides.get("page_key", page_key),
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
    page = config.PAGES.get(item.get("page_key") or "")
    item["page_label"] = page.label if page else ""
    return item


def _top_fan_row(row: dict) -> dict:
    item = dict(row)
    page = config.PAGES.get(item.get("page_key") or "")
    item["page_label"] = page.label if page else ""
    return item


def _video_stats_row(row: dict) -> dict:
    item = dict(row)
    item["container_label"] = CONTAINER_LABELS.get(item["platform"], "Post")
    item["video_title"] = (item.get("video_title") or "")[:80]
    item["page_key"] = item.get("page_key") or config.DEFAULT_PAGE_KEY
    page = config.PAGES.get(item["page_key"])
    item["page_label"] = page.label if page else ""
    return item


def _quota_cards(conn, platform: str, page_key: str = "") -> list[dict]:
    """Quota cards for the selected platform(s), scoped to the selected
    channel when one is picked -- consistent with every other section of
    the dashboard, which already filters by the page_key switcher."""
    selected = (platform,) if platform else ()
    cards = []
    youtube_period = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    def _scoped_keys(all_keys: list[str]) -> list[str]:
        if page_key:
            return [k for k in all_keys if k == page_key]
        # No channel filter and no pages configured for this platform at all
        # (fresh checkout / test env) -- still show one unlabeled card,
        # matching pre-multi-page behavior, instead of rendering nothing.
        return all_keys or [config.DEFAULT_PAGE_KEY]

    for name in selected:
        if name == "youtube":
            period = youtube_period
            all_keys = config.youtube_page_keys()
            multi = len(all_keys) > 1
            # Each YouTube channel has its own separate Google Cloud project
            # and its own separate 10,000-unit daily quota -- show one card
            # per channel instead of one combined (and misleadingly shared)
            # total, matching how Facebook/Instagram already do this below.
            for key in _scoped_keys(all_keys):
                quota_name = name if key == config.DEFAULT_PAGE_KEY else f"{name}:{key}"
                row = db.get_quota_usage(conn, quota_name, period)
                used = int(row["used"]) if row else 0
                limit_value = int(row["limit_value"]) if row else config.YOUTUBE_DAILY_QUOTA_LIMIT
                cards.append({
                    "platform": name,
                    "page_label": config.PAGES[key].label if multi else "",
                    "used": used if row else None,
                    "remaining": max(0, limit_value - used) if row else None,
                    "limit": limit_value,
                    "unit": "units",
                    "description": "Tracked today by this bot; YouTube resets at midnight Pacific Time.",
                })
            continue
        all_keys = (
            config.facebook_page_keys() if name == "facebook" else config.instagram_page_keys()
        )
        multi = len(all_keys) > 1
        for key in _scoped_keys(all_keys):
            quota_name = name if key == config.DEFAULT_PAGE_KEY else f"{name}:{key}"
            row = db.get_quota_usage(conn, quota_name, "rolling")
            used = int(row["used"]) if row else 0
            limit_value = int(row["limit_value"]) if row else 100
            cards.append({
                "platform": name,
                "page_label": config.PAGES[key].label if multi else "",
                "used": used if row else None,
                "remaining": max(0, limit_value - used) if row else None,
                "limit": limit_value,
                "unit": "%",
                "description": "Latest rolling usage reported by Meta; Facebook and Instagram limits are dynamic.",
            })
    return cards


def _page_choices(platform: str) -> list:
    if platform == "facebook":
        page_choice_keys = config.facebook_page_keys()
    elif platform == "instagram":
        page_choice_keys = config.instagram_page_keys()
    elif platform == "youtube":
        page_choice_keys = config.youtube_page_keys()
    else:
        page_choice_keys = config.all_page_keys()
    return [config.PAGES[k] for k in page_choice_keys]


def _selected_page_label(page_key: str, platform: str) -> str:
    """Label for the channel indicator at the top of the banner.

    "All channels" only means something when there is more than one channel to
    aggregate. A single-channel instance -- every billing tenant is one -- has
    nothing to aggregate, so name the channel instead of describing the set.
    """
    if page_key in config.PAGES:
        return config.PAGES[page_key].label
    choices = _page_choices(platform)
    if len(choices) == 1:
        return choices[0].label
    return "All channels"


def create_app() -> Flask:
    app = Flask(__name__)
    # Bounds every request body; the only large upload is the profile photo,
    # which is capped separately at config.AVATAR_MAX_BYTES after sniffing.
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.secret_key = config.DASHBOARD_SECRET
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.DASHBOARD_COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.DASHBOARD_SESSION_HOURS),
    )
    # The templates' inline <script> blocks each carry nonce="{{ csp_nonce() }}";
    # tests/test_security_headers.py fails if a bare <script> slips back in.
    security_headers.install(
        app, hsts=config.DASHBOARD_COOKIE_SECURE, inline_scripts=True
    )
    @app.template_filter("status_label")
    def status_label(status: str) -> str:
        """Human name for a status, left for CSS text-transform to case.

        The fallback is deliberately the bare underscores-to-spaces form the
        templates used before STATUS_LABELS existed, so unlabelled statuses
        render byte-for-byte as they always have.
        """
        return STATUS_LABELS.get(status) or (status or "").replace("_", " ")

    db.init_db()
    with db.connect() as conn:
        db.initialize_dashboard_auth(conn, config.DASHBOARD_PASSWORD_HASH)
        legacy_auth = db.get_dashboard_auth(conn)
        db.initialize_dashboard_user(
            conn,
            config.DASHBOARD_USERNAME,
            legacy_auth["password_hash"] if legacy_auth else config.DASHBOARD_PASSWORD_HASH,
        )
        db.ensure_dashboard_admin(conn, config.DASHBOARD_USERNAME)

    @contextmanager
    def request_db():
        """Reuse the expensive SQLCipher connection for the whole request."""
        if "dashboard_db" not in g:
            g.dashboard_db = db.open_connection()
        try:
            yield g.dashboard_db
            g.dashboard_db.commit()
        except Exception:
            g.dashboard_db.rollback()
            raise

    @app.teardown_appcontext
    def close_request_db(_error=None):
        conn = g.pop("dashboard_db", None)
        if conn is not None:
            conn.close()

    def dashboard_auth(username: str | None = None):
        username = username or session.get("dashboard_username", "")
        with request_db() as conn:
            return db.get_dashboard_user(conn, username) if username else None

    @app.context_processor
    def _profile_context():
        """Avatar + verification state for the shared header on every page.

        One small query per rendered page, keyed on the session user; pages
        rendered without a session (login) get nothing and fall back to the
        initial-letter badge.
        """
        username = session.get("dashboard_username")
        if not username or not session.get("dashboard_authenticated"):
            return {}
        try:
            user = dashboard_auth(username)
        except Exception:
            return {}
        if user is None:
            return {}
        avatar_url = (
            url_for("profile_avatar", username=user["username"], v=user["updated_at"])
            if user["avatar_path"] else None
        )
        return {
            "profile_avatar_url": avatar_url,
            "profile_email_verified": bool(user["email_verified_at"]),
        }

    @app.errorhandler(413)
    def _too_large(_error):
        auth = dashboard_auth() if session.get("dashboard_username") else None
        if auth is None:
            return "Request too large", 413
        return render_template(
            "profile.html", user=auth, verification=email_verification_state(auth),
            mail_configured=mailer.configured(),
            error="That photo is too large. Profile photos must be 1 MB or smaller.",
        ), 413

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
            "login", "static", "health_api", "favicon",
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
            signed_out=request.args.get("signed_out") == "1",
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
            with request_db() as conn:
                created = db.create_dashboard_user(
                    conn, username, generate_password_hash(password)
                )
            if not created:
                return render_template(
                    "signup.html", error="That User ID is already registered.", username=username
                ), 409
            return redirect(url_for("settings", user_created="1"))
        return render_template("signup.html", username="")

    @app.route("/profile/password", methods=("GET", "POST"))
    def reset_password():
        auth = dashboard_auth()
        is_admin = bool(auth and auth["is_admin"])
        with request_db() as conn:
            portal_users = db.list_dashboard_users(conn) if is_admin else []

        def render(**kwargs):
            return render_template(
                "reset_password.html",
                password_hint=PASSWORD_HINT,
                is_admin=is_admin,
                portal_users=portal_users,
                own_username=session.get("dashboard_username", ""),
                **kwargs,
            )

        if request.method == "POST":
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            if not csrf_valid:
                return render(error="Your session expired. Please try again."), 400
            new = request.form.get("new_password", "")
            confirmation = request.form.get("confirm_password", "")

            if is_admin:
                # Admins pick who they're resetting from a dropdown of every
                # portal user (including themselves). Only the self case
                # still requires the current password -- current-password
                # verification is what stops a hijacked session from
                # silently locking out the real admin, so it must stay for
                # the admin's own account even though it's skipped when
                # resetting someone else's.
                target_username = request.form.get("target_username", "").strip()
                with request_db() as conn:
                    target = db.get_dashboard_user(conn, target_username) if target_username else None
                if target is None:
                    return render(error=f"No portal user found with User ID {target_username!r}."), 400
                resetting_self = target["username"].casefold() == (session.get("dashboard_username") or "").casefold()
                if resetting_self:
                    current = request.form.get("current_password", "")
                    if auth is None or not check_password_hash(auth["password_hash"], current):
                        return render(error="Current password is incorrect."), 400
                if not password_meets_policy(new):
                    return render(error=PASSWORD_HINT), 400
                if new != confirmation:
                    return render(error="New passwords do not match."), 400
                with request_db() as conn:
                    db.update_dashboard_user_password(
                        conn, target["username"], generate_password_hash(new)
                    )
                if resetting_self:
                    session.clear()
                    return redirect(url_for("login", password_changed="1"))
                flash(f"Password updated for {target['username']}.", "ok")
                return redirect(url_for("reset_password"))

            current = request.form.get("current_password", "")
            if auth is None or not check_password_hash(auth["password_hash"], current):
                return render(error="Current password is incorrect."), 400
            if not password_meets_policy(new):
                return render(error=PASSWORD_HINT), 400
            if new != confirmation:
                return render(error="New passwords do not match."), 400
            with request_db() as conn:
                db.update_dashboard_user_password(
                    conn, session["dashboard_username"], generate_password_hash(new)
                )
            session.clear()
            return redirect(url_for("login", password_changed="1"))
        return render()

    def _render_profile(user, status=200, **kwargs):
        return render_template(
            "profile.html",
            user=user,
            verification=email_verification_state(user),
            mail_configured=mailer.configured(),
            **kwargs,
        ), status

    def _csrf_ok() -> bool:
        return hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        )

    def _send_verification_code(conn, user) -> str | None:
        """Generate, store and email a code. Returns an error message or None."""
        if not mailer.configured():
            return (
                "Email verification is not set up on this portal yet: SMTP_HOST and "
                "MAIL_FROM must be configured before codes can be sent."
            )
        state = email_verification_state(user)
        if not state["can_resend"]:
            return f"A code was sent moments ago. You can request another in {state['resend_in']}s."
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires_at = (_utcnow() + timedelta(minutes=config.EMAIL_CODE_TTL_MINUTES)).isoformat()
        try:
            mailer.send(
                to=user["email"],
                subject="Your verification code",
                body=(
                    f"Hi {user['display_name'] or user['username']},\n\n"
                    f"Your email verification code is: {code}\n\n"
                    f"It expires in {config.EMAIL_CODE_TTL_MINUTES} minutes. If you did not "
                    "request this, you can ignore this message.\n"
                ),
            )
        except mailer.MailError as exc:
            app.logger.warning("Verification email failed for %s: %s", user["username"], exc)
            return "The verification email could not be sent. Check the portal's SMTP settings and try again."
        db.store_email_code(
            conn, user["username"], code_hash=_code_hash(code),
            target_email=user["email"], expires_at=expires_at,
        )
        # The SMTP server accepted the message; what happens after that
        # (spam folder, greylisting) is invisible to us, so leave the one
        # fact we do know in the log where it can be found later.
        app.logger.info(
            "Verification code sent for %s to %s via %s (expires %s)",
            user["username"], user["email"], config.SMTP_HOST, expires_at,
        )
        return None

    def _store_avatar(auth, data: bytes) -> str | None:
        """Validate and write a profile photo; returns an error message or None."""
        if len(data) > config.AVATAR_MAX_BYTES:
            return "That photo is too large. Profile photos must be 1 MB or smaller."
        sniffed = _sniff_image(data)
        if sniffed is None:
            return "The photo must be a PNG, JPEG or WebP image."
        ext, _mimetype = sniffed
        config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{auth['id']}.{ext}"
        # Remove any previous photo with a different extension before
        # writing, so one user never has two files on disk.
        for stale in config.AVATAR_DIR.glob(f"{auth['id']}.*"):
            if stale.name != filename:
                stale.unlink(missing_ok=True)
        (config.AVATAR_DIR / filename).write_bytes(data)
        with request_db() as conn:
            db.set_dashboard_user_avatar(conn, auth["username"], filename)
        return None

    @app.route("/profile", methods=("GET", "POST"))
    def profile():
        auth = dashboard_auth()
        if request.method == "POST":
            display_name = request.form.get("display_name", "").strip()
            email = request.form.get("email", "").strip()
            details = {name: request.form.get(name, auth[name] or "").strip()
                       for name in ("current_location", "phone_number", "facebook_page_link", "instagram_page_link", "youtube_page_link", "tiktok_page_link")}
            submitted_user = {**dict(auth), "display_name": display_name, "email": email, **details}
            if not _csrf_ok():
                return _render_profile(submitted_user, 400, error="Your session expired. Please try again.")
            if len(display_name) > 100:
                return _render_profile(submitted_user, 400, error="Display name cannot exceed 100 characters.")
            if email and (len(email) > 254 or not EMAIL_PATTERN.fullmatch(email)):
                return _render_profile(submitted_user, 400, error="Enter a valid email address.")
            labels = {
                "current_location": (150, "Current location"),
                "phone_number": (40, "Phone number"),
                "facebook_page_link": (2048, "Facebook page link"),
                "instagram_page_link": (2048, "Instagram page link"),
                "youtube_page_link": (2048, "YouTube page link"),
                "tiktok_page_link": (2048, "TikTok page link"),
            }
            for name, (limit, label) in labels.items():
                value = details[name]
                if len(value) > limit:
                    return _render_profile(submitted_user, 400, error=f"{label} cannot exceed {limit} characters.")
                if name.endswith("_link") and value:
                    try:
                        link = urlsplit(value)
                        valid_link = link.scheme in ("http", "https") and bool(link.hostname) and not link.username and not link.password
                    except ValueError:
                        valid_link = False
                    if not valid_link:
                        return _render_profile(submitted_user, 400, error=f"Enter a valid {label.lower()} starting with https:// or http://.")
            # One form saves everything: a photo is optional and travels in
            # the same request, so there is no second submit button to
            # mistake for Save. A missing or empty file field is simply
            # "no new photo".
            upload = request.files.get("avatar")
            photo = upload.read(config.AVATAR_MAX_BYTES + 1) if upload is not None and upload.filename else b""
            if photo:
                problem = _store_avatar(auth, photo)
                if problem:
                    return _render_profile(submitted_user, 400, error=problem)
            email_changed = (auth["email"] or "").strip().casefold() != email.casefold()
            with request_db() as conn:
                if db.dashboard_email_registered(
                    conn, email, excluding_username=session["dashboard_username"]
                ):
                    return _render_profile(
                        submitted_user, 409,
                        error="This email address is already registered to another account.",
                    )
                updated = db.update_dashboard_user_profile(
                    conn, session["dashboard_username"], display_name=display_name, email=email, **details,
                )
                if not updated:
                    return _render_profile(
                        submitted_user, 409,
                        error="This email address is already registered to another account.",
                    )
                auth = db.get_dashboard_user(conn, session["dashboard_username"])
                notice = None
                if email_changed and email:
                    # A new address starts unverified; send its code right away
                    # so the user can finish in one visit.
                    notice = _send_verification_code(conn, auth)
                    auth = db.get_dashboard_user(conn, session["dashboard_username"])
            if email_changed and email:
                if notice:
                    return _render_profile(auth, 200, success="Profile details updated.", error=notice)
                return _render_profile(
                    auth, 200,
                    success=f"Profile details updated. We sent a 6-digit code to {email} -- enter it below to verify the address.",
                )
            return _render_profile(
                auth, 200,
                success="Profile details and photo updated." if photo else "Profile details updated.",
            )
        return _render_profile(auth)

    @app.post("/profile/email/send-code")
    def profile_send_email_code():
        auth = dashboard_auth()
        if not _csrf_ok():
            return _render_profile(auth, 400, error="Your session expired. Please try again.")
        if not auth["email"]:
            return _render_profile(auth, 400, error="Add an email address first.")
        if auth["email_verified_at"]:
            return _render_profile(auth, 200, success="This email address is already verified.")
        with request_db() as conn:
            notice = _send_verification_code(conn, auth)
            auth = db.get_dashboard_user(conn, session["dashboard_username"])
        if notice:
            return _render_profile(auth, 400, error=notice)
        return _render_profile(auth, 200, success=f"We sent a 6-digit code to {auth['email']}.")

    @app.post("/profile/email/verify")
    def profile_verify_email():
        auth = dashboard_auth()
        if not _csrf_ok():
            return _render_profile(auth, 400, error="Your session expired. Please try again.")
        code = re.sub(r"\D", "", request.form.get("code", ""))
        state = email_verification_state(auth)
        if not state["pending"]:
            return _render_profile(auth, 400, error="No code is pending for this address. Send a new one.")
        with request_db() as conn:
            if len(code) != 6 or not hmac.compare_digest(_code_hash(code), auth["email_code_hash"]):
                attempts = db.record_email_code_attempt(conn, auth["username"])
                if attempts >= config.EMAIL_CODE_MAX_ATTEMPTS:
                    db.clear_email_code(conn, auth["username"])
                    auth = db.get_dashboard_user(conn, auth["username"])
                    return _render_profile(
                        auth, 400,
                        error="Too many incorrect codes. That code is now void -- send a new one.",
                    )
                auth = db.get_dashboard_user(conn, auth["username"])
                left = config.EMAIL_CODE_MAX_ATTEMPTS - attempts
                return _render_profile(auth, 400, error=f"That code is not correct. {left} attempt(s) left.")
            db.mark_email_verified(conn, auth["username"])
            auth = db.get_dashboard_user(conn, auth["username"])
        return _render_profile(auth, 200, success="Email address verified.")

    @app.post("/profile/avatar/remove")
    def profile_remove_avatar():
        auth = dashboard_auth()
        if not _csrf_ok():
            return _render_profile(auth, 400, error="Your session expired. Please try again.")
        if auth["avatar_path"]:
            (config.AVATAR_DIR / auth["avatar_path"]).unlink(missing_ok=True)
        with request_db() as conn:
            db.set_dashboard_user_avatar(conn, auth["username"], None)
            auth = db.get_dashboard_user(conn, auth["username"])
        return _render_profile(auth, 200, success="Profile photo removed.")

    @app.get("/profile/avatar/<username>")
    def profile_avatar(username: str):
        # Any signed-in portal user may load any user's photo (it is what the
        # header shows); nothing is served to anonymous requests.
        user = dashboard_auth(username)
        if user is None or not user["avatar_path"]:
            abort(404)
        path = config.AVATAR_DIR / os.path.basename(user["avatar_path"])
        if not path.is_file():
            abort(404)
        ext = path.suffix.lstrip(".").lower()
        response = send_file(path, mimetype=_AVATAR_MIMETYPES.get(ext, "application/octet-stream"))
        response.headers["Cache-Control"] = "private, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/logout")
    def logout():
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return "Invalid request", 400
        session.clear()
        return redirect(url_for("login", signed_out="1"))

    app.jinja_env.globals["csrf_token"] = _csrf_token
    app.jinja_env.globals["password_hint"] = PASSWORD_HINT

    @app.get("/")
    def index():
        status, platform, page_key, query, page, sort_order = _filters()
        offset = (page - 1) * PAGE_SIZE
        with request_db() as conn:
            counts = db.count_by_status(conn, platform=platform or None, page_key=page_key or None)
            activity = db.activity_summary(
                conn, platform=platform or None, page_key=page_key or None
            )
            quota_cards = _quota_cards(conn, platform, page_key)
            total = db.count_comments(
                conn,
                status=status,
                platform=platform or None,
                page_key=page_key or None,
                query=query or None,
            )
            rows = [
                _row_dict(row)
                for row in db.list_comments(
                    conn,
                    status=status,
                    platform=platform or None,
                    page_key=page_key or None,
                    query=query or None,
                    sort_order=sort_order,
                    limit=PAGE_SIZE,
                    offset=offset,
                )
            ]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page_choices = _page_choices(platform)
        selected_page_label = _selected_page_label(page_key, platform)
        return render_template(
            "dashboard.html",
            rows=rows,
            counts=counts,
            activity=activity,
            quota_cards=quota_cards,
            statuses=STATUSES,
            platforms=PLATFORMS,
            page_choices=page_choices,
            selected_page_label=selected_page_label,
            status=status,
            platform=platform,
            page_key=page_key,
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
        with request_db() as conn:
            current = db.get_comment(conn, comment_id)
        if current is None:
            flash("Comment not found.", "error")
            return redirect(_index_url())
        if current["status"] != "failed":
            flash("Only failed comments can be retried. New replies post automatically.", "error")
            return redirect(_index_url())
        draft = request.form.get("draft_reply", "").strip() or None
        with request_db() as conn:
            db.update_status(
                conn, comment_id, "approved", draft_reply=draft, error=""
            )
        posted = post_approved(comment_id=comment_id, include_pending=True, limit=1)
        with request_db() as conn:
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
        with request_db() as conn:
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

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.get("/insights")
    def insights():
        page = max(1, request.args.get("page", default=1, type=int) or 1)
        page_size = 50
        platform = request.args.get("platform", "").strip()
        if platform not in PLATFORMS:
            platform = ""
        page_key = request.args.get("page_key", "").strip()
        if page_key not in config.PAGES:
            page_key = ""
        sort_by = request.args.get("sort_by", "recent").strip()
        if sort_by not in db.VIDEO_STATS_SORT_COLUMNS:
            sort_by = "recent"
        sort_dir = request.args.get("sort_dir", "desc").lower()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "desc"
        # There is no "all time" view: YouTube Developer Policy III.E.4 caps
        # displayed statistics at 30 days, so an unrecognised (or absent)
        # window falls back to the longest period we are allowed to show
        # rather than to lifetime totals.
        window = request.args.get("window", "").strip()
        if window not in db.VIDEO_STATS_WINDOWS:
            window = db.DEFAULT_VIDEO_STATS_WINDOW
        history_warning = ""
        with request_db() as conn:
            top_fans = {
                period: [_top_fan_row(row) for row in rows]
                for period, rows in db.top_fans(
                    conn, platform=platform or None, page_key=page_key or None, limit=10
                ).items()
            }
            try:
                video_stats = [
                    _video_stats_row(row)
                    for row in db.video_stats_growth(
                        conn,
                        horizon=db.VIDEO_STATS_WINDOWS[window][1],
                        platform=platform or None,
                        page_key=page_key or None,
                    )
                ]
            except Exception as exc:
                video_stats = []
                history_warning = (
                    "Historical statistics are temporarily unavailable for this period."
                )
                app.logger.exception("Insights history query failed: %s", exc)
            for row in video_stats:
                row["view_count"] = row.get("view_growth")
                row["like_count"] = row.get("like_growth")
                row["comment_count"] = row.get("comment_growth")
                row["share_count"] = row.get("share_growth")
                row["updated_at_ist"] = row.get("period_end")
            growth_sort = {
                "recent": "period_end", "updated": "period_end",
                "views": "view_count", "likes": "like_count",
                "comments": "comment_count", "shares": "share_count",
            }[sort_by]
            measured = [row for row in video_stats if row.get(growth_sort) is not None]
            missing = [row for row in video_stats if row.get(growth_sort) is None]
            video_stats = sorted(
                measured, key=lambda row: row[growth_sort], reverse=sort_dir == "desc"
            ) + missing
        def total_for(field: str):
            values = [row[field] for row in video_stats if row[field] is not None]
            return sum(values) if values else None

        insights_summary = {
            "content": len(video_stats),
            "views": total_for("view_count"),
            "likes": total_for("like_count"),
            "comments": total_for("comment_count"),
            "shares": total_for("share_count"),
        }
        total_items = len(video_stats)
        total_pages = max(1, (total_items + page_size - 1) // page_size)
        page = min(page, total_pages)
        page_start = (page - 1) * page_size
        video_stats = video_stats[page_start:page_start + page_size]
        if platform == "youtube":
            video_stats_refresh_label = (
                "YouTube auto-refreshes every "
                f"{config.YOUTUBE_VIDEO_STATS_REFRESH_MINUTES} min"
            )
        elif platform in {"facebook", "instagram"}:
            video_stats_refresh_label = (
                "Meta auto-refreshes every "
                f"{config.META_VIDEO_STATS_REFRESH_MINUTES} min"
            )
        else:
            video_stats_refresh_label = (
                f"YouTube every {config.YOUTUBE_VIDEO_STATS_REFRESH_MINUTES} min"
                f" · Meta every {config.META_VIDEO_STATS_REFRESH_MINUTES} min"
            )
        selected_page_label = _selected_page_label(page_key, platform)
        return render_template(
            "insights.html",
            video_stats=video_stats,
            insights_summary=insights_summary,
            top_fans=top_fans,
            video_stats_refresh_label=video_stats_refresh_label,
            platforms=PLATFORMS,
            page_choices=_page_choices(platform),
            selected_page_label=selected_page_label,
            sort_by=sort_by,
            sort_dir=sort_dir,
            window=window,
            windows=db.VIDEO_STATS_WINDOWS,
            report_label=db.VIDEO_STATS_WINDOWS[window][0],
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
            page_first=(page_start + 1 if total_items else 0),
            page_last=min(page_start + page_size, total_items),
            history_warning=history_warning,
            platform=platform,
            page_key=page_key,
            dashboard_username=session["dashboard_username"],
            profile_initial=session["dashboard_username"][:1].upper(),
        )

    @app.get("/momentum")
    def momentum():
        platform = request.args.get("platform", "").strip()
        if platform not in PLATFORMS:
            platform = ""
        page_key = request.args.get("page_key", "").strip()
        if page_key not in config.PAGES:
            page_key = ""
        analysis_warning = ""
        with request_db() as conn:
            video_stats = [
                _video_stats_row(row)
                for row in db.list_video_stats(
                    conn, platform=platform or None, page_key=page_key or None,
                )
            ]
            try:
                growth_24h = [
                    _video_stats_row(row)
                    for row in db.video_stats_growth(
                        conn, horizon=timedelta(hours=24),
                        platform=platform or None, page_key=page_key or None,
                    )
                ]
                growth_7d = [
                    _video_stats_row(row)
                    for row in db.video_stats_growth(
                        conn, horizon=timedelta(days=7),
                        platform=platform or None, page_key=page_key or None,
                    )
                ]
            except Exception as exc:
                growth_24h = []
                growth_7d = []
                analysis_warning = (
                    "Historical momentum is temporarily unavailable. Current creator "
                    "and audience analysis is still shown."
                )
                app.logger.exception("Historical momentum query failed: %s", exc)
            # 30 days, not 90: YouTube Developer Policy III.E.4 caps how long
            # retrieved statistics may be stored or displayed, and
            # engagement_activity reads video_stats_history, which is now
            # pruned at that same boundary. Keeping the three momentum
            # horizons equal to the retention window stops the page claiming
            # a depth of evidence the database no longer holds.
            activity_rows = db.audience_activity(
                conn, horizon=MOMENTUM_HORIZON,
                platform=platform or None, page_key=page_key or None,
            )
            try:
                engagement_rows = db.engagement_activity(
                    conn, horizon=MOMENTUM_HORIZON,
                    platform=platform or None, page_key=page_key or None,
                )
            except Exception as exc:
                engagement_rows = []
                app.logger.exception("Engagement timing query failed: %s", exc)
            intent_rows = db.comment_text_sample(
                conn, horizon=MOMENTUM_HORIZON,
                platform=platform or None, page_key=page_key or None,
            )
            experiments = db.list_recommendation_experiments(
                conn, platform=platform or None, page_key=page_key or None
            )
            online_rows = (
                db.list_audience_online(
                    conn, platform="instagram", page_key=page_key or None, days=28
                )
                if platform in ("", "instagram")
                else []
            )
        for row in activity_rows + intent_rows + engagement_rows:
            row["page_key"] = row.get("page_key") or config.DEFAULT_PAGE_KEY
        creator_recommendations = analytics.creator_focus(video_stats)
        momentum_recommendations = (
            analytics.momentum_focus(growth_24h, period_label="24-hour")
            + analytics.momentum_focus(growth_7d, period_label="7-day")
        )
        audience_timing = analytics.audience_timing_focus(
            activity_rows,
            engagement_rows=engagement_rows,
            quality_rows=intent_rows,
        )
        audience_intents = analytics.comment_intent_focus(intent_rows)
        instagram_online = analytics.instagram_online_focus(online_rows)
        for item in audience_timing + audience_intents + instagram_online:
            page = config.PAGES.get(item.get("page_key") or "")
            item["page_label"] = page.label if page else "Default channel"
        for kind, recommendations in (
            ("current", creator_recommendations),
            ("momentum", momentum_recommendations),
            ("timing", audience_timing + instagram_online),
            ("intent", audience_intents),
        ):
            for item in recommendations:
                item["recommendation_type"] = kind
                item["recommendation_key"] = analytics.recommendation_key(kind, item)
        for item in experiments:
            page = config.PAGES.get(item.get("page_key") or config.DEFAULT_PAGE_KEY)
            item["page_label"] = page.label if page else "Default channel"
        trend_index: dict[tuple[str, str, str], dict] = {}
        for period, rows in (("24h", growth_24h), ("7d", growth_7d)):
            for row in rows:
                identity = (
                    row.get("page_key") or "",
                    row["platform"],
                    row["video_id"],
                )
                trend = trend_index.setdefault(
                    identity,
                    {
                        "page_label": row.get("page_label") or "Default channel",
                        "platform": row["platform"],
                        "video_id": row["video_id"],
                        "video_title": row.get("video_title") or row["video_id"],
                        **{
                            f"{window}_{metric}_growth": None
                            for window in ("24h", "7d")
                            for metric in ("view", "like", "comment", "share")
                        },
                        "24h_sample_count": 0,
                        "24h_elapsed_hours": 0,
                        "7d_sample_count": 0,
                        "7d_elapsed_hours": 0,
                    },
                )
                for metric in ("view", "like", "comment", "share"):
                    trend[f"{period}_{metric}_growth"] = row.get(f"{metric}_growth")
                trend[f"{period}_sample_count"] = row.get("sample_count") or 0
                trend[f"{period}_elapsed_hours"] = row.get("elapsed_hours") or 0
        trend_rows = sorted(
            trend_index.values(),
            key=lambda row: (
                row.get("24h_view_growth") is not None,
                row.get("24h_view_growth") or 0,
                row.get("7d_view_growth") is not None,
                row.get("7d_view_growth") or 0,
            ),
            reverse=True,
        )[:12]
        confidence_items = (
            momentum_recommendations + audience_timing + audience_intents
        )
        confidence_summary = {
            level: sum(item.get("confidence") == level for item in confidence_items)
            for level in ("high", "medium", "early")
        }
        analysis_summary = {
            "content": len(video_stats),
            "channels": len({
                (row.get("page_key") or "", row["platform"]) for row in video_stats
            }),
            "history_samples": sum(int(row.get("sample_count") or 0) for row in growth_7d),
            "audience_comments": sum(
                int(item.get("comment_count") or 0) for item in audience_timing
            ),
            "active_experiments": sum(
                item.get("status") not in {"completed", "ignored"} for item in experiments
            ),
        }
        return render_template(
            "momentum.html",
            creator_recommendations=creator_recommendations,
            momentum_recommendations=momentum_recommendations,
            audience_timing=audience_timing,
            instagram_online=instagram_online,
            audience_intents=audience_intents,
            experiments=experiments,
            trend_rows=trend_rows,
            confidence_summary=confidence_summary,
            analysis_summary=analysis_summary,
            analysis_warning=analysis_warning,
            platforms=PLATFORMS,
            page_choices=_page_choices(platform),
            selected_page_label=_selected_page_label(page_key, platform),
            platform=platform,
            page_key=page_key,
            dashboard_username=session["dashboard_username"],
            profile_initial=session["dashboard_username"][:1].upper(),
        )

    @app.post("/insights/experiments")
    @app.post("/momentum/experiments")
    def create_insights_experiment():
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return "Invalid request", 400
        kind = request.form.get("recommendation_type", "")
        platform = request.form.get("platform", "")
        page_key = request.form.get("page_key", "") or config.DEFAULT_PAGE_KEY
        test_dimension = request.form.get("test_dimension", "")
        if kind not in {"current", "momentum", "timing", "intent"}:
            return "Invalid recommendation type", 400
        if platform not in PLATFORMS or page_key not in config.PAGES:
            return "Invalid recommendation scope", 400
        if test_dimension not in {"", "title", "topic", "format", "publish_time"}:
            return "Invalid test dimension", 400
        item = {
            "platform": platform,
            "page_key": page_key,
            "video_id": request.form.get("video_id", "")[:200],
            "period_label": request.form.get("period_label", "")[:50],
            "message": request.form.get("recommendation", "")[:1000],
        }
        expected_key = analytics.recommendation_key(kind, item)
        if not hmac.compare_digest(request.form.get("recommendation_key", ""), expected_key):
            return "Invalid recommendation", 400
        with request_db() as conn:
            created = db.create_recommendation_experiment(
                conn,
                recommendation_key=expected_key,
                recommendation_type=kind,
                platform=platform,
                page_key=page_key,
                video_id=item["video_id"] or None,
                title=request.form.get("title", "")[:200],
                recommendation=item["message"],
                test_dimension=test_dimension,
                variant_label=request.form.get("variant_label", "")[:100].strip(),
                notes=request.form.get("notes", "")[:500].strip(),
            )
        flash("Recommendation added to outcome tracking." if created else "Recommendation is already tracked.", "ok")
        return redirect(url_for("momentum"))

    @app.post("/insights/experiments/<int:experiment_id>/status")
    @app.post("/momentum/experiments/<int:experiment_id>/status")
    def update_insights_experiment(experiment_id: int):
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return "Invalid request", 400
        with request_db() as conn:
            updated = db.update_recommendation_experiment_status(
                conn, experiment_id, request.form.get("status", "")
            )
        if not updated:
            return "Invalid experiment or status", 400
        flash("Recommendation outcome updated.", "ok")
        return redirect(url_for("momentum"))

    @app.get("/settings")
    def settings():
        auth = dashboard_auth()
        is_admin = bool(auth and auth["is_admin"])
        with request_db() as conn:
            portal_users = db.list_dashboard_users(conn) if is_admin else []
        return render_template(
            "settings.html",
            user_created=request.args.get("user_created") == "1",
            is_admin=is_admin,
            portal_users=portal_users,
            own_username=session.get("dashboard_username", ""),
            settings_user=auth,
        )

    @app.post("/settings/admin")
    def toggle_admin():
        auth = dashboard_auth()
        if not (auth and auth["is_admin"]):
            return "Forbidden", 403
        csrf_valid = hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        )
        if not csrf_valid:
            flash("Your session expired. Please try again.", "error")
            return redirect(url_for("settings"))
        target_username = request.form.get("target_username", "").strip()
        make_admin = request.form.get("action") == "promote"
        with request_db() as conn:
            target = db.get_dashboard_user(conn, target_username) if target_username else None
            if target is None:
                flash(f"No portal user found with User ID {target_username!r}.", "error")
                return redirect(url_for("settings"))
            # Demoting the sole remaining admin would lock everyone out of
            # this page -- and of resetting anyone's password -- with no
            # way back in short of editing the database directly.
            if not make_admin and target["is_admin"] and db.count_dashboard_admins(conn) <= 1:
                flash(
                    f"Can't remove admin from {target['username']} -- "
                    "they're the only admin left.",
                    "error",
                )
                return redirect(url_for("settings"))
            db.set_dashboard_user_admin(conn, target["username"], make_admin)
        flash(
            f"{target['username']} is {'now an admin' if make_admin else 'no longer an admin'}.",
            "ok",
        )
        return redirect(url_for("settings"))

    @app.get("/status")
    def status():
        with request_db() as conn:
            heartbeats = db.list_heartbeats(conn)
            webhook_counts = db.webhook_event_counts(conn)
            quota_cards = (
                _quota_cards(conn, "youtube")
                + _quota_cards(conn, "facebook")
                + _quota_cards(conn, "instagram")
            )
        now = datetime.now(timezone.utc)
        for beat in heartbeats:
            last_run = datetime.fromisoformat(beat["last_run_at"])
            beat["seconds_ago"] = max(0, int((now - last_run).total_seconds()))
        try:
            db_size_mb = round(os.path.getsize(db.DB_PATH) / (1024 * 1024), 1)
        except OSError:
            db_size_mb = None
        return render_template(
            "status.html",
            heartbeats=heartbeats,
            webhook_counts=webhook_counts,
            quota_cards=quota_cards,
            db_size_mb=db_size_mb,
            dashboard_username=session["dashboard_username"],
            profile_initial=session["dashboard_username"][:1].upper(),
        )

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
    config.validate_runtime_security()
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
    config.validate_runtime_security()
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
