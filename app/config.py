import base64
import os
from dataclasses import dataclass

from app.scripts import SCRIPT_NAMES
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
# Profile photos uploaded on /profile. Lives under DATA_DIR so each tenant's
# bind mount keeps its own, and so it survives image rebuilds like the DB.
AVATAR_DIR = DATA_DIR / "avatars"
AVATAR_MAX_BYTES = 1024 * 1024

# Outbound email for profile email verification. All optional: with
# SMTP_HOST or MAIL_FROM unset the portal cannot send, and /profile says so.
# The settings use the Flask-Mail names (MAIL_SERVER, MAIL_PORT,
# MAIL_USERNAME, MAIL_PASSWORD, MAIL_USE_TLS, MAIL_USE_SSL) so every project
# of ours reads the same way as Content My Trip's .env. The SMTP_* names
# that PR #130 shipped with are still accepted as aliases; MAIL_* wins when
# both are present.


def _mail_setting(primary: str, alias: str, default: str = "") -> str:
    value = os.environ.get(primary, "").strip()
    return value or os.environ.get(alias, "").strip() or default


def _mail_flag(primary: str, alias: str, default: bool) -> bool:
    raw = _mail_setting(primary, alias).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


SMTP_HOST = _mail_setting("MAIL_SERVER", "SMTP_HOST")
SMTP_PORT = int(_mail_setting("MAIL_PORT", "SMTP_PORT", "587"))
SMTP_USERNAME = _mail_setting("MAIL_USERNAME", "SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("MAIL_PASSWORD") or os.environ.get("SMTP_PASSWORD", "")
SMTP_USE_TLS = _mail_flag("MAIL_USE_TLS", "SMTP_USE_TLS", True)
SMTP_USE_SSL = _mail_flag("MAIL_USE_SSL", "SMTP_USE_SSL", False)
SMTP_TIMEOUT_SECONDS = int(os.environ.get("SMTP_TIMEOUT_SECONDS", "20"))
# MAIL_FROM may be a bare address, a full "Name <address>", or just a display
# name. A display name on its own (no "@") is paired with the login mailbox,
# which is the address most providers insist the mail comes from anyway --
# so MAIL_FROM="Content My Trip" with MAIL_USERNAME=x@gmail.com sends as
# "Content My Trip <x@gmail.com>".
_mail_from_raw = os.environ.get("MAIL_FROM", "").strip()
if _mail_from_raw and "@" not in _mail_from_raw and "@" in SMTP_USERNAME:
    MAIL_FROM = f"{_mail_from_raw} <{SMTP_USERNAME}>"
elif not _mail_from_raw and "@" in SMTP_USERNAME:
    MAIL_FROM = SMTP_USERNAME
else:
    MAIL_FROM = _mail_from_raw
# Verification codes: 6 digits, short-lived, few guesses, limited resends.
EMAIL_CODE_TTL_MINUTES = int(os.environ.get("EMAIL_CODE_TTL_MINUTES", "15"))
EMAIL_CODE_MAX_ATTEMPTS = 5
EMAIL_CODE_RESEND_SECONDS = 60
DB_PATH = DATA_DIR / "comments.db"

YOUTUBE_OAUTH_CLIENT_ID = os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", "")
YOUTUBE_OAUTH_CLIENT_SECRET = os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
YOUTUBE_POLL_INTERVAL_SECONDS = int(
    os.environ.get("YOUTUBE_POLL_INTERVAL_SECONDS", "3600")
)
YOUTUBE_PUBLISH_LIMIT = int(os.environ.get("YOUTUBE_PUBLISH_LIMIT", "50"))
POLLING_LOG_FILE = os.environ.get("POLLING_LOG_FILE", "data/polling.log")
YOUTUBE_VIDEO_IDS = [
    v.strip() for v in os.environ.get("YOUTUBE_VIDEO_IDS", "").split(",") if v.strip()
]
YOUTUBE_VIDEO_LIMIT = int(os.environ.get("YOUTUBE_VIDEO_LIMIT", "10"))
# A video that keeps getting comments long after newer videos have been
# uploaded falls out of the "latest N uploads" scan window with no way
# back short of adding it to YOUTUBE_VIDEO_IDS by hand. Any video with a
# comment we've recorded within this many days is kept in scope
# automatically -- see db.recently_active_video_ids().
YOUTUBE_ACTIVE_VIDEO_DAYS = int(os.environ.get("YOUTUBE_ACTIVE_VIDEO_DAYS", "30"))
YOUTUBE_COMMENT_LIMIT = int(os.environ.get("YOUTUBE_COMMENT_LIMIT", "100"))
YOUTUBE_DAILY_QUOTA_LIMIT = int(os.environ.get("YOUTUBE_DAILY_QUOTA_LIMIT", "10000"))
# Cap how many replies this platform may post in one calendar day (IST),
# independent of the *_PUBLISH_LIMIT per-cycle cap. 0 means no daily cap.
YOUTUBE_DAILY_REPLY_LIMIT = int(os.environ.get("YOUTUBE_DAILY_REPLY_LIMIT", "0"))
PUBLISH_ERROR_LIMIT = int(os.environ.get("PUBLISH_ERROR_LIMIT", "3"))
COMMENT_MAX_AGE_DAYS = int(os.environ.get("COMMENT_MAX_AGE_DAYS", "90"))

# Keep platform statistics on a conservative cadence to preserve API quota
# for comment polling and replies, which are the application's primary work.
YOUTUBE_VIDEO_STATS_REFRESH_MINUTES = int(
    os.environ.get("YOUTUBE_VIDEO_STATS_REFRESH_MINUTES", "30")
)
META_VIDEO_STATS_REFRESH_MINUTES = int(
    os.environ.get("META_VIDEO_STATS_REFRESH_MINUTES", "30")
)
# How many of the most recently active videos/posts (per platform) are
# refreshed each cycle. The bound prevents an old channel history from
# consuming an entire cycle or a day's API allowance.
VIDEO_STATS_CONTAINER_LIMIT = int(os.environ.get("VIDEO_STATS_CONTAINER_LIMIT", "50"))
# YouTube Developer Policy III.E.4 forbids displaying or storing statistics
# retrieved as Authorized or Non-Authorized Data for more than 30 days. The
# ceiling is applied to the env override as well, so a stray deployment
# variable cannot quietly put us back in violation.
YOUTUBE_STATS_MAX_RETENTION_DAYS = 30
VIDEO_STATS_HISTORY_RETENTION_DAYS = min(
    YOUTUBE_STATS_MAX_RETENTION_DAYS,
    int(os.environ.get("VIDEO_STATS_HISTORY_RETENTION_DAYS", "30")),
)
# Instagram's hourly follower-online counts (Graph `online_followers`, the
# data behind the app's "Most active times") move slowly and Meta publishes
# them about two days behind, so a handful of fetches a day is plenty. The
# fetch asks for the trailing week each time, which also backfills any day a
# worker outage missed. Meta data, so the YouTube 30-day cap does not apply;
# 90 days gives every weekday a dozen samples.
INSTAGRAM_ONLINE_FOLLOWERS_REFRESH_HOURS = int(
    os.environ.get("INSTAGRAM_ONLINE_FOLLOWERS_REFRESH_HOURS", "6")
)
AUDIENCE_ONLINE_RETENTION_DAYS = int(os.environ.get("AUDIENCE_ONLINE_RETENTION_DAYS", "90"))

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
# How long the dashboard reuses the results of its heavy Insights/Momentum
# analytics queries (seconds). The data behind them changes only when the
# stats pollers write a snapshot, every 30 minutes by default, so a 10-minute
# reuse window is invisible to readers. 0 disables the cache.
ANALYTICS_CACHE_SECONDS = int(os.environ.get("ANALYTICS_CACHE_SECONDS", "600"))
# Local HTTP only. Production (HTTPS / Cloudflare Tunnel) must leave this unset
# so the process refuses a default session secret, insecure cookies, or an
# unencrypted comments database.
DASHBOARD_INSECURE_LOCAL = os.environ.get(
    "DASHBOARD_INSECURE_LOCAL", "false"
).lower() in ("1", "true", "yes", "on")
_cookie_secure = os.environ.get("DASHBOARD_COOKIE_SECURE")
if _cookie_secure is None or not _cookie_secure.strip():
    DASHBOARD_COOKIE_SECURE = not DASHBOARD_INSECURE_LOCAL
else:
    DASHBOARD_COOKIE_SECURE = _cookie_secure.lower() in ("1", "true", "yes", "on")
# Passphrase for SQLCipher. Empty keeps the legacy plaintext SQLite file
# (tests and DASHBOARD_INSECURE_LOCAL). Serving without INSECURE_LOCAL requires
# at least 32 characters; the first open encrypts an existing plaintext DB.
DB_ENCRYPTION_KEY = os.environ.get("DB_ENCRYPTION_KEY", "").strip()

_WEAK_DASHBOARD_SECRETS = frozenset(
    {"", "localhost-dashboard", "change-this-local-secret"}
)
_MIN_SECRET_LENGTH = 32

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


# How a page decides which language to reply in. Consumed by generate.py (the
# mandatory prompt rules) and sanitize.py (what it does with mixed scripts).
#
#   match_commenter  -- mirror the commenter's own language and script, for any
#                       language Gemini supports. The default: a viewer who
#                       writes in Kannada gets a Kannada reply.
#   odia_or_english  -- the original Hindolroad rule: every reply is wholly in
#                       Odia or wholly in English, and any other Indic script
#                       is transliterated to Odia or dropped. Kept as an opt-in
#                       because it is a far stronger guarantee against Gemini
#                       leaking Devanagari chant words into an Odia reply --
#                       the problem sanitize.py's _SCRIPT_FIXES was written for.
REPLY_LANGUAGE_POLICIES = ("match_commenter", "odia_or_english")
DEFAULT_REPLY_LANGUAGE_POLICY = "match_commenter"


def _reply_auto_post_scripts(suffix: str) -> tuple[str, ...]:
    """Scripts this page is willing to publish without a human reading first.

    Empty (the default) means no gate: every draft posts exactly as it did
    before this setting existed. Naming scripts turns it on, and a draft in
    any other script is held in pending_review for the dashboard instead of
    being auto-posted -- the safety valve for turning on match_commenter on a
    channel whose replies have only ever been proof-read in one language.
    `Latin` covers English and anything romanized; see app/scripts.py.
    """
    raw = os.environ.get(f"REPLY_AUTO_POST_SCRIPTS{suffix}", "").strip()
    if not raw:
        return ()
    names = []
    for value in raw.split(","):
        name = value.strip().title()
        if not name:
            continue
        if name not in SCRIPT_NAMES:
            raise RuntimeError(
                f"REPLY_AUTO_POST_SCRIPTS{suffix} lists unknown script {value.strip()!r}. "
                f"Known: {', '.join(SCRIPT_NAMES)}."
            )
        names.append(name)
    return tuple(names)


# Which mandatory reply-style block generate.py applies on top of the persona.
#   devotional -- the Hindolroad rules: chant/🙏 handling, the Odia and English
#                 thank-you phrasings that must never appear, direct answers.
#   generic    -- tone and no-generic-thanks rules only.
# Hindolroad and Gudiakateni are devotional Odia pages that share one example
# set, so they default to the same profile without an env entry; any other
# page is generic unless REPLY_STYLE_PROFILE<suffix> says otherwise.
REPLY_STYLE_PROFILES = ("devotional", "generic")
_DEVOTIONAL_PAGE_KEYS = ("hindolroad", "gudiakateni")


def _reply_style_profile(suffix: str, key: str) -> str:
    raw = os.environ.get(f"REPLY_STYLE_PROFILE{suffix}", "").strip().lower()
    if not raw:
        return "devotional" if key in _DEVOTIONAL_PAGE_KEYS else "generic"
    if raw not in REPLY_STYLE_PROFILES:
        raise RuntimeError(
            f"REPLY_STYLE_PROFILE{suffix}={raw!r} is not one of "
            f"{', '.join(REPLY_STYLE_PROFILES)}."
        )
    return raw


def _reply_language_policy(suffix: str) -> str:
    raw = os.environ.get(f"REPLY_LANGUAGE_POLICY{suffix}", "").strip().lower()
    if not raw:
        return DEFAULT_REPLY_LANGUAGE_POLICY
    if raw not in REPLY_LANGUAGE_POLICIES:
        raise RuntimeError(
            f"REPLY_LANGUAGE_POLICY{suffix}={raw!r} is not one of "
            f"{', '.join(REPLY_LANGUAGE_POLICIES)}."
        )
    return raw


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
    # Defaulted so every existing PageConfig(...) call site (and the test
    # builders) keeps working without naming them.
    reply_language_policy: str = DEFAULT_REPLY_LANGUAGE_POLICY
    # Language to fall back on when a comment gives nothing to mirror -- only
    # emoji, only punctuation, or a script the model can't place. Empty means
    # "say nothing about it in the prompt" and leaves the persona in charge.
    reply_fallback_language: str = ""
    # Empty = post every language unreviewed, as before. See
    # _reply_auto_post_scripts.
    reply_auto_post_scripts: tuple[str, ...] = ()
    reply_style_profile: str = "generic"


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
            f"PAGE_KEY{suffix} is required once FACEBOOK_PAGE_ID{suffix}, "
            f"INSTAGRAM_USER_ID{suffix}, or YOUTUBE_REFRESH_TOKEN{suffix} is set."
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
        reply_language_policy=_reply_language_policy(suffix),
        reply_fallback_language=os.environ.get(
            f"REPLY_FALLBACK_LANGUAGE{suffix}", ""
        ).strip(),
        reply_auto_post_scripts=_reply_auto_post_scripts(suffix),
        reply_style_profile=_reply_style_profile(suffix, key),
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
        reply_language_policy=_reply_language_policy(""),
        reply_fallback_language=os.environ.get("REPLY_FALLBACK_LANGUAGE", "").strip(),
        reply_auto_post_scripts=_reply_auto_post_scripts(""),
        reply_style_profile=_reply_style_profile("", "hindolroad"),
    )

DEFAULT_PAGE_KEY = next(iter(PAGES))
REPLY_PERSONA = PAGES[DEFAULT_PAGE_KEY].persona
PAGES_BY_FACEBOOK_ID = {
    p.facebook_page_id: p.key for p in PAGES.values() if p.facebook_page_id
}
PAGES_BY_INSTAGRAM_ID = {
    p.instagram_user_id: p.key for p in PAGES.values() if p.instagram_user_id
}


def reply_language_policy(page_key: str) -> str:
    """The reply-language policy for page_key, for callers that only have a key.

    Tolerates an unknown or empty key (legacy comment rows store page_key='')
    the same way _daily_limit_for does, rather than raising on data that
    predates multi-page support.
    """
    page = PAGES.get(page_key or DEFAULT_PAGE_KEY)
    return page.reply_language_policy if page else DEFAULT_REPLY_LANGUAGE_POLICY


def reply_fallback_language(page_key: str) -> str:
    """page_key's fallback reply language, or "" when it has none."""
    page = PAGES.get(page_key or DEFAULT_PAGE_KEY)
    return page.reply_fallback_language if page else ""


def reply_style_profile(page_key: str) -> str:
    """page_key's mandatory style profile; unknown keys are generic."""
    page = PAGES.get(page_key or DEFAULT_PAGE_KEY)
    return page.reply_style_profile if page else "generic"


def reply_auto_post_scripts(page_key: str) -> tuple[str, ...]:
    """Scripts page_key may auto-post; empty means "no gate"."""
    page = PAGES.get(page_key or DEFAULT_PAGE_KEY)
    return page.reply_auto_post_scripts if page else ()


def facebook_page_keys() -> list[str]:
    """Keys of pages with a Facebook Page configured, in PAGES order."""
    return [p.key for p in PAGES.values() if p.facebook_page_id]


def instagram_page_keys() -> list[str]:
    """Keys of pages with an Instagram account configured, in PAGES order."""
    return [p.key for p in PAGES.values() if p.instagram_user_id]


def youtube_page_keys() -> list[str]:
    """Keys of pages with a YouTube channel configured, in PAGES order."""
    return [p.key for p in PAGES.values() if p.youtube_refresh_token]


def all_page_keys() -> list[str]:
    """Keys of every configured page, in PAGES order -- used by the dashboard's
    channel switcher when no single platform is selected.
    """
    return list(PAGES.keys())


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


def dashboard_secret_is_weak(secret: str | None = None) -> bool:
    value = DASHBOARD_SECRET if secret is None else secret
    return value in _WEAK_DASHBOARD_SECRETS or len(value) < _MIN_SECRET_LENGTH


def validate_runtime_security() -> None:
    """Refuse to serve on the public HTTPS host with local-dev defaults.

    Tests and `http://127.0.0.1` can set DASHBOARD_INSECURE_LOCAL=true.
    bot.hindolroad.download must not.
    """
    if DASHBOARD_INSECURE_LOCAL:
        return
    errors = []
    if dashboard_secret_is_weak():
        errors.append(
            "Set DASHBOARD_SECRET to a random value of at least 32 characters "
            "(not localhost-dashboard or change-this-local-secret)."
        )
    if not DASHBOARD_COOKIE_SECURE:
        errors.append(
            "Set DASHBOARD_COOKIE_SECURE=true (required for HTTPS, including "
            "https://bot.hindolroad.download/)."
        )
    if len(DB_ENCRYPTION_KEY) < _MIN_SECRET_LENGTH:
        errors.append(
            "Set DB_ENCRYPTION_KEY to a random value of at least 32 characters "
            "so data/comments.db is encrypted at rest."
        )
    if errors:
        raise RuntimeError(
            "Refusing to start with insecure dashboard defaults:\n- "
            + "\n- ".join(errors)
        )
