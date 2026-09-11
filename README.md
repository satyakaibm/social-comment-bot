# social-comment-bot

Human-in-the-loop auto-replies for **YouTube**, **Facebook Pages**, and **Instagram**. The bot fetches new comments, drafts replies with Gemini, and only publishes after you approve them.

Nothing is posted automatically.

## How it works

1. **Poll** — pull new top-level comments. Already-seen comments (stored in SQLite) and your own comments are skipped.
2. **Draft** — Gemini writes a reply using the persona in `reply_examples/_reply_persona.txt` plus every example file in `reply_examples/`. Instagram drafts are prefixed with `@author` so the commenter is notified. YouTube replies are plain text beneath the original comment, and Facebook replies are plain text because its Graph API does not expose mentionable user IDs for public commenters.
3. **Review** — approve, edit-and-approve, reject, or skip each draft in the terminal.
4. **Post** — send approved replies via the YouTube Data API or Meta Graph API.

State lives in `data/comments.db` (created on first run).

Before drafting and again before posting, the bot checks all reply pages for a reply from your connected YouTube channel or Instagram username. If found, it saves the comment as `already_replied` and skips it, including replies you made manually. The pre-post check also protects drafts queued before this feature was added. Failed checks skip the comment for that run. Replies from other viewers do not prevent a reply.

Facebook uses a faster default: `FACEBOOK_VERIFY_EXISTING_REPLIES=false` avoids listing every reply on a busy thread before posting. The database still prevents a reply already confirmed as posted from being sent again. A Facebook write that reaches Meta but times out can be retried as a duplicate, and an existing manual Page reply is not detected. Set `FACEBOOK_VERIFY_EXISTING_REPLIES=true` if avoiding those duplicates is more important than speed.

This only detects replies visible to the API from the connected identity: Facebook replies from your personal profile are not Page replies. Likes alone do not count as replies. Avoid overlapping posting runs or replying manually while a posting run is active; the check and publication are separate API calls.

## Setup

### Docker dashboard (recommended for everyday use)

With Docker Desktop running and your `.env` configured (including
`META_APP_SECRET` and `META_WEBHOOK_VERIFY_TOKEN`), start the dashboard once:

```bash
docker compose up -d --build
```

Open http://localhost:9001. The dashboard and Meta webhook receiver run in the
background; you no longer need to run `scripts/dashboard.sh`. Stop any existing
manual dashboard before starting the container because they use the same port.
Keep one dashboard container/process running to own the webhook queue.

The container restarts automatically when Docker starts, unless you explicitly
stop it. Enable **Start Docker Desktop when you sign in** in Docker Desktop
settings to bring it back after a computer restart. Docker must remain running.

The dashboard shows API quota usage for the selected platform. YouTube displays
units observed by this bot against `YOUTUBE_DAILY_QUOTA_LIMIT` (default 10,000),
resetting at midnight Pacific Time. Facebook and Instagram display the latest
rolling usage percentage returned by Meta; their allowance is dynamic and shared
across app activity, so Meta does not expose a fixed daily unit total.

Your existing `data/` folder is mounted into the container, preserving comments
and webhook state across rebuilds and sharing them with the existing host polling
cron job. `reply_examples/` is mounted read-only, so example edits apply without
rebuilding. Credentials are loaded from `.env` at runtime and are excluded from
the image. `DASHBOARD_PORT` in `.env` can change the host port (default 9001).

```bash
docker compose ps                         # Status and health
docker compose logs -f --tail=100 dashboard # Follow logs
docker compose up -d --build              # Apply code or .env changes
docker compose stop                      # Stop until explicitly started again
docker compose up -d                      # Start again
```

The existing polling cron job still runs on the host and needs the Python setup
below. Docker runs the dashboard/webhook service only.

### Local Python setup

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
# The actual persona text lives in reply_examples/_reply_persona.txt
# (a plain text file, easier to edit than a single long .env line). This
# value is only a fallback used if that file is missing or empty.
REPLY_PERSONA=You are a friendly, concise community manager. Keep replies under 3 sentences.

# Optional: seconds between polls for `python -m app.cli run` (default 300)
POLL_INTERVAL_SECONDS=300

# YouTube
YOUTUBE_OAUTH_CLIENT_ID=
YOUTUBE_OAUTH_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
# Optional: comma-separated video IDs. Leave blank to poll the channel's uploads.
# Optional priority videos; these do not replace the latest-upload scan.
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

Use a Facebook **user** access token with `pages_show_list` (the bot exchanges it for a Page token via `/me/accounts`). A Page-only token can reply on Facebook and Instagram, but Instagram comment likes need the User token. Instagram must be a professional account linked to that Page.

Typical Graph permissions include Page read/manage engagement, `pages_show_list`, Instagram comment management (`instagram_basic`, `instagram_manage_comments`), and Instagram comment likes (`instagram_manage_engagement`).

`FACEBOOK_PAGE_ID` is the Page's numeric ID. `INSTAGRAM_USER_ID` is the Instagram professional account ID (not the username).

### Multiple pages/channels

One deployment can run more than one Facebook Page/Instagram account/YouTube channel, each with its own credentials, persona, and daily reply limits. The first page uses the unsuffixed vars above (`FACEBOOK_PAGE_ID`, `INSTAGRAM_USER_ID`, `YOUTUBE_REFRESH_TOKEN`, ...); additional pages repeat any of those vars with a numeric suffix `_2` through `_5`, e.g.:

```bash
PAGE_KEY_2=travel_explorer_satya
PAGE_LABEL_2=Travel Explorer Satya
FACEBOOK_PAGE_ID_2=
FACEBOOK_PAGE_ACCESS_TOKEN_2=
INSTAGRAM_USER_ID_2=
YOUTUBE_REFRESH_TOKEN_2=
REPLY_PERSONA_2=You are a friendly, concise travel community manager. Keep replies under 3 sentences.
```

`PAGE_KEY_2` is required once any `_2`-suffixed credential is set; `PAGE_LABEL_2` is what the dashboard's Channel switcher shows. Reply examples for that page live under `reply_examples/<PAGE_KEY_2>/`, mirroring the default `reply_examples/` layout. See `.env.example` for the full list of vars a second page can override.

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

### Dashboard login

The dashboard and browser health page require a login. Meta webhooks and `/api/health` remain public for delivery and container monitoring. Generate a password hash without storing the plaintext password. Passwords require at least 8 characters with one uppercase letter, one number, and one special character:

```bash
./.venv/bin/python scripts/generate_dashboard_password.py
```

Set `DASHBOARD_USERNAME` and the generated `DASHBOARD_PASSWORD_HASH_B64` in `.env`, and use a strong random `DASHBOARD_SECRET` for session signing. The Base64 encoding keeps Docker Compose from interpreting characters inside the password hash. Set `DASHBOARD_COOKIE_SECURE=true` when users access the dashboard through HTTPS. Login sessions expire after `DASHBOARD_SESSION_HOURS` (12 by default), and repeated invalid attempts are temporarily rate limited.

After signing in, use **My Profile → Reset password** to change the password. The replacement hash is stored in the SQLite database and persists through container restarts. Changing the password invalidates existing dashboard sessions.

New portal users can follow **Create an account** from the login page. User IDs are unique regardless of letter case, and every account uses the same password-strength requirements.

The avatar menu links to **My Profile**, where each user can maintain a display name and email address, review account dates, and open the password reset form. Email addresses are unique across portal accounts and are matched without regard to letter case.

Run a polling and publishing cycle manually with `./scripts/reply_comments.sh`. Its output is appended to the `POLLING_LOG_FILE` configured in `config/polling.env` (`data/polling.log` by default); follow a running cycle with `tail -f data/polling.log`.

Each cron cycle is bounded to recent content and a fixed number of comments per platform. Edit `config/polling.env` to control how many YouTube videos, Facebook posts, Instagram media items, and comments are checked in one run. The three `*_PUBLISH_LIMIT` values control how many replies can be attempted in that cycle. New work is processed first, then failed replies are retried after a remote duplicate check. `PUBLISH_ERROR_LIMIT` stops a platform after repeated consecutive API errors. The script prevents overlapping cron runs, and webhook and cron publishers atomically claim each comment before posting. Interrupted claims become retryable after ten minutes. The cron entry does not need limit variables:

In addition to the per-cycle `*_PUBLISH_LIMIT`, each platform has an optional daily cap: `YOUTUBE_DAILY_REPLY_LIMIT`, `FACEBOOK_DAILY_REPLY_LIMIT`, `INSTAGRAM_DAILY_REPLY_LIMIT`. Unlike the per-cycle limit, this counts replies actually posted across every cron cycle in the current IST calendar day; once it's reached, remaining comments for that platform are left untouched until the next day. Set one of these in `config/polling.env` (or `.env`) to cap daily reply volume; leave unset or `0` for no daily cap.

Every new comment also costs one Gemini API call to draft its reply, billed on `GEMINI_API_KEY` regardless of platform. Set `GEMINI_DAILY_DRAFT_LIMIT` in `.env` to cap total drafts generated per IST calendar day across YouTube, Facebook, and Instagram combined. Once reached, cron polling leaves further new comments unseen for a later cycle (they're picked up again once the cap resets); a webhook-delivered comment can't be redelivered later, so it's saved with an empty draft and a note instead, for a manual reply from the dashboard. Leave unset or `0` for no cap.

```cron
0 * * * * /Users/satyakaran/Documents/D/myproject/social-comment-bot/scripts/reply_comments.sh 2>&1
```

For another environment, create a file with the same variables and select it with `POLLING_CONFIG_FILE=/path/to/polling.env`. Set `FACEBOOK_POST_IDS` or `INSTAGRAM_MEDIA_IDS` in `.env` to stay on specific content.

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

# Post approved Facebook or Instagram replies and like their original comments
python -m app.cli post --platform facebook --like-comments
python -m app.cli post --platform instagram --like-comments

# Post YouTube drafts straight from poll records (skip review), limited batch
python -m app.cli post --platform youtube --pending --limit 1

# Rebuild pending drafts with the current REPLY_PERSONA and example files
python -m app.cli redraft

# Restore dashboard 1 Hour–365 Day counts from a backup after prune
python -m app.cli import-stats --backup data/comments.db.bak

# Reclaim disk: delete handled comment text and processed webhooks.
# Comment IDs and timestamps stay in seen_comments so they are not drafted
# again and activity counts still work. Stop the dashboard first.
# Pending/approved/failed rows are kept.
python -m app.cli prune
python -m app.cli prune --older-than-days 90

# Continuously poll YouTube only (drafts, no posting)
python -m app.cli run
```

`poll-all` continues if one platform fails and prints the error.

`--like-comments` likes the original Facebook or Instagram comment after a successful reply. Facebook likes use the Page token (`pages_manage_engagement`). Instagram likes use `POST /{INSTAGRAM_USER_ID}/likes` with the original User token in `FACEBOOK_PAGE_ACCESS_TOKEN` (`instagram_basic` and `instagram_manage_engagement`); a Page-only token can reply but cannot like. If a like fails, the reply remains posted and the failure is reported; retry the like manually. Previously posted comments are not processed again. YouTube's Data API has no comment-like endpoint.

When a batch has several Facebook or Instagram replies, it posts every reply first and then likes the original comments. This makes replies visible sooner when Meta's like endpoint is slow.

Review prompts: `[a]pprove` / `[e]dit & approve` / `[r]eject` / `[s]kip` / `[q]uit`.

## Notes

- Replies stay in `pending_review` until you run `review`, then `post`.
- `run` only polls YouTube. Use `poll-all` (or a cron job) for Facebook and Instagram.
- Instagram polling is capped so the first run does not draft replies for every historical comment.
- `prune` clears handled dashboard comment text (`posted`, `already_replied`, `rejected`) and processed webhook payloads. `seen_comments` keeps each `comment_id` plus `created_at` / `updated_at` / `status`, so polling will not re-reply and the 1 Hour–365 Day activity counts still work. Use `import-stats` to restore those timestamps from a `comments.db.bak` file.
- Do not commit `.env` or `data/comments.db`.
