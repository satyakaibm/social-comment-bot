#!/usr/bin/env python3
"""Check (and optionally fix) a tenant's Meta ids against the live Graph API.

Reads tenants/<tenant>.env, uses the tokens already in it, and reports what
Meta actually says the Page and linked Instagram account are. Prints ids,
names and scopes only -- never a token.

    python scripts/verify_meta_tenant_env.py gudiakateni          # report
    python scripts/verify_meta_tenant_env.py gudiakateni --fix    # also write

Exists because the Instagram *app* id shown on the "API setup with Instagram
login" panel is easy to mistake for INSTAGRAM_USER_ID, which must instead be
the IG business account linked to the Facebook Page. The two are unrelated
numbers and the wrong one fails only at reply/like time, far from the cause.
"""

import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v26.0"
REPO = pathlib.Path(__file__).resolve().parent.parent


def get(path: str, **params):
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.load(exc)


def val(env: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}=(.*)$", env, re.M)
    return m.group(1).strip() if m else ""


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    fix = "--fix" in sys.argv
    if len(args) != 1:
        raise SystemExit(__doc__)
    tenant = args[0]

    path = REPO / "tenants" / f"{tenant}.env"
    env = path.read_text()
    app_secret = val(env, "META_APP_SECRET")
    user_token = val(env, "META_USER_ACCESS_TOKEN")
    page_token = val(env, "FACEBOOK_PAGE_ACCESS_TOKEN")

    if not user_token:
        raise SystemExit("META_USER_ACCESS_TOKEN is empty -- nothing to verify.")

    print("== token scopes ==")
    # Inspect the token using itself as the caller. Each tenant has its own
    # Meta App (deliberately -- extending a token in one app invalidates its
    # sibling in another), so the app id must come from the token rather than
    # being hardcoded, or this script only ever works for one tenant.
    dbg = get("debug_token", input_token=user_token,
              access_token=user_token).get("data", {})
    if "error" in dbg or not dbg:
        raise SystemExit(f"debug_token failed: {dbg}")
    app_id = str(dbg.get("app_id", ""))
    if not app_id:
        raise SystemExit("debug_token returned no app_id; cannot continue.")
    scopes = sorted(dbg.get("scopes", []))
    exp = dbg.get("expires_at", 0)
    print(f"  type={dbg.get('type')} app_id={dbg.get('app_id')} valid={dbg.get('is_valid')}")
    print(f"  expires_at={exp} ({'NEVER (long-lived)' if exp == 0 else 'expires - see below'})")
    for need in ("pages_manage_engagement", "pages_manage_metadata",
                 "pages_read_engagement", "pages_read_user_content",
                 "instagram_basic", "instagram_manage_comments",
                 "instagram_manage_engagement"):
        print(f"  {'OK     ' if need in scopes else 'MISSING'} {need}")

    print("\n== pages this token can see ==")
    accounts = get("me/accounts", access_token=user_token)
    if "error" in accounts:
        raise SystemExit(f"  /me/accounts failed: {accounts['error'].get('message')}")
    data = accounts.get("data", [])
    if not data:
        raise SystemExit("  none -- the Page was not granted in the consent dialog.")
    for a in data:
        print(f"  id={a['id']}  name={a.get('name')!r}")

    page = data[0] if len(data) == 1 else next(
        (a for a in data if tenant.lower().replace("_", "") in
         a.get("name", "").lower().replace(" ", "").replace("_", "")), None)
    if page is None:
        print("\n  Could not auto-select a Page; pick one from the list above.")
        return
    print(f"\n  -> selected: {page['id']} ({page.get('name')!r})")

    tok = page.get("access_token") or page_token
    print("\n== instagram account linked to that page ==")
    ig = get(page["id"], fields="instagram_business_account,name", access_token=tok)
    acct = ig.get("instagram_business_account") if "error" not in ig else None
    if not acct:
        print(f"  none linked (or no permission): {ig.get('error', {}).get('message', '-')}")
        ig_id = ""
    else:
        ig_id = acct["id"]
        prof = get(ig_id, fields="username,followers_count,media_count", access_token=tok)
        print(f"  id={ig_id}  @{prof.get('username')}  "
              f"followers={prof.get('followers_count')}  media={prof.get('media_count')}")

    print("\n== env comparison ==")
    for key, actual in (("FACEBOOK_PAGE_ID", page["id"]), ("INSTAGRAM_USER_ID", ig_id)):
        current = val(env, key)
        state = "OK" if current == actual else ("EMPTY" if not current else "WRONG")
        print(f"  {key:26s} env={current or '(empty)':20s} actual={actual or '-':20s} {state}")

    if fix:
        for key, actual in (("FACEBOOK_PAGE_ID", page["id"]), ("INSTAGRAM_USER_ID", ig_id)):
            if actual and val(env, key) != actual:
                env = re.sub(rf"^{key}=.*$", f"{key}={actual}", env, count=1, flags=re.M)
        path.write_text(env)
        print(f"\nwrote {path}")
    else:
        print("\n(re-run with --fix to write the correct values)")


if __name__ == "__main__":
    main()
