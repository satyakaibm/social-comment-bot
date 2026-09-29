import base64
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "billing" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "billing.db"

# Where the shared bot checkout lives on the VM -- tenant_ops shells out to
# `docker compose` in this directory to start/stop a tenant's stack.
REPO_ROOT = Path(os.environ.get("BOT_REPO_ROOT", str(BASE_DIR))).resolve()
TENANTS_DIR = REPO_ROOT / "tenants"

# Every tenant runs as extra services inside this one Compose project (same
# network as the main instance) instead of its own isolated project, so they
# share one docker network and one built image per service rather than each
# tenant rebuilding an identical image under its own project name. A tenant's
# containers are still stopped/started independently by naming only that
# tenant's services (`<tenant_key>-dashboard`, etc.) in the compose command.
PROJECT_NAME = "social-comment-bot"

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_PLAN_ID = os.environ.get("RAZORPAY_PLAN_ID", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
# Number of monthly billing cycles to authorize up front. Razorpay requires a
# bound; ~10 years is effectively "until cancelled" for a monthly plan.
RAZORPAY_SUBSCRIPTION_TOTAL_COUNT = int(
    os.environ.get("RAZORPAY_SUBSCRIPTION_TOTAL_COUNT", "120")
)

BILLING_HOST = os.environ.get("BILLING_HOST", "127.0.0.1")
BILLING_PORT = int(os.environ.get("BILLING_PORT", "9000"))
BILLING_SECRET = os.environ.get("BILLING_SECRET", "localhost-billing-secret")

ADMIN_USERNAME = os.environ.get("BILLING_ADMIN_USERNAME", "")
_admin_password_hash_b64 = os.environ.get("BILLING_ADMIN_PASSWORD_HASH_B64", "")
ADMIN_PASSWORD_HASH = (
    base64.urlsafe_b64decode(_admin_password_hash_b64.encode()).decode()
    if _admin_password_hash_b64
    else os.environ.get("BILLING_ADMIN_PASSWORD_HASH", "")
)

BILLING_INSECURE_LOCAL = os.environ.get(
    "BILLING_INSECURE_LOCAL", "false"
).lower() in ("1", "true", "yes", "on")
_cookie_secure = os.environ.get("BILLING_COOKIE_SECURE")
if _cookie_secure is None or not _cookie_secure.strip():
    BILLING_COOKIE_SECURE = not BILLING_INSECURE_LOCAL
else:
    BILLING_COOKIE_SECURE = _cookie_secure.lower() in ("1", "true", "yes", "on")

_WEAK_SECRETS = frozenset({"", "localhost-billing-secret"})
_MIN_SECRET_LENGTH = 32

# First free port to hand out when provisioning a new tenant. Existing
# tenants' ports (read from the DB) are skipped automatically.
TENANT_PORT_RANGE_START = int(os.environ.get("TENANT_PORT_RANGE_START", "9101"))


def secret_is_weak(secret: str | None = None) -> bool:
    value = BILLING_SECRET if secret is None else secret
    return value in _WEAK_SECRETS or len(value) < _MIN_SECRET_LENGTH


def validate_runtime_security() -> None:
    """Refuse to serve with local-dev defaults once this is public-facing.

    Mirrors app.config.validate_runtime_security -- see that function for why
    this check exists (bot.hindolroad.download must never accept a weak
    secret or insecure cookie; the same applies to billing.hindolroad.download).
    """
    if BILLING_INSECURE_LOCAL:
        return
    errors = []
    if secret_is_weak():
        errors.append(
            "Set BILLING_SECRET to a random value of at least 32 characters."
        )
    if not BILLING_COOKIE_SECURE:
        errors.append("Set BILLING_COOKIE_SECURE=true (required for HTTPS).")
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        errors.append(
            "Set BILLING_ADMIN_USERNAME and BILLING_ADMIN_PASSWORD_HASH_B64."
        )
    if not RAZORPAY_WEBHOOK_SECRET:
        errors.append("Set RAZORPAY_WEBHOOK_SECRET.")
    if errors:
        raise RuntimeError(
            "Refusing to start with insecure billing defaults:\n- "
            + "\n- ".join(errors)
        )
