import asyncio
import re
import shutil
import logging
import tempfile
from pathlib import Path
from typing import Callable, Awaitable

import db
from config import DOWNLOAD_DIR, PROFILE_MAX_POSTS, PROFILE_POST_DELAY
from parsing import normalize_url

from worker.gallery import _cookie_args, _run_gallery_dl, collect_posts, site_args, stream_gallery_dl_print, upload_to_group
from worker.topics import get_or_create_topic

log = logging.getLogger("clone-bot.worker.media_sites")
StatusCb = Callable[[str], Awaitable[None]]


def _topic_title(username: str | None, fallback: str) -> str:
    """Topic name for a poster: their username (Telegram allows up to 128 characters)."""
    title = "".join(ch for ch in (username or "") if ch.isprintable()).strip()[:128]
    return title or fallback


def _new_job_dir(prefix: str) -> Path:
    """A fresh, unique download directory, so two jobs can never share (or delete) each other's files."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}_", dir=DOWNLOAD_DIR))


async def _clone_with_gallery_dl(
    url: str,
    status_cb: StatusCb,
    *,
    label: str,          # shown to the user: "X", "Instagram", "TikTok"
    site_key: str,       # cookie file key in worker.gallery.COOKIE_FILES
    upload_name: str,    # name used in upload messages
    topic_name: str,
    job_prefix: str,
    empty_msg: str,
    fail_hint: str = "",
    hint_only_without_cookies: bool = False,
    require_cookies: bool = False,
) -> bool:
    """Download `url` with gallery-dl and upload it to the destination topic.

    Returns True only if at least one file reached the destination.
    """
    await status_cb(f"🔍 {label} link detected\n📥 Downloading...")

    cookies = _cookie_args(site_key)
    if require_cookies and not cookies:
        await status_cb(f"❌ Download failed: `cookies_{site_key}.txt` not found\n💡 Cookies are required for {label}")
        return False

    job_dir = _new_job_dir(job_prefix)
    try:
        ok, err = await _run_gallery_dl(url, job_dir, cookies + site_args(site_key))
        if not ok:
            hint = fail_hint if (fail_hint and not (hint_only_without_cookies and cookies)) else ""
            await status_cb(f"❌ {label} download failed:\n`{err}`{hint}")
            return False

        posts = collect_posts(job_dir)
        if not posts:
            await status_cb(empty_msg)
            return False

        total_files = sum(len(post.files) for post in posts)
        await status_cb(f"✅ Downloaded {total_files} files\n📤 Uploading to group...")
        # One topic per poster, named after their username; if the username is unknown, the site's topic.
        topics: dict[str | None, tuple[int, str]] = {}
        try:
            for username in dict.fromkeys(post.username for post in posts):
                title = _topic_title(username, topic_name)
                topics[username] = (await get_or_create_topic(title), title)
        except Exception as e:
            await status_cb(f"❌ Failed to create/find topic: `{e}`")
            return False
        return await upload_to_group(posts, topics, upload_name, status_cb)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def clone_x_media(url: str, status_cb: StatusCb) -> bool:
    return await _clone_with_gallery_dl(
        url, status_cb,
        label="X", site_key="x", upload_name="X", topic_name="X Downloads", job_prefix="x",
        empty_msg="⚠️ No images/videos found, or you don't have access (Cookies required)",
        fail_hint="\n\n💡 If the account is private, place a `cookies_x.txt` file in the bot's main folder",
        hint_only_without_cookies=True,
    )


async def clone_ig_media(url: str, status_cb: StatusCb) -> bool:
    return await _clone_with_gallery_dl(
        url, status_cb,
        label="Instagram", site_key="ig", upload_name="IG", topic_name="Instagram Downloads", job_prefix="ig",
        empty_msg="⚠️ No images/videos found (post may have been deleted)",
        fail_hint="\n\n💡 Cookies may have expired. Please update your `cookies_ig.txt` file",
        require_cookies=True,
    )


async def clone_tiktok_media(url: str, status_cb: StatusCb) -> bool:
    return await _clone_with_gallery_dl(
        url, status_cb,
        label="TikTok", site_key="tiktok", upload_name="TikTok", topic_name="TikTok Downloads", job_prefix="tiktok",
        empty_msg="⚠️ No videos found (post may have been deleted or set to private)",
        fail_hint="\n\n💡 If blocked, place a `cookies_tiktok.txt` file in the bot folder",
        hint_only_without_cookies=True,
    )


# Real posts only (stories and highlights are not part of a profile's post list)
_POST_URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:[^/?#]+/)?(?:p|reels?|tv)/([^/?#]+)")
MAX_FAILED_IN_A_ROW = 3


async def clone_ig_profile(username: str, status_cb: StatusCb, *, force: bool = False) -> bool:
    """Download the posts of an Instagram profile, newest first, each into the topic named after the user.

    At most PROFILE_MAX_POSTS posts that were not sent before are taken per run, and every post that
    went through is remembered, so sending the profile link again continues with older posts.
    `force` ignores what was sent before. Returns True if the run did what it should (including
    "nothing new").
    """
    cookies = _cookie_args("ig")
    if not cookies:
        await status_cb("❌ Download failed: `cookies_ig.txt` not found\n💡 Cookies are required for Instagram")
        return False
    await status_cb(f"🔍 Instagram profile @{username} detected\n📋 Looking for posts...")

    already_sent = set() if force else set(db.get_sent_urls())
    limit = max(1, PROFILE_MAX_POSTS)
    new_posts: list[str] = []
    seen: set[str] = set()
    skipped = 0

    def on_line(line: str) -> bool:
        nonlocal skipped
        m = _POST_URL_RE.search(line)
        if not m or m.group(1) in seen:
            return False  # not a post line, or another picture of a post we already know
        code = m.group(1)
        seen.add(code)
        post_url = f"https://www.instagram.com/p/{code}/"
        if normalize_url(post_url) in already_sent:
            skipped += 1
            return False
        new_posts.append(post_url)
        return len(new_posts) >= limit  # enough: stop listing

    ok, err = await stream_gallery_dl_print(f"https://www.instagram.com/{username}/", cookies, "{post_url}", on_line)
    if not ok and not new_posts:
        hint = "\n\n💡 Cookies may have expired, or the account is private/does not exist" if "timeout" not in err else ""
        await status_cb(f"❌ Could not list the posts of @{username}:\n`{err}`{hint}")
        return False
    if not new_posts:
        await status_cb(f"✅ @{username}: nothing new — all {skipped} posts were already sent\n(use `force <link>` to send them again)")
        return True
    more_may_exist = len(new_posts) >= limit or not ok

    done = failed = in_a_row = 0
    last_error = ""

    async def quiet(text: str):  # per-post messages would drown the progress line; keep the last error
        nonlocal last_error
        if text.startswith("❌") or text.startswith("⚠️"):
            last_error = text

    aborted = False
    for i, post_url in enumerate(new_posts, 1):
        await status_cb(f"📥 @{username}: post {i}/{len(new_posts)}\n• Sent: {done} • Failed: {failed}")
        if await clone_ig_media(post_url, quiet):
            await db.mark_url_sent(normalize_url(post_url))
            done += 1
            in_a_row = 0
        else:
            failed += 1
            in_a_row += 1
            if in_a_row >= MAX_FAILED_IN_A_ROW:
                aborted = True
                break
        if i < len(new_posts):
            await asyncio.sleep(PROFILE_POST_DELAY)

    lines = [f"{'⛔' if aborted else '✅'} Profile @{username}: {done} posts sent, {failed} failed"]
    if aborted:
        lines.append(f"Stopped after {MAX_FAILED_IN_A_ROW} failed posts in a row (expired cookies or Instagram limiting requests?)")
        if last_error:
            lines.append(last_error)
    elif more_may_exist:
        lines.append(f"There may be more posts — send the profile link again to continue with older ones ({limit} per run)")
    await status_cb("\n".join(lines))
    return done > 0
