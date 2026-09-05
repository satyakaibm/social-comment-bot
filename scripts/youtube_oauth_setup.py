"""One-time interactive OAuth flow to obtain a YouTube refresh token.

Run this once, as the channel owner, after setting YOUTUBE_OAUTH_CLIENT_ID
and YOUTUBE_OAUTH_CLIENT_SECRET in .env. It opens a browser for consent, then
prints the resulting refresh token to paste into YOUTUBE_REFRESH_TOKEN.

Usage:
    python scripts/youtube_oauth_setup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from app import config
from app.youtube_client import SCOPES


def main() -> None:
    config.require("YOUTUBE_OAUTH_CLIENT_ID", "YOUTUBE_OAUTH_CLIENT_SECRET")
    client_config = {
        "installed": {
            "client_id": config.YOUTUBE_OAUTH_CLIENT_ID,
            "client_secret": config.YOUTUBE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0)

    if not creds.refresh_token:
        print(
            "No refresh token was returned. If you've authorized this app before, "
            "revoke access at https://myaccount.google.com/permissions and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nAuthorization complete. Add this to your .env file:\n")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}\n")


if __name__ == "__main__":
    main()
