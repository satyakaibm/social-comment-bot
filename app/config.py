import base64
import os
from dataclasses import dataclass
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
# A separate long-lived User token (with instagram_manage_engagement) for
# liking Instagram comments -- that action rejects a Page token outright.
# Falls back to FACEBOOK_PAGE_ACCESS_TOKEN if unset, matching the old
# behavior where a single user token was resolved into a page token as
# needed (see meta_client.get_user_access_token).
META_USER_ACCESS_TOKEN = os.environ.get(
    "META_USER_ACCESS_TOKEN", FACEBOOK_PAGE_ACCESS_TOKEN
)
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

# Operator-written comment/reply examples (one file per example).
_examples_dir = os.environ.get("REPLY_EXAMPLES_DIR", "").strip()
REPLY_EXAMPLES_DIR = (
    Path(_examples_dir) if _examples_dir else BASE_DIR / "reply_examples"
)


def _load_reply_persona(*, suffix: str = "", persona_dir: Path | None = None) -> str:
    """Prefer REPLY_PERSONA_FILE<suffix>'s content; REPLY_PERSONA<suffix>/default are fallbacks.

    A long persona reads and edits far more easily as its own text file than
    as a single giant line embedded in .env. Lives under persona_dir (named
    with a leading underscore so the example loader skips it, same as
    _template.txt) so both ship via the same read-only Docker mount.
    """
    persona_dir = persona_dir if persona_dir is not None else REPLY_EXAMPLES_DIR
    persona_file = os.environ.get(f"REPLY_PERSONA_FILE{suffix}", "").strip()
    path = Path(persona_file) if persona_file else persona_dir / "_reply_persona.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return os.environ.get(
        f"REPLY_PERSONA{suffix}",
        "You are a friendly, concise community manager. Keep replies under 3 sentences.",
    )


@dataclass(frozen=True)
class PageConfig:
    """One Facebook Page + its linked Instagram account: its own credentials,
    reply persona, and daily reply limits. Supports running more than one
    Facebook/Instagram identity from a single deployment -- see PAGES below.
    """

    key: str
    label: str
    facebook_page_id: str
    facebook_page_access_token: str
    meta_user_access_token: str
    instagram_user_id: str
    facebook_post_ids: list[str]
    instagram_media_ids: list[str]
    facebook_daily_reply_limit: int
    instagram_daily_reply_limit: int
    youtube_oauth_client_id: str
    youtube_oauth_client_secret: str
    youtube_refresh_token: str
    youtube_video_ids: list[str]
    youtube_daily_reply_limit: int
    persona: str
    persona_dir: Path


def _build_page_config(suffix: str) -> "PageConfig | None":
    """Build one page's config from <VAR><suffix> env vars, or None if unconfigured.

    suffix="" reads today's exact unsuffixed vars (FACEBOOK_PAGE_ID,
    INSTAGRAM_USER_ID, ...) so the first/default page needs zero .env
    changes. Additional pages (suffix="_2", "_3", ...) are fully additive.
    """
    facebook_page_id = os.environ.get(f"FACEBOOK_PAGE_ID{suffix}", "").strip()
    instagram_user_id = os.environ.get(f"INSTAGRAM_USER_ID{suffix}", "").strip()
    youtube_refresh_token = os.environ.get(f"YOUTUBE_REFRESH_TOKEN{suffix}", "").strip()
    if not facebook_page_id and not instagram_user_id and not youtube_refresh_token:
        return None

    key = os.environ.get(f"PAGE_KEY{suffix}", "").strip() or ("hindolroad" if suffix == "" else "")
    if not key:
        raise RuntimeError(
            f"PAGE_KEY{suffix} is required once FACEBOOK_PAGE_ID{suffix} or "
            f"INSTAGRAM_USER_ID{suffix} is set."
        )
    label = os.environ.get(f"PAGE_LABEL{suffix}", "").strip() or key.replace("_", " ").title()

    facebook_page_access_token = os.environ.get(f"FACEBOOK_PAGE_ACCESS_TOKEN{suffix}", "")
    meta_user_access_token = os.environ.get(
        f"META_USER_ACCESS_TOKEN{suffix}", facebook_page_access_token
    )
    persona_dir = REPLY_EXAMPLES_DIR if suffix == "" else REPLY_EXAMPLES_DIR / key

    return PageConfig(
        key=key,
        label=label,
        facebook_page_id=facebook_page_id,
        facebook_page_access_token=facebook_page_access_token,
        meta_user_access_token=meta_user_access_token,
        instagram_user_id=instagram_user_id,
        facebook_post_ids=[
            v.strip()
            for v in os.environ.get(f"FACEBOOK_POST_IDS{suffix}", "").split(",")
            if v.strip()
        ],
        instagram_media_ids=[
            v.strip()
            for v in os.environ.get(f"INSTAGRAM_MEDIA_IDS{suffix}", "").split(",")
            if v.strip()
        ],
        facebook_daily_reply_limit=int(
            os.environ.get(f"FACEBOOK_DAILY_REPLY_LIMIT{suffix}", "0")
        ),
        instagram_daily_reply_limit=int(
            os.environ.get(f"INSTAGRAM_DAILY_REPLY_LIMIT{suffix}", "0")
        ),
        youtube_oauth_client_id=os.environ.get(f"YOUTUBE_OAUTH_CLIENT_ID{suffix}", ""),
        youtube_oauth_client_secret=os.environ.get(
            f"YOUTUBE_OAUTH_CLIENT_SECRET{suffix}", ""
        ),
        youtube_refresh_token=youtube_refresh_token,
        youtube_video_ids=[
            v.strip()
            for v in os.environ.get(f"YOUTUBE_VIDEO_IDS{suffix}", "").split(",")
            if v.strip()
        ],
        youtube_daily_reply_limit=int(
            os.environ.get(f"YOUTUBE_DAILY_REPLY_LIMIT{suffix}", "0")
        ),
        persona=_load_reply_persona(suffix=suffix, persona_dir=persona_dir),
        persona_dir=persona_dir,
    )


# Numbered suffixes give headroom for future pages without a dynamic
# discovery mechanism -- raising the ceiling later is a one-line change.
_PAGE_SUFFIXES = ("", "_2", "_3", "_4", "_5")
PAGES: dict[str, PageConfig] = {}
for _suffix in _PAGE_SUFFIXES:
    _page = _build_page_config(_suffix)
    if _page is not None:
        if _page.key in PAGES:
            raise RuntimeError(f"Duplicate PAGE_KEY '{_page.key}' across configured pages.")
        PAGES[_page.key] = _page

if not PAGES:
    # No Facebook/Instagram page configured at all (e.g. a fresh checkout, or
    # the test suite's clean environment) -- keep one empty placeholder so
    # DEFAULT_PAGE_KEY/PAGES[...] lookups elsewhere never need a "no pages"
    # special case.
    PAGES["hindolroad"] = PageConfig(
        key="hindolroad",
        label="Hindolroad",
        facebook_page_id="",
        facebook_page_access_token="",
        meta_user_access_token="",
        instagram_user_id="",
        facebook_post_ids=[],
        instagram_media_ids=[],
        facebook_daily_reply_limit=0,
        instagram_daily_reply_limit=0,
        youtube_oauth_client_id="",
        youtube_oauth_client_secret="",
        youtube_refresh_token="",
        youtube_video_ids=[],
        youtube_daily_reply_limit=0,
        persona=_load_reply_persona(),
        persona_dir=REPLY_EXAMPLES_DIR,
    )

DEFAULT_PAGE_KEY = next(iter(PAGES))
REPLY_PERSONA = PAGES[DEFAULT_PAGE_KEY].persona
PAGES_BY_FACEBOOK_ID = {
    p.facebook_page_id: p.key for p in PAGES.values() if p.facebook_page_id
}
PAGES_BY_INSTAGRAM_ID = {
    p.instagram_user_id: p.key for p in PAGES.values() if p.instagram_user_id
}


def facebook_page_keys() -> list[str]:
    """Keys of pages with a Facebook Page configured, in PAGES order."""
    return [p.key for p in PAGES.values() if p.facebook_page_id]


def instagram_page_keys() -> list[str]:
    """Keys of pages with an Instagram account configured, in PAGES order."""
    return [p.key for p in PAGES.values() if p.instagram_user_id]


def youtube_page_keys() -> list[str]:
    """Keys of pages with a YouTube channel configured, in PAGES order."""
    return [p.key for p in PAGES.values() if p.youtube_refresh_token]


def resolve_page_key(platform: str, entry_id: str) -> str | None:
    """Map a webhook payload's entry.id to the page it belongs to.

    Falls back to the only configured page when entry_id is missing/unmatched
    AND there is exactly one page -- preserves single-page webhook behavior
    (and every existing webhook test fixture, none of which include
    entry.id) unchanged. Once a second page exists, an unmatched entry_id
    returns None (a hard skip) rather than guessing, since misattributing an
    event means the wrong persona/token/quota bucket, not just a missing one.
    """
    by_id = PAGES_BY_FACEBOOK_ID if platform == "facebook" else PAGES_BY_INSTAGRAM_ID
    key = by_id.get(entry_id)
    if key is not None:
        return key
    if len(PAGES) == 1:
        return DEFAULT_PAGE_KEY
    return None


def require(*names: str) -> None:
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise RuntimeError(
            f"Missing required config: {', '.join(missing)}. Set them in .env "
            "(see .env.example)."
        )
