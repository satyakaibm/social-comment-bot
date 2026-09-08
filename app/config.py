import base64
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "comments.db"

YOUTUBE_OAUTH_CLIENT_ID = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", "")
YOUTUBE_OAUTH_CLIENT_SECRET = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
YOUTUBE_VIDEO_IDS = [
    v.strip() for v in os.environ.get("YOUTUBE_VIDEO_IDS", "").split(",") if v.strip()
]
YOUTUBE_VIDEO_LIMIT = int(os.environ.get("YOUTUBE_VIDEO_LIMIT", "10"))
YOUTUBE_COMMENT_LIMIT = int(os.environ.get("YOUTUBE_COMMENT_LIMIT", "100"))
YOUTUBE_DAILY_QUOTA_LIMIT = int(os.environ.get("YOUTUBE_DAILY_QUOTA_LIMIT", "10000"))
# Cap how many replies this platform may post in one calendar day (IST),
# independent of the *_PUBLISH_LIMIT per-cycle cap. 0 means no daily cap.
YOUTUBE_DAILY_REPLY_LIMIT = int(os.environ.get("YOUTUBE_DAILY_REPLY_LIMIT", "0"))
PUBLISH_ERROR_LIMIT = int(os.environ.get("PUBLISH_ERROR_LIMIT", "3"))
COMMENT_MAX_AGE_DAYS = int(os.environ.get("COMMENT_MAX_AGE_DAYS", "90"))

META_GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
INSTAGRAM_USER_ID = os.environ.get("INSTAGRAM_USER_ID", "")
# Meta reads are safe to retry. Keep them short so a slow duplicate check does
# not hold up an entire publishing cycle; writes keep their longer timeout
# because a timed-out write has an uncertain result and must be reconciled.
META_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("META_CONNECT_TIMEOUT_SECONDS", "5"))
META_READ_TIMEOUT_SECONDS = float(os.environ.get("META_READ_TIMEOUT_SECONDS", "15"))
META_WRITE_TIMEOUT_SECONDS = float(os.environ.get("META_WRITE_TIMEOUT_SECONDS", "30"))
META_GET_RETRIES = int(os.environ.get("META_GET_RETRIES", "2"))
# Meta Graph error code 1 is a temporary service-side rejection that often
# succeeds on a short retry. Keep this small so it cannot slow an entire run.
META_POST_RETRIES = int(os.environ.get("META_POST_RETRIES", "2"))
META_POST_RETRY_DELAY_SECONDS = float(
    os.environ.get("META_POST_RETRY_DELAY_SECONDS", "2")
)
# Some comments never accept a Page reply (e.g. Meta has hidden the comment,
# the commenter blocked the Page, or the comment was deleted) and fail with
# the same error on every attempt. Stop auto-retrying a comment once it has
# failed this many times so it cannot keep tripping PUBLISH_ERROR_LIMIT and
# crowding out comments that can still succeed.
META_MAX_POST_ATTEMPTS = int(os.environ.get("META_MAX_POST_ATTEMPTS", "5"))
# A comment checked during this same poll or webhook delivery does not need a
# second identical remote check immediately before its first post. Failed and
# interrupted attempts never use this shortcut.
META_RECENT_REPLY_CHECK_SECONDS = int(
    os.environ.get("META_RECENT_REPLY_CHECK_SECONDS", "300")
)
# Facebook reply-list checks can take minutes on very active threads. Disable
# them by default so Facebook replies are posted promptly. Set this to true if
# avoiding a possible duplicate after a write timeout matters more than speed.
FACEBOOK_VERIFY_EXISTING_REPLIES = os.environ.get(
    "FACEBOOK_VERIFY_EXISTING_REPLIES", "false"
).lower() in ("1", "true", "yes", "on")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
META_WEBHOOK_VERIFY_TOKEN = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")
META_WEBHOOK_AUTO_POST = os.environ.get("META_WEBHOOK_AUTO_POST", "true").lower() in (
    "1", "true", "yes", "on"
)
META_WEBHOOK_ENABLED = os.environ.get("META_WEBHOOK_ENABLED", "false").lower() in (
    "1", "true", "yes", "on"
)
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "9001"))
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "localhost-dashboard")
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "")
_dashboard_password_hash_b64 = os.environ.get("DASHBOARD_PASSWORD_HASH_B64", "")
DASHBOARD_PASSWORD_HASH = (
    base64.urlsafe_b64decode(_dashboard_password_hash_b64.encode()).decode()
    if _dashboard_password_hash_b64
    else os.environ.get("DASHBOARD_PASSWORD_HASH", "")
)
DASHBOARD_SESSION_HOURS = int(os.environ.get("DASHBOARD_SESSION_HOURS", "12"))
DASHBOARD_COOKIE_SECURE = os.environ.get(
    "DASHBOARD_COOKIE_SECURE", "false"
).lower() in ("1", "true", "yes", "on")

# Optional: comma-separated Facebook post IDs / Instagram media IDs to
# restrict polling to. Leave blank to poll all posts/media on the account.
FACEBOOK_POST_IDS = [
    v.strip() for v in os.environ.get("FACEBOOK_POST_IDS", "").split(",") if v.strip()
]
FACEBOOK_POST_LIMIT = int(os.environ.get("FACEBOOK_POST_LIMIT", "10"))
FACEBOOK_COMMENT_LIMIT = int(os.environ.get("FACEBOOK_COMMENT_LIMIT", "100"))
FACEBOOK_PUBLISH_LIMIT = int(os.environ.get("FACEBOOK_PUBLISH_LIMIT", "25"))
# Cap how many replies Facebook may post in one calendar day (IST),
# independent of FACEBOOK_PUBLISH_LIMIT's per-cycle cap. 0 means no daily cap.
FACEBOOK_DAILY_REPLY_LIMIT = int(os.environ.get("FACEBOOK_DAILY_REPLY_LIMIT", "0"))
INSTAGRAM_MEDIA_IDS = [
    v.strip() for v in os.environ.get("INSTAGRAM_MEDIA_IDS", "").split(",") if v.strip()
]

# Cap how many of the most recent Instagram media items get polled per run
# (ignored if INSTAGRAM_MEDIA_IDS is set). Accounts can have hundreds of old
# posts; without a cap the first run would draft-via-Gemini every unseen
# comment across all of them in one go.
INSTAGRAM_MEDIA_LIMIT = int(os.environ.get("INSTAGRAM_MEDIA_LIMIT", "10"))
INSTAGRAM_COMMENT_LIMIT = int(os.environ.get("INSTAGRAM_COMMENT_LIMIT", "100"))
INSTAGRAM_PUBLISH_LIMIT = int(os.environ.get("INSTAGRAM_PUBLISH_LIMIT", "25"))
# Cap how many replies Instagram may post in one calendar day (IST),
# independent of INSTAGRAM_PUBLISH_LIMIT's per-cycle cap. 0 means no daily cap.
INSTAGRAM_DAILY_REPLY_LIMIT = int(os.environ.get("INSTAGRAM_DAILY_REPLY_LIMIT", "0"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
# Every new comment across every platform costs one Gemini call to draft a
# reply. Cap how many drafts (all platforms combined, since they share one
# billed API key) may be generated in one IST calendar day. Comments beyond
# the cap are left unseen and get drafted on a later cycle/day. 0 = no cap.
GEMINI_DAILY_DRAFT_LIMIT = int(os.environ.get("GEMINI_DAILY_DRAFT_LIMIT", "0"))
REPLY_PERSONA = os.environ.get(
    "REPLY_PERSONA",
    "You are a friendly, concise community manager. Keep replies under 3 sentences.",
)

# Operator-written comment/reply examples (one file per example).
_examples_dir = os.environ.get("REPLY_EXAMPLES_DIR", "").strip()
REPLY_EXAMPLES_DIR = (
    Path(_examples_dir) if _examples_dir else BASE_DIR / "reply_examples"
)


def require(*names: str) -> None:
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise RuntimeError(
            f"Missing required config: {', '.join(missing)}. Set them in .env "
            "(see .env.example)."
        )
