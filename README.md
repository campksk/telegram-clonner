# tg-clone-bot

A Telegram bot that clones media (photos, videos, files) from groups and channels, and downloads posts from X, Instagram and TikTok, into a destination group. Everything is sorted into Topics: one per source chat for Telegram sources, and one per username for X, Instagram and TikTok.

## Setup

```bash
cp .env.example .env   # then fill in the values in .env
./setup.sh             # creates a venv and installs the packages
./run.sh               # starts the bot
```

The first time you run it, the bot asks in the terminal for your phone number and the login code of your user account, to create a session (stored in `sessions/`).

The bot uses your user account to read and copy media; the bot token is only used to receive your commands.

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| API_ID | Telegram API ID (get it at https://my.telegram.org) |
| API_HASH | Telegram API hash |
| BOT_TOKEN | Bot token from @BotFather |
| DEST_GROUP_ID | ID of the destination group (your user account must be an admin and the group must have Topics enabled) |
| ALLOWED_USER_IDS | **Required** — IDs of the users allowed to command the bot, separated by `,`. If empty the bot refuses to start |
| SEND_DELAY | Pause between sends, in seconds (default 1.5) |
| PARALLEL_WORKERS | Number of workers running at the same time (default 3) |
| PRESERVE_ORDER | `1` = always send in the original message order (default) · `0` = faster, but the order may get mixed up (especially in forward mode) |
| MAX_SCAN_MESSAGES | Maximum number of new media per run when cloning a chat (default 10000). Send the same source again to continue |
| PROFILE_MAX_POSTS | New Instagram profile posts per run (default 20). Send the profile link again to continue with older posts |
| PROFILE_POST_DELAY | Seconds to pause between profile posts (default 3), so Instagram does not rate-limit the account |
| DOWNLOAD_DIR | Folder for temporary downloads |
| SESSION_DIR | Folder for the sessions (default `./sessions`) |

## Cookies

Put these files in the bot's main folder:

| File | Used for |
|------|----------|
| cookies_ig.txt | Instagram (required) |
| cookies_x.txt | X/Twitter (for private accounts) |
| cookies_tiktok.txt | TikTok (use it if you get blocked) |

To export them, install the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) extension in Chrome, log in to the site, export, and save the file under the name from the table.

Cookie files give access to your accounts. Never share or commit them (`.gitignore` already excludes them).

## Usage

Send a link or text to the bot in a private chat. Only users listed in `ALLOWED_USER_IDS` are served.

| You want | Send |
|----------|------|
| Clone a whole group/channel | `https://t.me/username` · `username` · `-1001234567890` |
| Clone a private group/channel | `https://t.me/c/1234567890` (your user account must be a member) |
| Clone one message | `https://t.me/username/12345` · `https://t.me/c/1234567890/12345` |
| An X/Twitter post | `https://x.com/user/status/123...` |
| An Instagram post, reel or story | `https://instagram.com/reel/xxxxx` — `/p/`, `/reels/`, `/share/` and `/<username>/reel/` links all work |
| An Instagram profile | `https://instagram.com/username` |
| A TikTok video or photo post | `https://tiktok.com/@user/video/123...` |
| Send a link that was already sent | `force https://x.com/user/status/123...` |
| Cancel jobs | `/cancel` (all) or `/cancel <job name shown by /jobs>` |
| List running jobs | `/jobs` |
| Server status | `/ping` |

Instagram needs `cookies_ig.txt`. If a message contains several links, the first one is used.

## How it works

- **Telegram sources** are copied into a topic named after the source chat. Sources that allow forwarding use forward mode; sources that forbid it (and chats with users) are downloaded and uploaded again. If forwarding a message fails, the bot tries to copy it by download instead.
- **X, Instagram and TikTok** posts go into a topic named after the poster's username (if the username can't be determined, into "X Downloads", "Instagram Downloads" or "TikTok Downloads"). Each post is sent as one album (up to 10 items; larger posts become several albums). TikTok photo posts are sent without the music track.
- **Instagram profiles** are cloned newest post first, `PROFILE_MAX_POSTS` posts per run, with a pause of `PROFILE_POST_DELAY` seconds between posts. Every post that was sent is remembered, so sending the profile link again continues with older posts; `force <link>` starts over. The run stops after 3 failed posts in a row (usually expired cookies or Instagram limiting requests).
- **Sent items are remembered** in `sent_media.json` (Telegram messages, with an automatic `.bak` backup) and `sent_urls.json` (X/Instagram/TikTok links), so running a source again skips what was already sent. Delete a file to start over.
- **Cloning a chat can be resumed**: a job that was running when the bot stopped continues by itself on the next start (jobs stopped with `/cancel` do not). Single-message, X, Instagram and TikTok jobs are not resumed.
- **FloodWait**: when Telegram asks the bot to wait, it waits as long as requested and retries automatically.

## Project layout

| File | Purpose |
|------|---------|
| `main.py` | Entry point: login, and resuming unfinished jobs |
| `handlers/` | Receive messages and commands from users |
| `jobs.py` | Background jobs (tracking, cancelling, error reports) |
| `parsing.py` | Parsing the links and text users send |
| `worker/` | Cloning, sending media, topics and the gallery-dl downloader |
| `db.py` | Storage for sent messages, unfinished jobs and sent links |

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
