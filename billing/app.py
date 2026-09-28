import hmac
import re
import secrets
import threading
import time

from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from billing import config, db, razorpay_client, tenant_ops

LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures: dict[str, list[float]] = {}
_login_lock = threading.Lock()

TENANT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _client_ip() -> str:
    # Same reasoning as app/dashboard.py's _client_ip: behind the Cloudflare
    # Tunnel, remote_addr is always the Docker bridge gateway.
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "unknown"


def _login_blocked(client: str) -> bool:
    with _login_lock:
        attempts = [t for t in _login_failures.get(client, []) if time.time() - t < LOGIN_WINDOW_SECONDS]
        _login_failures[client] = attempts
        return len(attempts) >= LOGIN_ATTEMPTS


def _record_login_failure(client: str) -> None:
    with _login_lock:
        _login_failures.setdefault(client, []).append(time.time())


def _clear_login_failures(client: str) -> None:
    with _login_lock:
        _login_failures.pop(client, None)


def _csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.BILLING_SECRET
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.BILLING_COOKIE_SECURE,
    )
    app.jinja_env.globals["csrf_token"] = _csrf_token
    db.init_db()

    @app.get("/api/health")
    def health_api():
        return {"status": "ok"}

    @app.get("/")
    def pricing():
        return render_template("pricing.html")

    @app.route("/signup", methods=("GET", "POST"))
    def signup():
        if request.method == "POST":
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            tenant_key = request.form.get("tenant_key", "").strip().lower()
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            if not csrf_valid:
                return render_template("signup.html", error="Your session expired. Please try again."), 400
            if not TENANT_KEY_PATTERN.fullmatch(tenant_key):
                return render_template(
                    "signup.html",
                    error="Workspace ID must be 3-32 characters: lowercase letters, digits, hyphens.",
                    name=name, email=email,
                ), 400
            if not name or not EMAIL_PATTERN.fullmatch(email):
                return render_template(
                    "signup.html", error="Enter your name and a valid email.",
                    tenant_key=tenant_key,
                ), 400
            with db.connect() as conn:
                if db.get_tenant(conn, tenant_key) is not None:
                    return render_template(
                        "signup.html", error="That workspace ID is taken. Pick another.",
                        name=name, email=email,
                    ), 409
                try:
                    subscription = razorpay_client.create_subscription()
                except Exception:
                    app.logger.exception("Razorpay subscription creation failed")
                    return render_template(
                        "signup.html",
                        error="Payment setup is temporarily unavailable. Please try again shortly.",
                        name=name, email=email, tenant_key=tenant_key,
                    ), 502
                port = db.next_free_port(conn)
                db.create_tenant(
                    conn,
                    tenant_key=tenant_key,
                    name=name,
                    email=email,
                    port=port,
                    razorpay_subscription_id=subscription["id"],
                )
            # short_url is Razorpay's own hosted authorization page for this
            # subscription -- simplest integration for v1. Swapping this for
            # inline Checkout.js (subscription_id option) later is a
            # signup.html/template-only change, nothing here.
            return redirect(subscription["short_url"])
        return render_template("signup.html")

    @app.post("/webhooks/razorpay")
    def razorpay_webhook():
        body = request.get_data()
        signature = request.headers.get("X-Razorpay-Signature", "")
        if not razorpay_client.verify_webhook_signature(body, signature):
            return "invalid signature", 401

        payload = request.get_json(force=True, silent=True) or {}
        event = payload.get("event", "")
        subscription = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        subscription_id = subscription.get("id", "")
        if not subscription_id:
            return "", 200

        with db.connect() as conn:
            tenant = db.get_tenant_by_subscription(conn, subscription_id)
            if tenant is None:
                return "", 200  # not one of ours (or not yet linked) -- ignore

            if event in razorpay_client.ACTIVE_EVENTS:
                if tenant["provisioned"]:
                    tenant_ops.start(tenant["tenant_key"], tenant["env_path"])
                    db.set_status(conn, tenant["tenant_key"], "active")
                else:
                    # First payment on a brand-new signup: nothing running
                    # yet, just surface it in /admin for manual provisioning.
                    db.set_status(conn, tenant["tenant_key"], "pending_provisioning")
            elif event in razorpay_client.INACTIVE_EVENTS:
                db.set_status(conn, tenant["tenant_key"], "inactive")
                if tenant["provisioned"]:
                    tenant_ops.stop(tenant["tenant_key"], tenant["env_path"])
        return "", 200

    @app.before_request
    def require_admin_login():
        public = request.endpoint in {"pricing", "signup", "razorpay_webhook", "health_api", "admin_login", "static"}
        if public:
            return None
        if not request.path.startswith("/admin"):
            return None
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login", next=request.full_path.rstrip("?")))
        return None

    @app.route("/admin/login", methods=("GET", "POST"))
    def admin_login():
        client = _client_ip()
        if request.method == "POST":
            if _login_blocked(client):
                return render_template("admin_login.html", error="Too many attempts. Try again in 15 minutes."), 429
            csrf_valid = hmac.compare_digest(
                request.form.get("csrf_token", ""), session.get("csrf_token", "")
            )
            username = request.form.get("username", "").strip()
            password_valid = (
                bool(config.ADMIN_USERNAME)
                and username == config.ADMIN_USERNAME
                and check_password_hash(config.ADMIN_PASSWORD_HASH, request.form.get("password", ""))
            )
            if csrf_valid and password_valid:
                session.clear()
                session["admin_authenticated"] = True
                session.permanent = True
                _clear_login_failures(client)
                return redirect(url_for("admin_tenants"))
            _record_login_failure(client)
            return render_template("admin_login.html", error="Invalid username or password."), 401
        return render_template("admin_login.html")

    @app.get("/admin")
    def admin_tenants():
        with db.connect() as conn:
            tenants = db.list_tenants(conn)
        return render_template("admin.html", tenants=tenants)

    @app.post("/admin/<tenant_key>/mark-provisioned")
    def admin_mark_provisioned(tenant_key: str):
        csrf_valid = hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        )
        if csrf_valid:
            with db.connect() as conn:
                if db.get_tenant(conn, tenant_key) is not None:
                    db.set_provisioned(conn, tenant_key)
        return redirect(url_for("admin_tenants"))

    return app


def create_serving_app() -> Flask:
    config.validate_runtime_security()
    return create_app()
