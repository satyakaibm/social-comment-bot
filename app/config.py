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

META_GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v21.0")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "")
FACEBOOK_PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "")
INSTAGRAM_USER_ID = os.environ.get("INSTAGRAM_USER_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
META_WEBHOOK_VERIFY_TOKEN = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")
META_WEBHOOK_AUTO_POST = os.environ.get("META_WEBHOOK_AUTO_POST", "true").lower() in (
    "1", "true", "yes", "on"
)
META_WEBHOOK_ENABLED = os.environ.get("META_WEBHOOK_ENABLED", "false").lower() in (
    "1", "true", "yes", "on"
)
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8081"))
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "9001"))
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "localhost-dashboard")

# Optional: comma-separated Facebook post IDs / Instagram media IDs to
# restrict polling to. Leave blank to poll all posts/media on the account.
FACEBOOK_POST_IDS = [
    v.strip() for v in os.environ.get("FACEBOOK_POST_IDS", "").split(",") if v.strip()
]
INSTAGRAM_MEDIA_IDS = [
    v.strip() for v in os.environ.get("INSTAGRAM_MEDIA_IDS", "").split(",") if v.strip()
]

# Cap how many of the most recent Instagram media items get polled per run
# (ignored if INSTAGRAM_MEDIA_IDS is set). Accounts can have hundreds of old
# posts; without a cap the first run would draft-via-Gemini every unseen
# comment across all of them in one go.
INSTAGRAM_MEDIA_LIMIT = int(os.environ.get("INSTAGRAM_MEDIA_LIMIT", "10"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
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
