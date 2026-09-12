import json
import time

import requests
from requests import RequestException

from app import config, db

GRAPH_BASE = "https://graph.facebook.com"
READ_TIMEOUT_SECONDS = (
    config.META_CONNECT_TIMEOUT_SECONDS,
    config.META_READ_TIMEOUT_SECONDS,
)
WRITE_TIMEOUT_SECONDS = (
    config.META_CONNECT_TIMEOUT_SECONDS,
    config.META_WRITE_TIMEOUT_SECONDS,
)
GET_RETRIES = config.META_GET_RETRIES

# Keyed by page_key -- a single-slot cache would leak one page's token or
# username into another page's requests the moment more than one page is
# configured (harmless today with exactly one page; a real correctness bug
# -- e.g. posting a reply with the wrong Page's token -- the moment a
# second page's requests share this process).
_ig_username_cache: dict[str, str] = {}
_page_token_cache: dict[str, str] = {}
_user_token_cache: dict[str, str] = {}
# Keep TCP/TLS connections open between Graph calls. A Facebook batch normally
# makes many small requests, so recreating a connection for every request adds
# avoidable latency.
_http_session = requests.Session()


class GraphAPIError(RuntimeError):
    pass


class _DefaultPageProxy:
    """Proxies the default page's fields to the live top-level config
    constants (FACEBOOK_PAGE_ID, etc.), so `patch.object(config,
    "FACEBOOK_PAGE_ID", ...)` in existing tests keeps working exactly as
    before. config.PAGES entries are frozen at import time and would not
    see such a patch.
    """

    @property
    def facebook_page_id(self) -> str:
        return config.FACEBOOK_PAGE_ID

    @property
    def facebook_page_access_token(self) -> str:
        return config.FACEBOOK_PAGE_ACCESS_TOKEN

    @property
    def meta_user_access_token(self) -> str:
        return config.META_USER_ACCESS_TOKEN

    @property
    def instagram_user_id(self) -> str:
        return config.INSTAGRAM_USER_ID

    @property
    def facebook_post_ids(self) -> list[str]:
        return config.FACEBOOK_POST_IDS

    @property
    def instagram_media_ids(self) -> list[str]:
        return config.INSTAGRAM_MEDIA_IDS


_DEFAULT_PAGE_PROXY = _DefaultPageProxy()


def _page(page_key: str):
    """Return page_key's config (live-patchable proxy for the default page)."""
    if page_key == config.DEFAULT_PAGE_KEY:
        return _DEFAULT_PAGE_PROXY
    return config.PAGES[page_key]


_REQUIRE_FIELDS = {
    "FACEBOOK_PAGE_ID": "facebook_page_id",
    "FACEBOOK_PAGE_ACCESS_TOKEN": "facebook_page_access_token",
    "META_USER_ACCESS_TOKEN": "meta_user_access_token",
    "INSTAGRAM_USER_ID": "instagram_user_id",
}


def _require_page(page_key: str, *names: str) -> None:
    """Like config.require(), scoped to one page's resolved config.

    For the default page, delegates to config.require() against the plain
    global names so tests patching those globals directly keep raising
    exactly as before.
    """
    if page_key == config.DEFAULT_PAGE_KEY:
        config.require(*names)
        return
    page = _page(page_key)
    missing = [n for n in names if not getattr(page, _REQUIRE_FIELDS[n], None)]
    if missing:
        raise RuntimeError(
            f"Missing required config for page '{page_key}': {', '.join(missing)}."
        )


def _url(path: str) -> str:
    return f"{GRAPH_BASE}/{config.META_GRAPH_VERSION}/{path}"


def _raise_if_error(path: str, data: dict) -> None:
    if "error" in data:
        raise GraphAPIError(f"{path}: {data['error']}")


def _quota_platform(path: str, page_key: str) -> str | None:
    first = path.split("/", 1)[0]
    if first in {"me", ""}:
        return None
    page = _page(page_key)
    platform = "facebook" if ("_" in first or first == page.facebook_page_id) else "instagram"
    # Composite key only for non-default pages, so Hindolroad's existing
    # quota history rows (bare "facebook"/"instagram") are undisturbed.
    return platform if page_key == config.DEFAULT_PAGE_KEY else f"{platform}:{page_key}"


def _record_meta_usage(path: str, response, page_key: str) -> None:
    platform = _quota_platform(path, page_key)
    if not platform:
        return
    raw = response.headers.get("x-app-usage") or response.headers.get("x-page-usage")
    business_raw = response.headers.get("x-business-use-case-usage")
    if not raw and not business_raw:
        return
    try:
        if raw:
            usage_rows = [json.loads(raw)]
        else:
            by_asset = json.loads(business_raw)
            usage_rows = [row for rows in by_asset.values() for row in rows]
        used = max(
            int(usage.get(name, 0))
            for usage in usage_rows
            for name in ("call_count", "total_time", "total_cputime")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    db.set_quota_usage(platform, "rolling", min(100, used), 100)


def get_page_access_token(page_key: str = config.DEFAULT_PAGE_KEY) -> str:
    """Resolve a genuine Page access token for page_key.

    FACEBOOK_PAGE_ACCESS_TOKEN may be a user access token (with
    pages_show_list) rather than a Page token — Facebook's "New Pages
    Experience" rejects a plain user token on Page edges like /posts. If so,
    exchange it via /me/accounts for the real Page token and cache it.
    """
    if page_key in _page_token_cache:
        return _page_token_cache[page_key]

    _require_page(page_key, "FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")
    page = _page(page_key)
    identity_resp = _http_session.get(
        _url("me"),
        params={"fields": "id", "access_token": page.facebook_page_access_token},
        timeout=READ_TIMEOUT_SECONDS,
    )
    identity = identity_resp.json()
    _raise_if_error("me", identity)
    if identity.get("id") == page.facebook_page_id:
        _page_token_cache[page_key] = page.facebook_page_access_token
        return _page_token_cache[page_key]

    resp = _http_session.get(
        _url("me/accounts"),
        params={"fields": "id,access_token", "access_token": page.facebook_page_access_token},
        timeout=READ_TIMEOUT_SECONDS,
    )
    data = resp.json()
    _raise_if_error("me/accounts", data)

    for candidate in data.get("data", []):
        if candidate.get("id") == page.facebook_page_id:
            _page_token_cache[page_key] = candidate["access_token"]
            return _page_token_cache[page_key]

    # Fall back to the configured token in case it's already a Page token.
    _page_token_cache[page_key] = page.facebook_page_access_token
    return _page_token_cache[page_key]


def get_user_access_token(page_key: str = config.DEFAULT_PAGE_KEY) -> str:
    """Return a User token for IG User edges that reject Page tokens.

    Instagram comment likes require a genuine User token with
    instagram_manage_engagement -- a Page token is rejected outright.
    META_USER_ACCESS_TOKEN holds that dedicated token; it defaults to
    FACEBOOK_PAGE_ACCESS_TOKEN for back-compat with setups where that value
    is still a user token the app resolves into a Page token as needed.
    """
    if page_key in _user_token_cache:
        return _user_token_cache[page_key]

    _require_page(page_key, "META_USER_ACCESS_TOKEN")
    page = _page(page_key)
    token = page.meta_user_access_token
    identity_resp = _http_session.get(
        _url("me"),
        params={"fields": "id", "access_token": token},
        timeout=READ_TIMEOUT_SECONDS,
    )
    identity = identity_resp.json()
    _raise_if_error("me", identity)
    if page.facebook_page_id and identity.get("id") == page.facebook_page_id:
        raise GraphAPIError(
            "Instagram comment likes require a User access token with "
            "instagram_manage_engagement; META_USER_ACCESS_TOKEN is a Page token."
        )
    _user_token_cache[page_key] = token
    return _user_token_cache[page_key]


def graph_get(path: str, *, page_key: str = config.DEFAULT_PAGE_KEY, **params) -> dict:
    params["access_token"] = get_page_access_token(page_key)
    for attempt in range(GET_RETRIES):
        try:
            resp = _http_session.get(
                _url(path), params=params, timeout=READ_TIMEOUT_SECONDS
            )
            _record_meta_usage(path, resp, page_key)
            data = resp.json()
            _raise_if_error(path, data)
            return data
        except RequestException as exc:
            if attempt + 1 == GET_RETRIES:
                raise GraphAPIError(
                    f"{path}: Meta read failed after {GET_RETRIES} attempts: {exc}"
                ) from exc
            print(
                f"Meta read for {path} timed out; retrying once before "
                "leaving the comment for the next cycle.",
                flush=True,
            )
            time.sleep(attempt + 1)


def graph_post(
    path: str,
    *,
    page_key: str = config.DEFAULT_PAGE_KEY,
    retry_on_temporary_error: bool = True,
    **data,
) -> dict:
    """POST to a Graph API edge.

    `retry_on_temporary_error` blindly resends the exact same write on Meta's
    code 1 (“Please reduce the amount of data...”), which is often a
    misleading generic error rather than proof the write failed — Meta can
    return it even after creating the resource. That resend is safe for an
    idempotent edge like a like, but NOT for one that creates a new resource
    (a comment reply): resending can create a second, duplicate reply. Only
    idempotent callers should pass True.
    """
    data["access_token"] = get_page_access_token(page_key)
    attempts = max(1, config.META_POST_RETRIES) if retry_on_temporary_error else 1
    for attempt in range(attempts):
        resp = _http_session.post(
            _url(path), data=data, timeout=WRITE_TIMEOUT_SECONDS
        )
        _record_meta_usage(path, resp, page_key)
        result = resp.json()
        error = result.get("error") if isinstance(result, dict) else None
        if (
            isinstance(error, dict)
            and error.get("code") == 1
            and attempt + 1 < attempts
        ):
            print(
                f"Meta temporarily rejected {path} (code 1); retrying in "
                f"{config.META_POST_RETRY_DELAY_SECONDS:g}s.",
                flush=True,
            )
            time.sleep(config.META_POST_RETRY_DELAY_SECONDS)
            continue
        _raise_if_error(path, result)
        return result
    raise AssertionError("Graph post retry loop exited unexpectedly")


def iter_paged(path: str, *, page_key: str = config.DEFAULT_PAGE_KEY, **params):
    """Yield items from a Graph API edge, following `paging.next` links."""
    data = graph_get(path, page_key=page_key, **params)
    while True:
        if not isinstance(data.get("data"), list):
            raise GraphAPIError(f"{path}: expected a paginated list")
        yield from data["data"]
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return
        for attempt in range(GET_RETRIES):
            try:
                resp = _http_session.get(next_url, timeout=READ_TIMEOUT_SECONDS)
                _record_meta_usage(path, resp, page_key)
                data = resp.json()
                _raise_if_error(path, data)
                break
            except RequestException as exc:
                if attempt + 1 == GET_RETRIES:
                    raise GraphAPIError(
                        f"{path}: Meta page read failed after {GET_RETRIES} "
                        f"attempts: {exc}"
                    ) from exc
                print(
                    f"Meta reply-list page for {path} timed out; retrying once "
                    "before leaving this comment for the next cycle.",
                    flush=True,
                )
                time.sleep(attempt + 1)


def reply_to_comment(
    comment_id: str, message: str, *, platform: str, page_key: str = config.DEFAULT_PAGE_KEY
) -> str:
    """Post a public reply to a Facebook or Instagram comment. Returns the new reply's id."""
    if platform not in ("facebook", "instagram"):
        raise ValueError(f"Unsupported Meta platform: {platform}")
    edge = "comments" if platform == "facebook" else "replies"
    # Never blindly resend this write: it creates a new comment, so retrying
    # after an ambiguous error (Meta's code 1 can follow a write that actually
    # succeeded) risks posting a visible duplicate reply. The caller is
    # responsible for verifying via find_own_reply before trying again.
    resp = graph_post(
        f"{comment_id}/{edge}",
        message=message,
        retry_on_temporary_error=False,
        page_key=page_key,
    )
    return resp["id"]


# --- Facebook Page ---


def find_own_reply(
    comment_id: str, *, platform: str, page_key: str = config.DEFAULT_PAGE_KEY
) -> str | None:
    """Find replies from the configured Page or Instagram account, across pages."""
    page = _page(page_key)
    if platform == "facebook":
        _require_page(page_key, "FACEBOOK_PAGE_ID")
        replies = iter_paged(
            f"{comment_id}/comments",
            fields="id,from",
            filter="stream",
            # Request Meta's largest supported page to avoid spending a full
            # round trip on every small page of a busy comment thread.
            limit=100,
            page_key=page_key,
        )
        for reply in replies:
            if reply.get("from", {}).get("id") == page.facebook_page_id:
                return reply["id"]

        # Facebook's comments edge can occasionally return an empty result even
        # when a Page reply exists. Check the same relationship through the
        # comment object's nested comments field before declaring it unanswered.
        # This independent read prevents a transient edge result from creating
        # a duplicate automated reply.
        data = graph_get(
            comment_id, fields="comments.limit(100){id,from}", page_key=page_key
        )
        for reply in (data.get("comments") or {}).get("data", []):
            if reply.get("from", {}).get("id") == page.facebook_page_id:
                return reply["id"]
    elif platform == "instagram":
        username = get_instagram_username(page_key=page_key)
        if not username:
            raise GraphAPIError("Cannot identify the connected Instagram account")
        for reply in iter_paged(
            f"{comment_id}/replies", fields="id,username", page_key=page_key
        ):
            if str(reply.get("username") or "").casefold() == username.casefold():
                return reply["id"]
    else:
        raise ValueError(f"Unsupported Meta platform: {platform}")
    return None


def like_facebook_comment(comment_id: str, *, page_key: str = config.DEFAULT_PAGE_KEY) -> None:
    """Like a Facebook comment as the configured Page."""
    like_comment(comment_id, platform="facebook", page_key=page_key)


def like_comment(
    comment_id: str, *, platform: str = "facebook", page_key: str = config.DEFAULT_PAGE_KEY
) -> None:
    """Like a Facebook comment as the Page, or an Instagram comment as the user."""
    page = _page(page_key)
    if platform == "facebook":
        result = graph_post(f"{comment_id}/likes", page_key=page_key)
    elif platform == "instagram":
        _require_page(page_key, "INSTAGRAM_USER_ID")
        path = f"{page.instagram_user_id}/likes"
        resp = _http_session.post(
            _url(path),
            params={
                "access_token": get_user_access_token(page_key),
                "comment_id": comment_id,
            },
            timeout=WRITE_TIMEOUT_SECONDS,
        )
        _record_meta_usage(path, resp, page_key)
        result = resp.json()
        _raise_if_error(path, result)
    else:
        raise ValueError(f"Comment likes are not supported for {platform}.")
    if result.get("success") is not True:
        raise GraphAPIError(f"{comment_id}/likes: like was not confirmed")


def iter_facebook_post_ids(*, page_key: str = config.DEFAULT_PAGE_KEY):
    _require_page(page_key, "FACEBOOK_PAGE_ID")
    page = _page(page_key)
    if page.facebook_post_ids:
        yield from page.facebook_post_ids
        return
    for index, post in enumerate(iter_paged(
        f"{page.facebook_page_id}/posts",
        fields="id",
        limit=max(1, min(config.FACEBOOK_POST_LIMIT, 100)),
        page_key=page_key,
    )):
        if index >= config.FACEBOOK_POST_LIMIT:
            return
        yield post["id"]


def iter_facebook_post_comments(
    post_id: str, *, limit: int | None = None, page_key: str = config.DEFAULT_PAGE_KEY
):
    request_limit = max(1, min(limit or 100, 100))
    count = 0
    for comment in iter_paged(
        f"{post_id}/comments",
        fields="id,message,from,created_time",
        filter="toplevel",
        order="reverse_chronological",
        limit=request_limit,
        page_key=page_key,
    ):
        yield comment
        count += 1
        if limit is not None and count >= limit:
            return


def get_facebook_post_message(post_id: str, *, page_key: str = config.DEFAULT_PAGE_KEY) -> str:
    data = graph_get(post_id, fields="message", page_key=page_key)
    return data.get("message", "")


def get_facebook_post_stats(
    post_id: str, *, page_key: str = config.DEFAULT_PAGE_KEY
) -> dict:
    """Return a Facebook post's like/comment/share counts.

    Graph API omits the "shares" field entirely on a post with zero shares,
    so its absence here means zero shares, not unknown.
    """
    data = graph_get(
        post_id,
        fields="likes.summary(true).limit(0),comments.summary(true).limit(0),shares",
        page_key=page_key,
    )
    return {
        "like_count": data.get("likes", {}).get("summary", {}).get("total_count"),
        "comment_count": data.get("comments", {}).get("summary", {}).get("total_count"),
        "share_count": data.get("shares", {}).get("count", 0),
    }


# --- Instagram (via the linked Facebook Page's access token) ---


def iter_instagram_media_ids(*, page_key: str = config.DEFAULT_PAGE_KEY):
    _require_page(page_key, "INSTAGRAM_USER_ID")
    page = _page(page_key)
    if page.instagram_media_ids:
        yield from page.instagram_media_ids
        return
    # The /media edge is already returned newest-first, so capping here
    # limits polling to the N most recent posts.
    for i, media in enumerate(
        iter_paged(f"{page.instagram_user_id}/media", fields="id", page_key=page_key)
    ):
        if i >= config.INSTAGRAM_MEDIA_LIMIT:
            return
        yield media["id"]


def iter_instagram_media_comments(
    media_id: str, *, limit: int | None = None, page_key: str = config.DEFAULT_PAGE_KEY
):
    request_limit = max(1, min(limit or 100, 100))
    count = 0
    for comment in iter_paged(
        f"{media_id}/comments",
        fields="id,text,username,timestamp",
        limit=request_limit,
        page_key=page_key,
    ):
        yield comment
        count += 1
        if limit is not None and count >= limit:
            return


def get_instagram_media_caption(media_id: str, *, page_key: str = config.DEFAULT_PAGE_KEY) -> str:
    data = graph_get(media_id, fields="caption", page_key=page_key)
    return data.get("caption", "")


def get_instagram_media_stats(
    media_id: str, *, page_key: str = config.DEFAULT_PAGE_KEY
) -> dict:
    """Return an Instagram media's like/comment counts.

    Instagram's Graph API has no public share-count field, so callers only
    get likes and comments back.
    """
    data = graph_get(media_id, fields="like_count,comments_count", page_key=page_key)
    return {
        "like_count": data.get("like_count"),
        "comment_count": data.get("comments_count"),
    }


def get_instagram_username(*, page_key: str = config.DEFAULT_PAGE_KEY) -> str:
    if page_key in _ig_username_cache:
        return _ig_username_cache[page_key]
    _require_page(page_key, "INSTAGRAM_USER_ID")
    page = _page(page_key)
    data = graph_get(page.instagram_user_id, fields="username", page_key=page_key)
    _ig_username_cache[page_key] = data.get("username", "")
    return _ig_username_cache[page_key]
