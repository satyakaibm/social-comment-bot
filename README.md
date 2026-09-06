# social-comment-bot

Human-in-the-loop auto-replies for **YouTube**, **Facebook Pages**, and **Instagram**. The bot fetches new comments, drafts replies with Gemini, and only publishes after you approve them.

Nothing is posted automatically.

## How it works

1. **Poll** — pull new top-level comments. Already-seen comments (stored in SQLite) and your own comments are skipped.
2. **Draft** — Gemini writes a reply using `REPLY_PERSONA`. YouTube and Instagram drafts are prefixed with `@author` so the commenter is notified. Facebook replies are plain text (Graph API does not expose mentionable user IDs for public commenters).
3. **Review** — approve, edit-and-approve, reject, or skip each draft in the terminal.
4. **Post** — send approved replies via the YouTube Data API or Meta Graph API.

State lives in `data/comments.db` (created on first run).

## Setup

Python 3.10+ recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` in the project root. You only need credentials for the platforms you use, plus a Gemini API key for drafting.

```env
# Gemini (required for drafting)
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
REPLY_PERSONA=You are a friendly, concise community manager. Keep replies under 3 sentences.

# Optional: seconds between polls for `python -m app.cli run` (default 300)
POLL_INTERVAL_SECONDS=300

# YouTube
YOUTUBE_OAUTH_CLIENT_ID=
YOUTUBE_OAUTH_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
# Optional: comma-separated video IDs. Leave blank to poll the channel's uploads.
YOUTUBE_VIDEO_IDS=

# Facebook / Instagram (Meta Graph API)
META_GRAPH_VERSION=v21.0
FACEBOOK_PAGE_ID=
FACEBOOK_PAGE_ACCESS_TOKEN=
INSTAGRAM_USER_ID=
# Optional: restrict polling to specific posts/media. Leave blank to poll the account.
FACEBOOK_POST_IDS=
INSTAGRAM_MEDIA_IDS=
# Cap of recent Instagram media items when INSTAGRAM_MEDIA_IDS is unset (default 10)
INSTAGRAM_MEDIA_LIMIT=10
```

### YouTube

1. Create an OAuth client (Desktop app) in Google Cloud with the YouTube Data API v3 enabled.
2. Put the client ID and secret in `.env`.
3. As the channel owner, run:

```bash
python scripts/youtube_oauth_setup.py
```

4. Paste the printed refresh token into `YOUTUBE_REFRESH_TOKEN`.

### Facebook and Instagram

Use a Facebook Page access token (or a user token with `pages_show_list`; the bot exchanges it for the Page token via `/me/accounts`). Instagram must be a professional account linked to that Page.

Typical Graph permissions include Page read/manage engagement, `pages_show_list`, and Instagram comment management (`instagram_basic`, `instagram_manage_comments`).

`FACEBOOK_PAGE_ID` is the Page's numeric ID. `INSTAGRAM_USER_ID` is the Instagram professional account ID (not the username).

If `FACEBOOK_POST_IDS` / `INSTAGRAM_MEDIA_IDS` are empty, Facebook polls all Page posts and Instagram polls the most recent `INSTAGRAM_MEDIA_LIMIT` media items. Set those ID lists to stay on specific posts.

## Usage

```bash
# Fetch + draft (per platform or all)
python -m app.cli poll
python -m app.cli poll-facebook
python -m app.cli poll-instagram
python -m app.cli poll-all

# Approve, edit, or reject drafts
python -m app.cli review

# Publish approved replies (all platforms)
python -m app.cli post

# Post YouTube drafts straight from poll records (skip review), limited batch
python -m app.cli post --platform youtube --pending --limit 1

# Rebuild pending drafts with the current REPLY_PERSONA
python -m app.cli redraft

# Continuously poll YouTube only (drafts, no posting)
python -m app.cli run
```

`poll-all` continues if one platform fails and prints the error.

Review prompts: `[a]pprove` / `[e]dit & approve` / `[r]eject` / `[s]kip` / `[q]uit`.

## Notes

- Replies stay in `pending_review` until you run `review`, then `post`.
- `run` only polls YouTube. Use `poll-all` (or a cron job) for Facebook and Instagram.
- Instagram polling is capped so the first run does not draft replies for every historical comment.
- Do not commit `.env` or `data/comments.db`.
