"""One-time interactive OAuth flow to obtain a YouTube refresh token.

Run this once, as the channel owner, after setting YOUTUBE_OAUTH_CLIENT_ID
and YOUTUBE_OAUTH_CLIENT_SECRET in .env. It opens a browser for consent, then
prints the resulting refresh token to paste into YOUTUBE_REFRESH_TOKEN.

For a second+ page (see .env's PAGE_KEY_2 etc.), pass that page's env var
suffix with --suffix so it reads YOUTUBE_OAUTH_CLIENT_ID<suffix>/SECRET<suffix>
instead -- in that mode the refresh token is written straight into .env's
YOUTUBE_REFRESH_TOKEN<suffix> line rather than printed, since it's easy to
mismatch which page's line to paste it into by hand.

Usage:
    python scripts/youtube_oauth_setup.py
    python scripts/youtube_oauth_setup.py --suffix _2
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from app import config
from app.youtube_client import SCOPES

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _write_refresh_token(var_name: str, token: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(var_name)}=.*$", re.MULTILINE)
    line = f"{var_name}={token}"
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suffix",
        default="",
        help="Env var suffix for a second+ page, e.g. _2 (matches PAGE_KEY_2 "
        "etc. in .env). Leave unset for the default/unsuffixed page.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't try to auto-launch a browser; just print the "
        "authorization URL to open manually (e.g. in a different "
        "profile/browser signed into the right Google account).",
    )
    args = parser.parse_args()
    suffix = args.suffix

    client_id_var = f"YOUTUBE_OAUTH_CLIENT_ID{suffix}"
    client_secret_var = f"YOUTUBE_OAUTH_CLIENT_SECRET{suffix}"
    refresh_var = f"YOUTUBE_REFRESH_TOKEN{suffix}"

    client_id = os.environ.get(client_id_var, "")
    client_secret = os.environ.get(client_secret_var, "")
    if not client_id or not client_secret:
        print(
            f"Missing {client_id_var}/{client_secret_var} in .env "
            "(see .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0, open_browser=not args.no_browser)

    if not creds.refresh_token:
        print(
            "No refresh token was returned. If you've authorized this app before, "
            "revoke access at https://myaccount.google.com/permissions and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if suffix:
        _write_refresh_token(refresh_var, creds.refresh_token)
        print(
            f"\nAuthorization complete. Saved {refresh_var} to .env "
            f"({len(creds.refresh_token)} chars).\n"
        )
    else:
        print("\nAuthorization complete. Add this to your .env file:\n")
        print(f"{refresh_var}={creds.refresh_token}\n")


if __name__ == "__main__":
    main()
