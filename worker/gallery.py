import asyncio
import os
import shutil
import sys
import time
import logging
from pathlib import Path
from typing import Callable, Awaitable

from worker.utils import retry_flood
from config import DOWNLOAD_DIR, DEST_GROUP_ID, user_client

log = logging.getLogger("clone-bot.worker.gallery")
StatusCb = Callable[[str], Awaitable[None]]

GALLERY_DL_TIMEOUT = 300  # seconds

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


async def _run_gallery_dl(url: str, job_dir: Path, cookies: list[str]) -> tuple[bool, str]:
    """Run gallery-dl subprocess with timeout. Returns (success, error_msg)."""
    try:
        cmd = _gallery_dl_cmd() + ["-d", str(job_dir)] + cookies + [url]
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


def collect_files(job_dir: Path) -> list[str]:
    """Collect media file paths from a directory (skip empty/non-media files)."""
    media_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mkv', '.mov', '.webm', '.mp3', '.m4a', '.ogg', '.webp', '.avif', '.heic'}
    paths = []
    for root, _, files in os.walk(job_dir):
        for file in files:
            p = os.path.join(root, file)
            ext = os.path.splitext(file)[1].lower()
            if ext in media_exts and os.path.getsize(p) > 1024:
                paths.append(p)
    return paths


def collect_files_grouped(job_dir: Path) -> list[list[str]]:
    """Collect media files grouped by parent directory (per-post grouping).
    
    gallery-dl creates a subdirectory per post, so files in the same
    subdirectory belong to the same post. Returns a list of groups,
    where each group is a list of file paths for one post.
    """
    media_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mkv', '.mov', '.webm', '.mp3', '.m4a', '.ogg', '.avif', '.heic'}
    groups: dict[str, list[str]] = {}
    for root, _, files in os.walk(job_dir):
        for file in sorted(files):  # sort to preserve 001, 002, ... order
            p = os.path.join(root, file)
            ext = os.path.splitext(file)[1].lower()
            if ext in media_exts and os.path.getsize(p) > 1024:
                groups.setdefault(root, []).append(p)
    return list(groups.values())


async def upload_to_group(paths: list[list[str]], thread_id: int, site_name: str, status_cb: StatusCb, cleanup_dir: Path | None = None) -> bool:
    """Upload downloaded files to the Telegram group, with per-file error handling.

    Returns True only if at least one file was actually sent.
    """
    total_files = sum(len(g) for g in paths)
    sent_files = 0
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    try:
        for group in paths:
            # Send each group (post) as a separate album
            for i in range(0, len(group), 10):
                batch = group[i:i+10]
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
