import shutil
import time
import logging
from pathlib import Path
from typing import Callable, Awaitable

from config import DOWNLOAD_DIR, user_client

from worker.gallery import _cookie_args, _run_gallery_dl, collect_files_grouped, upload_to_group
from worker.topics import get_or_create_topic

log = logging.getLogger("clone-bot.worker.media_sites")
StatusCb = Callable[[str], Awaitable[None]]


async def clone_x_media(url: str, status_cb: StatusCb) -> None:
    await status_cb(f"🔍 X link detected\n📥 Downloading...")
    
    job_id = url.split('/')[-1].split('?')[0]
    job_dir = DOWNLOAD_DIR / f"x_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    cookies = _cookie_args("x")
    ok, err = await _run_gallery_dl(url, job_dir, cookies)
    
    if not ok:
        hint = f"\n\n💡 If the account is private, place a `cookies_x.txt` file in the bot's main folder" if not cookies else ""
        await status_cb(f"❌ X download failed:\n`{err}`{hint}")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    paths = collect_files_grouped(job_dir)
    
    if not paths:
        await status_cb(f"⚠️ No images/videos found, or you don't have access (Cookies required)")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    await status_cb(f"✅ Downloaded {len(paths)} files\n📤 Uploading to group...")
    topic_name = "X Downloads"
    try:
        thread_id = await get_or_create_topic(topic_name)
    except Exception as e:
        await status_cb(f"❌ Failed to create/find topic: `{e}`")
        return
    await upload_to_group(paths, thread_id, "X", status_cb, cleanup_dir=job_dir)


async def clone_ig_media(url: str, status_cb: StatusCb) -> None:
    await status_cb(f"🔍 Instagram link detected\n📥 Downloading...")
    
    job_id = str(int(time.time()))
    job_dir = DOWNLOAD_DIR / f"ig_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    cookies = _cookie_args("ig")
    if not cookies:
        await status_cb(f"❌ Download failed: `cookies_ig.txt` not found\n💡 Cookies are required for Instagram")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
    
    ok, err = await _run_gallery_dl(url, job_dir, cookies)
    if not ok:
        await status_cb(f"❌ Instagram download failed:\n`{err}`\n\n💡 Cookies may have expired. Please update your `cookies_ig.txt` file")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    paths = collect_files_grouped(job_dir)
    
    if not paths:
        await status_cb(f"⚠️ No images/videos found (post may have been deleted)")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    await status_cb(f"✅ Downloaded {len(paths)} files\n📤 Uploading to group...")
    topic_name = "Instagram Downloads"
    try:
        thread_id = await get_or_create_topic(topic_name)
    except Exception as e:
        await status_cb(f"❌ Failed to create/find topic: `{e}`")
        return
    await upload_to_group(paths, thread_id, "IG", status_cb, cleanup_dir=job_dir)


async def clone_tiktok_media(url: str, status_cb: StatusCb) -> None:
    await status_cb(f"🔍 TikTok link detected\n📥 Downloading...")
    
    job_id = str(int(time.time()))
    job_dir = DOWNLOAD_DIR / f"tiktok_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    cookies = _cookie_args("tiktok")
    ok, err = await _run_gallery_dl(url, job_dir, cookies)
    
    if not ok:
        hint = f"\n\n💡 If blocked, place a `cookies_tiktok.txt` file in the bot folder" if not cookies else ""
        await status_cb(f"❌ TikTok download failed:\n`{err}`{hint}")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    paths = collect_files_grouped(job_dir)
    
    if not paths:
        await status_cb(f"⚠️ No videos found (post may have been deleted or set to private)")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    await status_cb(f"✅ Downloaded {len(paths)} files\n📤 Uploading to group...")
    topic_name = "TikTok Downloads"
    try:
        thread_id = await get_or_create_topic(topic_name)
    except Exception as e:
        await status_cb(f"❌ Failed to create/find topic: `{e}`")
        return
    await upload_to_group(paths, thread_id, "TikTok", status_cb, cleanup_dir=job_dir)
