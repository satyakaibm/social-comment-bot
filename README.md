# social-comment-bot

Human-in-the-loop auto-replies for **YouTube**, **Facebook Pages**, and **Instagram**. The bot fetches new comments, drafts replies with Gemini, and only publishes after you approve them.

Nothing is posted automatically unless you run `post` or install `scripts/reply_cron.sh`.

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

Meta comments can be received immediately without scanning historical posts. The receiver validates Meta's `X-Hub-Signature-256`, saves each delivery to SQLite before processing it, ignores duplicate deliveries and replies, then drafts, replies, and likes the original comment. Set `META_WEBHOOK_AUTO_POST=false` to queue drafts for manual review instead.

1. Copy the Meta app secret into `META_APP_SECRET` and create a private random `META_WEBHOOK_VERIFY_TOKEN`.
2. Start the receiver:

   ```bash
   ./scripts/webhook_server.sh
   ```

3. Expose local port 8080 through a stable public HTTPS URL. The callback is `https://YOUR_HOST/webhooks/meta`; `/health` is available for monitoring.
4. In the Meta app dashboard, configure that callback and the same verify token.
5. Subscribe the Facebook Page webhook to `feed` and the Instagram webhook to `comments`, then subscribe the Hindolroad Page/account to the app.

Webhook delivery state is stored in the `webhook_events` table. Use one Gunicorn worker because its background processor owns this SQLite queue.

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

# Approve, edit, or reject drafts in the terminal
python -m app.cli review

# Localhost admin dashboard (pending, posted, failed, already-replied)
python -m app.cli dashboard
# or: ./scripts/dashboard.sh

# Publish approved replies (all platforms)
python -m app.cli post

# Post approved Facebook replies and like their original comments as the Page
python -m app.cli post --platform facebook --like-comments
python -m app.cli post --platform instagram --like-comments

# Post YouTube drafts straight from poll records (skip review), limited batch
python -m app.cli post --platform youtube --pending --limit 1

# Rebuild pending drafts with the current REPLY_PERSONA and example files
python -m app.cli redraft

# Continuously poll YouTube only (drafts, no posting)
python -m app.cli run
```

## Admin dashboard

Open a browser review UI on this machine. It reads `data/comments.db` and stays on localhost (default `http://127.0.0.1:9000/`). If that port is already in use, the server takes the next free port and prints the URL.

```bash
chmod +x scripts/dashboard.sh
./scripts/dashboard.sh
```

Tabs cover **pending review**, **approved**, **posted**, **failed**, **already replied**, and **rejected**. From pending you can save a draft, approve, approve-and-post, or reject. Failed posts keep the API error and can be retried. Override `DASHBOARD_HOST` / `DASHBOARD_PORT` in `.env` if needed. This is separate from the Meta webhook server on port 8080.

Posting failures are stored as `failed` so they show up in the dashboard. Checking whether you already replied still skips a comment for that run without marking it failed.

`poll-all` continues if one platform fails and prints the error.

## Cron (auto-reply)

`scripts/reply_cron.sh` polls YouTube because YouTube has no comment webhook. Meta polling is disabled when webhooks are used. Set `META_POLL_FALLBACK=true` only while the webhook receiver is unavailable. YouTube posts at most `YOUTUBE_CRON_LIMIT` replies per tick (default 50) so one run cannot exhaust the daily Data API quota.

Make it executable once:

```bash
chmod +x scripts/reply_cron.sh
```

Install on this Mac (every 30 minutes). Crontab has no `.env`; the script loads `.venv` and the app loads `.env` from the repo root.

```cron
*/30 * * * * /Users/satyakaran/Documents/D/myproject/social-comment-bot/scripts/reply_cron.sh
```

Logs append to `data/cron.log` (that folder is gitignored). Override with `CRON_LOG` / `YOUTUBE_CRON_LIMIT` if needed.

`--like-comments` works for Facebook and Instagram. It uses the Page token and the platform's comment-management permission. Likes are attempted after successful replies. If a like fails, the reply remains posted and the failure is reported; retry the like manually. Previously posted comments are not processed again. YouTube's API has no comment-like endpoint.

Review prompts: `[a]pprove` / `[e]dit & approve` / `[r]eject` / `[s]kip` / `[q]uit`.

## Notes

- Manual flow: drafts stay in `pending_review` until `review`, then `post`. The cron script posts pending drafts without that review step.
- `run` only polls YouTube. Use `poll-all` or `scripts/reply_cron.sh` for Facebook and Instagram (and to post pending drafts).
- Instagram polling is capped so the first run does not draft replies for every historical comment.
- Do not commit `.env` or `data/comments.db`.
