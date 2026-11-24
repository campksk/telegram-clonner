import asyncio
import os
import shutil
import sys
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

from worker.utils import retry_flood
from config import DEST_GROUP_ID, user_client

log = logging.getLogger("clone-bot.worker.gallery")
StatusCb = Callable[[str], Awaitable[None]]

GALLERY_DL_TIMEOUT = 300  # seconds

# File types, by how Telegram treats them in an album: photos and videos can be mixed in one album,
# anything else (GIF/WebP/AVIF/HEIC, audio) can only be albumed with other "files", never with photos.
PHOTO_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".opus", ".wav"}
OTHER_EXTS = {".gif", ".webp", ".avif", ".heic"}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS | AUDIO_EXTS | OTHER_EXTS
MIN_FILE_SIZE = 1024   # smaller files are error pages / placeholders
ALBUM_LIMIT = 10       # Telegram allows at most 10 items per album

# Per-site gallery-dl options. Every post goes into its own directory <category>/<username>/<post id>/,
# which is how we know the username (topic name) and which files belong to the same post.
# TikTok photo posts also come with the post's music track; we don't want that mixed into the album.
SITE_ARGS = {
    "x": ["-o", 'directory=["{category}","{user[name]}","{tweet_id}"]'],
    "ig": ["-o", 'directory=["{category}","{username}","{post_id}"]'],
    "tiktok": ["-o", 'directory=["{category}","{user}","{id}"]', "-o", "audio=false"],
}


def site_args(site: str) -> list[str]:
    return list(SITE_ARGS.get(site, []))


COOKIE_FILES = {
    "x": Path("cookies_x.txt"),
    "ig": Path("cookies_ig.txt"),
    "tiktok": Path("cookies_tiktok.txt"),
}


def _gallery_dl_cmd() -> list[str]:
    """Return the full path to gallery-dl binary."""
    venv_bin = Path(sys.executable).parent / "gallery-dl"
    if venv_bin.exists():
        return [str(venv_bin)]
    gd = shutil.which("gallery-dl")
    if gd:
        return [gd]
    raise RuntimeError("gallery-dl not found. Install with: pip install gallery-dl")


def _cookie_args(site: str) -> list[str]:
    """Return --cookies arg if cookie file exists, else []."""
    cf = COOKIE_FILES.get(site)
    if cf and cf.exists():
        return ["--cookies", str(cf)]
    return []


async def _run_gallery_dl(url: str, job_dir: Path, extra_args: list[str]) -> tuple[bool, str]:
    """Run gallery-dl subprocess with timeout. Returns (success, error_msg)."""
    try:
        cmd = _gallery_dl_cmd() + ["-d", str(job_dir)] + extra_args + [url]
    except RuntimeError as e:
        return False, str(e)

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=GALLERY_DL_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return False, f"gallery-dl timeout ({GALLERY_DL_TIMEOUT}s)"
    except asyncio.CancelledError:
        process.kill()  # /cancel or shutdown: don't leave an orphan gallery-dl running
        raise

    if process.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
        return False, err
    return True, ""


@dataclass
class Post:
    """The media files of one post, and the account that posted it (None if unknown)."""
    username: str | None
    files: list[str]


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _drop_audio_when_mixed(files: list[str]) -> list[str]:
    """A post with pictures/videos plus an audio track (TikTok slideshows) keeps only the pictures/videos."""
    audio = [f for f in files if _ext(f) in AUDIO_EXTS]
    if audio and len(audio) < len(files):
        return [f for f in files if f not in audio]
    return files


def collect_posts(job_dir: Path) -> list[Post]:
    """Collect downloaded media, one Post per post directory.

    Expected layout (see SITE_ARGS): <job_dir>/<category>/<username>/<post id>/<files>. Shallower layouts
    are tolerated: <category>/<username>/<files> is one post, anything else has no known username.
    Posts are returned oldest first (post ids grow with time); files within a post in name order
    (001, 002, ...), which is the order of the post.
    """
    job_dir = Path(job_dir)
    groups: dict[tuple[str | None, str], list[str]] = {}
    for root, _, names in os.walk(job_dir):
        for name in names:
            path = os.path.join(root, name)
            if _ext(name) not in MEDIA_EXTS or os.path.getsize(path) <= MIN_FILE_SIZE:
                continue
            parts = Path(path).relative_to(job_dir).parts
            if len(parts) >= 4:
                key = (parts[1], parts[2])
            elif len(parts) == 3:
                key = (parts[1], "")
            else:
                key = (None, "")
            groups.setdefault(key, []).append(path)

    posts = []
    for (username, post_dir), files in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", len(kv[0][1]), kv[0][1])):
        files = _drop_audio_when_mixed(sorted(files, key=lambda f: (os.path.basename(f), f)))
        if files:
            posts.append(Post(username, files))
    return posts


def album_batches(files: list[str]) -> list[list[str]]:
    """Split one post's files into batches that Telegram accepts as albums (max 10 each).

    Photos and videos share albums; other file types are albumed separately, because mixing
    them with photos makes Telegram reject the whole album.
    """
    visual = [f for f in files if _ext(f) in PHOTO_EXTS | VIDEO_EXTS]
    rest = [f for f in files if _ext(f) not in PHOTO_EXTS | VIDEO_EXTS]
    return [group[i:i + ALBUM_LIMIT] for group in (visual, rest) for i in range(0, len(group), ALBUM_LIMIT)]


async def upload_to_group(posts: list[Post], thread_id: int, site_name: str, status_cb: StatusCb, cleanup_dir: Path | None = None) -> bool:
    """Upload downloaded posts to the Telegram group, each post as its own album(s).

    Returns True only if at least one file was actually sent.
    """
    total_files = sum(len(post.files) for post in posts)
    sent_files = 0
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    try:
        for post in posts:
            for batch in album_batches(post.files):
                try:
                    if len(batch) == 1:
                        await retry_flood(lambda: user_client.send_file(dest_entity, file=batch[0], reply_to=thread_id), "upload")
                    else:
                        await retry_flood(lambda: user_client.send_file(dest_entity, file=batch, reply_to=thread_id), "upload album")
                    sent_files += len(batch)
                except Exception:
                    # Fallback: send one by one
                    for p in batch:
                        try:
                            await retry_flood(lambda p=p: user_client.send_file(dest_entity, file=p, reply_to=thread_id), "upload")
                            sent_files += 1
                        except Exception as e:
                            log.warning(f"{site_name}: skip file {p}: {e}")
            await asyncio.sleep(0.5)  # small delay between posts
    except Exception as e:
        log.error(f"{site_name} upload error: {e}")
        await status_cb(f"❌ Failed to upload files from {site_name}: `{e}`")
        return False
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)

    if sent_files == 0:
        await status_cb(f"❌ Failed to upload any file from {site_name}")
        return False
    await status_cb(f"✅ Files from {site_name} sent successfully!\n• Topic: **{site_name} Downloads**\n• Count: {sent_files}/{total_files} files")
    return True
