import shutil
import logging
import tempfile
from pathlib import Path
from typing import Callable, Awaitable

from config import DOWNLOAD_DIR

from worker.gallery import _cookie_args, _run_gallery_dl, collect_posts, site_args, upload_to_group
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
