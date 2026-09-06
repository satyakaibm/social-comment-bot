import requests

from app import config

GRAPH_BASE = "https://graph.facebook.com"
REQUEST_TIMEOUT_SECONDS = 30

_ig_username_cache: str | None = None
_page_token_cache: str | None = None


class GraphAPIError(RuntimeError):
    pass


def _url(path: str) -> str:
    return f"{GRAPH_BASE}/{config.META_GRAPH_VERSION}/{path}"


def _raise_if_error(path: str, data: dict) -> None:
    if "error" in data:
        raise GraphAPIError(f"{path}: {data['error']}")


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
    resp = requests.get(
        _url("me/accounts"),
        params={"fields": "id,access_token", "access_token": config.FACEBOOK_PAGE_ACCESS_TOKEN},
        timeout=REQUEST_TIMEOUT_SECONDS,
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


def graph_get(path: str, **params) -> dict:
    params["access_token"] = get_page_access_token()
    resp = requests.get(_url(path), params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    data = resp.json()
    _raise_if_error(path, data)
    return data


def graph_post(path: str, **data) -> dict:
    data["access_token"] = get_page_access_token()
    resp = requests.post(_url(path), data=data, timeout=REQUEST_TIMEOUT_SECONDS)
    result = resp.json()
    _raise_if_error(path, result)
    return result


def iter_paged(path: str, **params):
    """Yield items from a Graph API edge, following `paging.next` links."""
    data = graph_get(path, **params)
    while True:
        yield from data.get("data", [])
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return
        resp = requests.get(next_url, timeout=REQUEST_TIMEOUT_SECONDS)
        data = resp.json()
        _raise_if_error(path, data)


def reply_to_comment(comment_id: str, message: str) -> str:
    """Post a public reply to a Facebook or Instagram comment. Returns the new reply's id."""
    resp = graph_post(f"{comment_id}/replies", message=message)
    return resp["id"]


# --- Facebook Page ---


def iter_facebook_post_ids():
    config.require("FACEBOOK_PAGE_ID")
    if config.FACEBOOK_POST_IDS:
        yield from config.FACEBOOK_POST_IDS
        return
    for post in iter_paged(f"{config.FACEBOOK_PAGE_ID}/posts", fields="id"):
        yield post["id"]


def iter_facebook_post_comments(post_id: str):
    yield from iter_paged(
        f"{post_id}/comments",
        fields="id,message,from,created_time",
        filter="stream",
        order="chronological",
    )


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


def iter_instagram_media_comments(media_id: str):
    yield from iter_paged(f"{media_id}/comments", fields="id,text,username,timestamp")


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
