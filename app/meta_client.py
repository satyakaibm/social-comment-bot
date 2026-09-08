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

_ig_username_cache: str | None = None
_page_token_cache: str | None = None
_user_token_cache: str | None = None
# Keep TCP/TLS connections open between Graph calls. A Facebook batch normally
# makes many small requests, so recreating a connection for every request adds
# avoidable latency.
_http_session = requests.Session()


class GraphAPIError(RuntimeError):
    pass


def _url(path: str) -> str:
    return f"{GRAPH_BASE}/{config.META_GRAPH_VERSION}/{path}"


def _raise_if_error(path: str, data: dict) -> None:
    if "error" in data:
        raise GraphAPIError(f"{path}: {data['error']}")


def _quota_platform(path: str) -> str | None:
    first = path.split("/", 1)[0]
    if first in {"me", ""}:
        return None
    return "facebook" if "_" in first or first == config.FACEBOOK_PAGE_ID else "instagram"


def _record_meta_usage(path: str, response) -> None:
    platform = _quota_platform(path)
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


def get_page_access_token() -> str:
    """Resolve a genuine Page access token.

    FACEBOOK_PAGE_ACCESS_TOKEN may be a user access token (with
    pages_show_list) rather than a Page token — Facebook's "New Pages
    Experience" rejects a plain user token on Page edges like /posts. If so,
    exchange it via /me/accounts for the real Page token and cache it.
    """
    global _page_token_cache
    if _page_token_cache is not None:
        return _page_token_cache

    config.require("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN")
    identity_resp = _http_session.get(
        _url("me"),
        params={"fields": "id", "access_token": config.FACEBOOK_PAGE_ACCESS_TOKEN},
        timeout=READ_TIMEOUT_SECONDS,
    )
    identity = identity_resp.json()
    _raise_if_error("me", identity)
    if identity.get("id") == config.FACEBOOK_PAGE_ID:
        _page_token_cache = config.FACEBOOK_PAGE_ACCESS_TOKEN
        return _page_token_cache

    resp = _http_session.get(
        _url("me/accounts"),
        params={"fields": "id,access_token", "access_token": config.FACEBOOK_PAGE_ACCESS_TOKEN},
        timeout=READ_TIMEOUT_SECONDS,
    )
    data = resp.json()
    _raise_if_error("me/accounts", data)

    for page in data.get("data", []):
        if page.get("id") == config.FACEBOOK_PAGE_ID:
            _page_token_cache = page["access_token"]
            return _page_token_cache

    # Fall back to the configured token in case it's already a Page token.
    _page_token_cache = config.FACEBOOK_PAGE_ACCESS_TOKEN
    return _page_token_cache


def get_user_access_token() -> str:
    """Return a User token for IG User edges that reject Page tokens.

    FACEBOOK_PAGE_ACCESS_TOKEN is often a user token that we later exchange
    for a Page token. Instagram likes require the original User token with
    instagram_manage_engagement.
    """
    global _user_token_cache
    if _user_token_cache is not None:
        return _user_token_cache

    config.require("FACEBOOK_PAGE_ACCESS_TOKEN")
    token = config.FACEBOOK_PAGE_ACCESS_TOKEN
    identity_resp = _http_session.get(
        _url("me"),
        params={"fields": "id", "access_token": token},
        timeout=READ_TIMEOUT_SECONDS,
    )
    identity = identity_resp.json()
    _raise_if_error("me", identity)
    if config.FACEBOOK_PAGE_ID and identity.get("id") == config.FACEBOOK_PAGE_ID:
        raise GraphAPIError(
            "Instagram comment likes require a User access token with "
            "instagram_manage_engagement; FACEBOOK_PAGE_ACCESS_TOKEN is a Page token."
        )
    _user_token_cache = token
    return _user_token_cache


def graph_get(path: str, **params) -> dict:
    params["access_token"] = get_page_access_token()
    for attempt in range(GET_RETRIES):
        try:
            resp = _http_session.get(
                _url(path), params=params, timeout=READ_TIMEOUT_SECONDS
            )
            _record_meta_usage(path, resp)
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


def graph_post(path: str, **data) -> dict:
    data["access_token"] = get_page_access_token()
    resp = _http_session.post(_url(path), data=data, timeout=WRITE_TIMEOUT_SECONDS)
    _record_meta_usage(path, resp)
    result = resp.json()
    _raise_if_error(path, result)
    return result


def iter_paged(path: str, **params):
    """Yield items from a Graph API edge, following `paging.next` links."""
    data = graph_get(path, **params)
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
                _record_meta_usage(path, resp)
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


def reply_to_comment(comment_id: str, message: str, *, platform: str) -> str:
    """Post a public reply to a Facebook or Instagram comment. Returns the new reply's id."""
    if platform not in ("facebook", "instagram"):
        raise ValueError(f"Unsupported Meta platform: {platform}")
    edge = "comments" if platform == "facebook" else "replies"
    resp = graph_post(f"{comment_id}/{edge}", message=message)
    return resp["id"]


# --- Facebook Page ---


def find_own_reply(comment_id: str, *, platform: str) -> str | None:
    """Find replies from the configured Page or Instagram account, across pages."""
    if platform == "facebook":
        config.require("FACEBOOK_PAGE_ID")
        replies = iter_paged(
            f"{comment_id}/comments",
            fields="id,from",
            filter="stream",
            # Request Meta's largest supported page to avoid spending a full
            # round trip on every small page of a busy comment thread.
            limit=100,
        )
        for reply in replies:
            if reply.get("from", {}).get("id") == config.FACEBOOK_PAGE_ID:
                return reply["id"]

        # Facebook's comments edge can occasionally return an empty result even
        # when a Page reply exists. Check the same relationship through the
        # comment object's nested comments field before declaring it unanswered.
        # This independent read prevents a transient edge result from creating
        # a duplicate automated reply.
        data = graph_get(comment_id, fields="comments.limit(100){id,from}")
        for reply in (data.get("comments") or {}).get("data", []):
            if reply.get("from", {}).get("id") == config.FACEBOOK_PAGE_ID:
                return reply["id"]
    elif platform == "instagram":
        username = get_instagram_username()
        if not username:
            raise GraphAPIError("Cannot identify the connected Instagram account")
        for reply in iter_paged(f"{comment_id}/replies", fields="id,username"):
            if str(reply.get("username") or "").casefold() == username.casefold():
                return reply["id"]
    else:
        raise ValueError(f"Unsupported Meta platform: {platform}")
    return None


def like_facebook_comment(comment_id: str) -> None:
    """Like a Facebook comment as the configured Page."""
    like_comment(comment_id, platform="facebook")


def like_comment(comment_id: str, *, platform: str = "facebook") -> None:
    """Like a Facebook comment as the Page, or an Instagram comment as the user."""
    if platform == "facebook":
        result = graph_post(f"{comment_id}/likes")
    elif platform == "instagram":
        config.require("INSTAGRAM_USER_ID")
        path = f"{config.INSTAGRAM_USER_ID}/likes"
        resp = _http_session.post(
            _url(path),
            params={
                "access_token": get_user_access_token(),
                "comment_id": comment_id,
            },
            timeout=WRITE_TIMEOUT_SECONDS,
        )
        _record_meta_usage(path, resp)
        result = resp.json()
        _raise_if_error(path, result)
    else:
        raise ValueError(f"Comment likes are not supported for {platform}.")
    if result.get("success") is not True:
        raise GraphAPIError(f"{comment_id}/likes: like was not confirmed")


def iter_facebook_post_ids():
    config.require("FACEBOOK_PAGE_ID")
    if config.FACEBOOK_POST_IDS:
        yield from config.FACEBOOK_POST_IDS
        return
    for index, post in enumerate(iter_paged(
        f"{config.FACEBOOK_PAGE_ID}/posts",
        fields="id",
        limit=max(1, min(config.FACEBOOK_POST_LIMIT, 100)),
    )):
        if index >= config.FACEBOOK_POST_LIMIT:
            return
        yield post["id"]


def iter_facebook_post_comments(post_id: str, *, limit: int | None = None):
    request_limit = max(1, min(limit or 100, 100))
    count = 0
    for comment in iter_paged(
        f"{post_id}/comments",
        fields="id,message,from,created_time",
        filter="toplevel",
        order="reverse_chronological",
        limit=request_limit,
    ):
        yield comment
        count += 1
        if limit is not None and count >= limit:
            return


def get_facebook_post_message(post_id: str) -> str:
    data = graph_get(post_id, fields="message")
    return data.get("message", "")


# --- Instagram (via the linked Facebook Page's access token) ---


def iter_instagram_media_ids():
    config.require("INSTAGRAM_USER_ID")
    if config.INSTAGRAM_MEDIA_IDS:
        yield from config.INSTAGRAM_MEDIA_IDS
        return
    # The /media edge is already returned newest-first, so capping here
    # limits polling to the N most recent posts.
    for i, media in enumerate(iter_paged(f"{config.INSTAGRAM_USER_ID}/media", fields="id")):
        if i >= config.INSTAGRAM_MEDIA_LIMIT:
            return
        yield media["id"]


def iter_instagram_media_comments(media_id: str, *, limit: int | None = None):
    request_limit = max(1, min(limit or 100, 100))
    count = 0
    for comment in iter_paged(
        f"{media_id}/comments",
        fields="id,text,username,timestamp",
        limit=request_limit,
    ):
        yield comment
        count += 1
        if limit is not None and count >= limit:
            return


def get_instagram_media_caption(media_id: str) -> str:
    data = graph_get(media_id, fields="caption")
    return data.get("caption", "")


def get_instagram_username() -> str:
    global _ig_username_cache
    if _ig_username_cache is None:
        config.require("INSTAGRAM_USER_ID")
        data = graph_get(config.INSTAGRAM_USER_ID, fields="username")
        _ig_username_cache = data.get("username", "")
    return _ig_username_cache
