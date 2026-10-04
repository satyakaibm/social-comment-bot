#!/usr/bin/env python3
"""Turn a short-lived Meta user token into a tenant's four credential values.

Run this after generating a User token in the Graph API Explorer for the
tenant's own Meta app. It never prints a token: everything secret goes
straight into tenants/<tenant>.env, and stdout gets only ids, names,
lengths and expiries so the result can be verified in a transcript.

    python scripts/finish_meta_tenant_tokens.py gudiakateni 1118330220656131 /tmp/token.txt

The app id is the tenant's OWN Meta App (each tenant has one: extending a
token in one app invalidates its sibling in another), found on that app's
dashboard URL or Basic Settings page.

Fills in FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN, META_USER_ACCESS_TOKEN
and INSTAGRAM_USER_ID, leaving every other line of the .env untouched.

Why a long-lived exchange at all: the Explorer hands out a ~1 hour token.
Exchanged for a long-lived user token, the Page tokens derived from it via
/me/accounts do not expire, which is what the bot needs to keep replying
without a human re-authorising it every hour.
"""

import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v26.0"
REPO = pathlib.Path(__file__).resolve().parent.parent


def get(path: str, **params) -> dict:
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.load(resp)
    if "error" in body:
        raise SystemExit(f"Graph error on /{path}: {body['error'].get('message')}")
    return body


def set_env(text: str, key: str, value: str) -> str:
    """Replace KEY=... at line start, preserving everything else."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if not pattern.search(text):
        raise SystemExit(f"{key} not found in the env file")
    return pattern.sub(f"{key}={value}", text, count=1)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    tenant, app_id, token_path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
    if not app_id.isdigit():
        raise SystemExit(f"Second argument must be the numeric Meta App id, got {app_id!r}")

    env_path = REPO / "tenants" / f"{tenant}.env"
    if not env_path.exists():
        raise SystemExit(f"No such tenant env: {env_path}")
    env = env_path.read_text()

    short = token_path.read_text().strip()
    if not short.startswith("EAA"):
        raise SystemExit("That file does not look like a Meta access token.")

    secret = re.search(r"^META_APP_SECRET=(.*)$", env, re.M).group(1).strip()
    if not secret or len(secret) != 32:
        raise SystemExit("META_APP_SECRET looks unset in the env file.")

    print(f"short-lived token: {len(short)} chars")

    # 1. Short-lived -> long-lived user token (~60 days).
    long_lived = get(
        "oauth/access_token",
        grant_type="fb_exchange_token",
        client_id=app_id,
        client_secret=secret,
        fb_exchange_token=short,
    )["access_token"]
    print(f"long-lived user token: {len(long_lived)} chars")

    # 2. Confirm what that token actually carries before trusting it.
    debug = get("debug_token", input_token=long_lived, access_token=f"{app_id}|{secret}")["data"]
    expires = debug.get("expires_at", 0)
    scopes = sorted(debug.get("scopes", []))
    print(f"  expires_at: {expires} ({'never' if expires == 0 else 'see below'})")
    print(f"  scopes ({len(scopes)}): {', '.join(scopes)}")
    for needed in ("pages_manage_engagement", "instagram_manage_engagement",
                   "instagram_manage_comments", "pages_manage_metadata"):
        print(f"  {'OK ' if needed in scopes else 'MISSING'} {needed}")

    # 3. The Page and its never-expiring Page token.
    accounts = get("me/accounts", access_token=long_lived).get("data", [])
    if not accounts:
        raise SystemExit("This token can see no Pages -- was the Page ticked in the dialog?")
    print(f"\npages visible to this token ({len(accounts)}):")
    for acct in accounts:
        print(f"  {acct['id']}  {acct.get('name')}")

    match = [a for a in accounts if tenant.lower() in (a.get("name", "").lower().replace(" ", ""))]
    page = match[0] if len(match) == 1 else (accounts[0] if len(accounts) == 1 else None)
    if page is None:
        raise SystemExit(
            "Could not pick a Page automatically. Re-run with the right one: "
            "edit this script's selection, or pass a single-Page token."
        )
    print(f"\nselected page: {page['id']}  {page.get('name')}")

    page_token = page["access_token"]
    page_debug = get("debug_token", input_token=page_token,
                     access_token=f"{app_id}|{secret}")["data"]
    print(f"  page token: {len(page_token)} chars, expires_at={page_debug.get('expires_at')}")

    # 4. The Instagram account linked to that Page.
    ig = get(page["id"], fields="instagram_business_account,name", access_token=page_token)
    ig_account = ig.get("instagram_business_account")
    if not ig_account:
        print("\nWARNING: no instagram_business_account linked to this Page.")
        print("INSTAGRAM_USER_ID left blank -- link the IG account in Page settings, re-run.")
        ig_id = ""
    else:
        ig_id = ig_account["id"]
        profile = get(ig_id, fields="username,followers_count", access_token=page_token)
        print(f"  instagram: {ig_id}  @{profile.get('username')} "
              f"({profile.get('followers_count')} followers)")

    env = set_env(env, "FACEBOOK_PAGE_ID", page["id"])
    env = set_env(env, "FACEBOOK_PAGE_ACCESS_TOKEN", page_token)
    env = set_env(env, "META_USER_ACCESS_TOKEN", long_lived)
    if ig_id:
        env = set_env(env, "INSTAGRAM_USER_ID", ig_id)
    env_path.write_text(env)
    print(f"\nwrote {env_path}")

    remaining = [l.split("=")[0] for l in env.splitlines()
                 if l.endswith("=") and not l.startswith("#")]
    print("still empty:", ", ".join(remaining) or "(nothing)")


if __name__ == "__main__":
    main()
