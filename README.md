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
Video statistics are refreshed by the separate `video-stats` container, keeping
remote API and database work out of Gunicorn's request-serving process. The
worker is CPU-limited because its cached statistics are not latency-sensitive.

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
docker compose logs -f --tail=100 hindolroad-dashboard hindolroad-video-stats hindolroad-youtube-comments # Follow logs
docker compose up -d --build              # Apply code or .env changes
docker compose stop                      # Stop until explicitly started again
docker compose up -d                      # Start again
```

The existing Meta fallback polling cron job still runs on the host and needs the
Python setup below. Docker runs the dashboard/webhook service, video-statistics
worker, and YouTube comments worker.

### Local Python setup

Python 3.10+ recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Tests

Install the extra test runner, then run the suite from the repo root. Tests mock
YouTube, Meta, and Gemini — they do not call live APIs or touch `data/comments.db`.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Jenkins runs the same suite inside `Dockerfile.test` on every push (`docker build
-f Dockerfile.test`). Add a failing test with the change it protects; keep
network and credentials out of the suite.

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

Set `DASHBOARD_USERNAME` and the generated `DASHBOARD_PASSWORD_HASH_B64` in `.env`, and use a strong random `DASHBOARD_SECRET` of at least 32 characters for session signing. The Base64 encoding keeps Docker Compose from interpreting characters inside the password hash. On https://bot.hindolroad.download/ the process will not start with the example secret, with `DASHBOARD_COOKIE_SECURE=false`, or without `DB_ENCRYPTION_KEY`. Local HTTP can set `DASHBOARD_INSECURE_LOCAL=true` to keep the old defaults. Login sessions expire after `DASHBOARD_SESSION_HOURS` (12 by default), and repeated invalid attempts are temporarily rate limited.

After signing in, use **My Profile → Reset password** to change the password. The replacement hash is stored in the SQLite database and persists through container restarts. Changing the password invalidates existing dashboard sessions.

New portal users are added from **Settings → Add portal user** after you are signed in. The public `/signup` page is not open. User IDs are unique regardless of letter case, and every account uses the same password-strength requirements.

Set `DB_ENCRYPTION_KEY` to a random 32+ character value in `.env` (same file Docker and host cron already load). The first process that opens `data/comments.db` encrypts a legacy plaintext file in place. A copy of the database without that key is unreadable. Generate a key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The avatar menu links to **My Profile**, where each user can maintain a display name and email address, review account dates, and open the password reset form. Email addresses are unique across portal accounts and are matched without regard to letter case.

YouTube polling and publishing run in the dedicated `hindolroad-youtube-comments` container. It runs immediately when started and then every `YOUTUBE_POLL_INTERVAL_SECONDS` (3600 by default). Each configured channel is processed independently, so a token or API failure for one channel does not block the others. Output is available through `docker compose logs hindolroad-youtube-comments` and is also appended to the `POLLING_LOG_FILE` configured in `config/polling.env` (`data/polling.log` by default). The separate video-stats worker refreshes YouTube engagement every `YOUTUBE_VIDEO_STATS_REFRESH_MINUTES` (30 by default) and Facebook/Instagram engagement every `META_VIDEO_STATS_REFRESH_MINUTES` (30 by default).

### Statistics retention (YouTube Developer Policy III.E.4)

Statistics retrieved from the YouTube Data API are never displayed or stored
for more than 30 days:

- `VIDEO_STATS_HISTORY_RETENTION_DAYS` is clamped to 30 in `app/config.py`, so
  a larger value in any `.env` has no effect.
- Every stats-worker tick runs `prune_video_stats_history()` (deletes
  historical snapshots past the window) and `expire_stale_video_stats()`
  (clears the counts on a latest-snapshot row whose video has dropped out of
  the refresh set). Video titles are kept, since they are cached only to avoid
  re-spending API quota.
- No reporting window longer than 30 days exists anywhere in the dashboard:
  the Insights and Channel activity range selectors stop at **30 Days**, there
  is no "All time" view, and the Momentum analyses look back exactly 30 days.
  `tests/test_video_stats.py` asserts both the clamp and the window ceiling.
- `python -m app.cli purge-stats` applies the cap on demand rather than
  waiting for the next worker tick.

Trigger an immediate YouTube cycle with `docker compose restart hindolroad-youtube-comments`; the worker runs once on startup. The legacy `./scripts/reply_comments.sh` remains for Facebook and Instagram polling when Meta webhooks are disabled; it no longer handles YouTube.

Each worker cycle is bounded to recent content and a fixed number of comments per platform. Edit `config/polling.env` to control how many YouTube videos, Facebook posts, Instagram media items, and comments are checked in one run. The three `*_PUBLISH_LIMIT` values control how many replies can be attempted in that cycle. New work is processed first, then failed replies are retried after a remote duplicate check. `PUBLISH_ERROR_LIMIT` stops a platform after repeated consecutive API errors. Publishers atomically claim each comment before posting, and interrupted claims become retryable after ten minutes.

In addition to the per-cycle `*_PUBLISH_LIMIT`, each platform has an optional daily cap: `YOUTUBE_DAILY_REPLY_LIMIT`, `FACEBOOK_DAILY_REPLY_LIMIT`, `INSTAGRAM_DAILY_REPLY_LIMIT`. Unlike the per-cycle limit, this counts replies actually posted across every cron cycle in the current IST calendar day; once it's reached, remaining comments for that platform are left untouched until the next day. Set one of these in `config/polling.env` (or `.env`) to cap daily reply volume; leave unset or `0` for no daily cap.

Every new comment also costs one Gemini API call to draft its reply, billed on `GEMINI_API_KEY` regardless of platform. Set `GEMINI_DAILY_DRAFT_LIMIT` in `.env` to cap total drafts generated per IST calendar day across YouTube, Facebook, and Instagram combined. Once reached, cron polling leaves further new comments unseen for a later cycle (they're picked up again once the cap resets); a webhook-delivered comment can't be redelivered later, so it's saved with an empty draft and a note instead, for a manual reply from the dashboard. Leave unset or `0` for no cap.

If Meta webhooks are disabled, the host cron entry for Facebook and Instagram does not need limit variables:

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

On all platforms, the drafting prompt prohibits generic thanks for watching, commenting, supporting, or sharing devotion. The persona takes priority over examples: when configured for emoji-only devotional greetings, drafts use 🙏. Questions receive a direct, brief answer. Existing drafts can be regenerated with `python -m app.cli redraft`.

Optional: `REPLY_EXAMPLES_DIR` in `.env` if you keep the folder somewhere else.

### Reply language

Every page replies in the commenter's own language and script by default — Odia to an Odia comment, Kannada to Kannada, Tamil to Tamil, romanized to romanized — for any language Gemini supports. Three settings control it, each suffixable (`_2`..`_5`) per page:

| Variable | Default | What it does |
| --- | --- | --- |
| `REPLY_LANGUAGE_POLICY` | `match_commenter` | `match_commenter` mirrors the commenter's language. `odia_or_english` is the original Hindolroad rule: every reply wholly Odia or wholly English, any other Indic script transliterated to Odia or deleted. |
| `REPLY_FALLBACK_LANGUAGE` | unset | Language to use when a comment gives nothing to mirror — only emoji, only punctuation, a bare name. Unset leaves the choice to the persona. |
| `REPLY_AUTO_POST_SCRIPTS` | unset | Scripts allowed to post **without** a human reading the draft. Unset gates nothing. Set it (e.g. `Odia,Latin`) and a draft in any other script is held in `pending_review` for the dashboard instead of auto-posting. `Latin` covers English and anything romanized; names are listed in `app/scripts.py`. Only unreviewed drafts are gated — an approved one always posts. |

Three layers enforce the policy: the mandatory prompt rules (`app/generate.py`'s `_LANGUAGE_INSTRUCTIONS`), the examples preamble (`app/reply_examples.py`), and the sanitizer (`app/sanitize.py`), which keeps one Indic script per reply — the dominant one under `match_commenter`, always Odia under `odia_or_english`.

**Reply quality in a new language.** What makes the Odia replies read naturally is not only the language rule: it is the persona's "write it the way a native speaker casually writes, not a textbook translation" instruction *plus* a folder of Odia examples to imitate. A language with no examples gets the instruction but nothing to imitate, so its drafts start out stiffer. To lift one, add `comment:` / `reply:` pairs in that language to the page's examples folder — the model is told to copy the examples' tone and length but never their language, so a Kannada pair raises Kannada replies without pulling Odia replies toward Kannada. Until a language has been proof-read a few times, `REPLY_AUTO_POST_SCRIPTS` is the way to see its drafts before viewers do.


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

## Running this as a paid service (multiple customers)

`billing/` is a separate, small Flask app + SQLite database (own Docker Compose project, own Dockerfile) that turns this repo into a subscription service: customers pay via Razorpay, and each one gets their own isolated copy of this bot running as extra services inside this repo's own `social-comment-bot` Compose project (same network, same built images as the main instance -- no per-tenant image rebuild), on its own port and hostname.

Onboarding is semi-manual by design -- there's no way to automate collecting a customer's Facebook Page token, Instagram user ID, or YouTube refresh token without a full Meta/Google OAuth app review, so you still gather those yourself.

1. One-time setup: create a Plan in the Razorpay dashboard (Subscriptions > Plans), then fill in `billing/.env` from `billing/.env.example` (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_PLAN_ID`, `RAZORPAY_WEBHOOK_SECRET`, `BILLING_ADMIN_USERNAME`/`BILLING_ADMIN_PASSWORD_HASH_B64` via `scripts/generate_dashboard_password.py`, and `HOST_REPO_PATH` -- see the comment in `billing/docker-compose.yml` for why this must match the host path exactly). Then `cd billing && docker compose up -d --build`.
2. A customer signs up at `https://billing.<yourdomain>/`, completes Razorpay Checkout. The `subscription.activated` webhook marks them `pending_provisioning` in `/admin`.
3. You collect their Facebook Page ID/access token, Instagram user ID, and/or YouTube OAuth credentials (same fields as the "Setup" section above).
4. Run `python -m billing.cli provision <tenant_key>` (from the repo root, in the host venv or `docker compose exec billing-portal ...`). This writes `tenants/<tenant_key>.env` from `.env.example` with fresh secrets and a unique port, prints a one-time dashboard password, and leaves the platform credential lines as `# TODO` comments for you to fill in.
5. Fill in those TODO lines, then run `docker compose -p social-comment-bot -f docker-compose.yml -f tenants/<tenant_key>.compose.yml up -d <tenant_key>-dashboard <tenant_key>-video-stats <tenant_key>-youtube-comments` (or re-run `provision --start`). No `--build`: it reuses this instance's already-built `social-comment-bot-*` images.
6. Run `scripts/add_tenant_route.sh <tenant_key> <port>` to add a Cloudflare Tunnel hostname for them and reload `cloudflared`.
7. Have the customer point their Meta webhook subscription at `https://<tenant_key>.<yourdomain>/webhooks/meta` (see "Facebook and Instagram webhooks" above).

From here, billing is hands-off: `subscription.charged` keeps their containers running, and `subscription.halted`/`subscription.cancelled` stops them automatically -- no app code checks a "is this customer paid up" flag, the tenant's containers simply aren't running when they haven't paid.

## Notes

- Replies stay in `pending_review` until you run `review`, then `post`.
- `run` only polls YouTube. Use `poll-all` (or a cron job) for Facebook and Instagram.
- Instagram polling is capped so the first run does not draft replies for every historical comment.
- `prune` clears handled dashboard comment text (`posted`, `already_replied`, `rejected`) and processed webhook payloads. `seen_comments` keeps each `comment_id` plus `created_at` / `updated_at` / `status`, so polling will not re-reply and the 1 Hour–365 Day activity counts still work. Use `import-stats` to restore those timestamps from a `comments.db.bak` file.
- Do not commit `.env` or `data/comments.db`.
- Production serving refuses to start without a 32+ character `DASHBOARD_SECRET`, `DASHBOARD_COOKIE_SECURE=true` (the default unless `DASHBOARD_INSECURE_LOCAL` is set), and `DB_ENCRYPTION_KEY`.
