# social-comment-bot

Human-in-the-loop auto-replies for **YouTube**, **Facebook Pages**, and **Instagram**. The bot fetches new comments, drafts replies with Gemini, and only publishes after you approve them.

Nothing is posted automatically.

## How it works

1. **Poll** — pull new top-level comments. Already-seen comments (stored in SQLite) and your own comments are skipped.
2. **Draft** — Gemini writes a reply using `REPLY_PERSONA` plus every example file in `reply_examples/`. YouTube and Instagram drafts are prefixed with `@author` so the commenter is notified. Facebook replies are plain text (Graph API does not expose mentionable user IDs for public commenters).
3. **Review** — approve, edit-and-approve, reject, or skip each draft in the terminal.
4. **Post** — send approved replies via the YouTube Data API or Meta Graph API.

State lives in `data/comments.db` (created on first run).

Before drafting and again before posting, the bot checks all reply pages for a reply from your connected YouTube channel, Facebook Page, or Instagram username. If found, it saves the comment as `already_replied` and skips it, including replies you made manually. The pre-post check also protects drafts queued before this feature was added. Failed checks skip the comment for that run. Replies from other viewers do not prevent a reply.

This only detects replies visible to the API from the connected identity: Facebook replies from your personal profile are not Page replies. Likes alone do not count as replies. Avoid overlapping posting runs or replying manually while a posting run is active; the check and publication are separate API calls.

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
META_APP_SECRET=
META_WEBHOOK_VERIFY_TOKEN=
META_WEBHOOK_AUTO_POST=true
META_WEBHOOK_ENABLED=false
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

### Facebook and Instagram webhooks

Meta can deliver new comments immediately, so the bot does not need to rescan historical Facebook posts or Instagram media. The receiver validates Meta's `X-Hub-Signature-256`, stores each delivery in SQLite before processing, ignores duplicate deliveries and existing replies, then drafts and posts a reply and likes the original comment. Set `META_WEBHOOK_AUTO_POST=false` to save drafts for manual review.

1. Copy the Meta app secret into `META_APP_SECRET`. Generate a separate private random value for `META_WEBHOOK_VERIFY_TOKEN`.
2. Start the review dashboard. It now hosts the webhook receiver in the same process:

   ```bash
   ./scripts/dashboard.sh
   ```

3. Expose dashboard port 9001 through a stable public HTTPS address. Use `https://YOUR_HOST/webhooks/meta` as the callback URL. The dashboard remains at `/`, `/health` shows a browser-friendly status page, and `/api/health` returns JSON for monitoring.
4. In the Meta app dashboard, configure the callback and the same verify token.
5. Subscribe the Facebook Page webhook to `feed` and the Instagram webhook to `comments`, then subscribe the Hindolroad Page and Instagram professional account to the app.
6. After a real test comment is received successfully, set `META_WEBHOOK_ENABLED=true`. Until then, cron continues polling Meta as a fallback.

Webhook delivery state is visible in the `webhook_events` SQLite table. Run one dashboard process because its background processor owns this local SQLite queue. YouTube does not offer comment webhooks, so it still requires polling.

Run a polling and publishing cycle manually with `./scripts/reply_comments.sh`. Its output is appended to `data/polling.log`; follow a running cycle with `tail -f data/polling.log`.

If `FACEBOOK_POST_IDS` / `INSTAGRAM_MEDIA_IDS` are empty, Facebook polls all Page posts and Instagram polls the most recent `INSTAGRAM_MEDIA_LIMIT` media items. Set those ID lists to stay on specific posts.

## Reply examples

Put examples in `reply_examples/` (copy `_template.txt`). One file can hold **many** `comment:` / `reply:` pairs, or you can split them across files. Gemini still drafts every reply; it uses your pairs as a style list, not a skip list.

```text
comment: What time is the aarti?
reply: Aarti time is in the video description.
```

Examples are maintained manually; the bot does not create or append chant examples. `_template.txt` is ignored when loading examples.

On all platforms, the drafting prompt prohibits generic thanks for watching, commenting, supporting, or sharing devotion. For the hindolroad / Hindolroad channel, each reply must be **only Odia or only English** — never Hindi or mixed scripts. The persona takes priority over examples: when configured for emoji-only devotional greetings, drafts use 🙏. Questions receive a direct, brief answer. Existing drafts can be regenerated with `python -m app.cli redraft`.

Optional: `REPLY_EXAMPLES_DIR` in `.env` if you keep the folder somewhere else.

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

# Post approved Facebook replies and like their original comments as the Page
python -m app.cli post --platform facebook --like-comments

# Post YouTube drafts straight from poll records (skip review), limited batch
python -m app.cli post --platform youtube --pending --limit 1

# Rebuild pending drafts with the current REPLY_PERSONA and example files
python -m app.cli redraft

# Continuously poll YouTube only (drafts, no posting)
python -m app.cli run
```

`poll-all` continues if one platform fails and prints the error.

`--like-comments` is Facebook-only and requires a Page token with the appropriate engagement permissions (`pages_manage_engagement`). Likes are attempted after successful replies. If a like fails, the reply remains posted and the failure is reported; retry the like manually. Previously posted comments are not processed again. YouTube's API has no comment-like endpoint; Instagram likes are not implemented.

Review prompts: `[a]pprove` / `[e]dit & approve` / `[r]eject` / `[s]kip` / `[q]uit`.

## Notes

- Replies stay in `pending_review` until you run `review`, then `post`.
- `run` only polls YouTube. Use `poll-all` (or a cron job) for Facebook and Instagram.
- Instagram polling is capped so the first run does not draft replies for every historical comment.
- Do not commit `.env` or `data/comments.db`.
