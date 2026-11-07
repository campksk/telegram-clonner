import asyncio
import hashlib
import logging
import shutil
import re
import os
import sys
import time
from pathlib import Path
from typing import Callable, Awaitable

from telethon.errors import FloodWaitError, ChatAdminRequiredError
from telethon.errors.rpcbaseerrors import FloodError
try:
    from telethon.errors import FloodPremiumWaitError
except ImportError:
    FloodPremiumWaitError = FloodError
from telethon.tl.functions.messages import CreateForumTopicRequest, GetForumTopicsRequest
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.types import User as TLUser

import db
from config import DEST_GROUP_ID, DOWNLOAD_DIR, SEND_DELAY, PARALLEL_WORKERS, MAX_SCAN_MESSAGES, user_client

log = logging.getLogger("clone-bot.worker")
StatusCb = Callable[[str], Awaitable[None]]

GALLERY_DL_TIMEOUT = 300  # seconds


def _gallery_dl_cmd() -> list[str]:
    """Return the full path to gallery-dl binary."""
    venv_bin = Path(sys.executable).parent / "gallery-dl"
    if venv_bin.exists():
        return [str(venv_bin)]
    gd = shutil.which("gallery-dl")
    if gd:
        return [gd]
    raise RuntimeError("gallery-dl not found. Install with: pip install gallery-dl")


COOKIE_FILES = {
    "x": Path("cookies_x.txt"),
    "ig": Path("cookies_ig.txt"),
    "tiktok": Path("cookies_tiktok.txt"),
}


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

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=GALLERY_DL_TIMEOUT
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return False, f"gallery-dl timeout ({GALLERY_DL_TIMEOUT}s)"

    if process.returncode != 0:
        err = stderr.decode('utf-8').strip() or stdout.decode('utf-8').strip()
        return False, err
    return True, ""


STICKER_MIMES = {"image/webp", "application/x-tgsticker"}

def _flood_seconds(e: Exception, default: int = 5) -> int:
    if hasattr(e, "seconds"): return e.seconds
    m = re.search(r"(\d+)\s*seconds?", str(e))
    return int(m.group(1)) if m else default

def is_supported_media(msg) -> bool:
    if msg.sticker: return False
    if msg.photo: return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime in STICKER_MIMES: return False
        return True
    return False

def is_public_entity(entity) -> bool:
    if isinstance(entity, TLUser): return False
    return not bool(getattr(entity, "noforwards", False))

async def get_or_create_topic(group_title: str) -> int:
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    result = await user_client(GetForumTopicsRequest(
        peer=dest_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100, q=group_title
    ))
    for topic in result.topics:
        if topic.title.lower() == group_title.lower():
            log.info(f"พบ topic: '{topic.title}' (id={topic.id})")
            return topic.id

    log.info(f"สร้าง topic ใหม่ '{group_title}'...")
    created = await user_client(CreateForumTopicRequest(peer=dest_entity, title=group_title))
    return created.updates[0].id

async def _forward_to_topic(from_peer, dest, msg_ids: list[int], thread_id: int) -> None:
    await user_client(ForwardMessagesRequest(
        from_peer=from_peer, id=msg_ids, to_peer=dest, top_msg_id=thread_id,
        random_id=[__import__('random').randint(0, 2**63) for _ in msg_ids],
        silent=False, drop_author=False
    ))

async def clone_group(source: str | int, status_cb: StatusCb) -> None:
    try:
        source_entity = await user_client.get_entity(source)
    except ValueError:
        await status_cb(f"🔄 กำลังอัปเดตข้อมูลแชทเพื่อค้นหา {source} ...")
        await user_client.get_dialogs()
        try:
            source_entity = await user_client.get_entity(source)
        except Exception as e:
            await status_cb(f"❌ ไม่พบ `{source}` (บัญชี User ต้องอยู่ในกลุ่ม/ช่องนี้ด้วย)\n`{e}`")
            return
    except Exception as e:
        await status_cb(f"❌ ไม่พบ `{source}`\n`{e}`")
        return

    if isinstance(source_entity, TLUser):
        parts = [source_entity.first_name or "", source_entity.last_name or ""]
        title = " ".join(p for p in parts if p).strip() or str(source_entity.id)
        entity_type = "user"
    else:
        title = getattr(source_entity, "title", str(source))
        entity_type = "group/channel"

    src_key = str(source_entity.id)
    await status_cb(f"🔍 พบ {entity_type}: **{title}**\nกำลังนับ media...")

    try:
        thread_id = await get_or_create_topic(title)
    except ChatAdminRequiredError:
        await status_cb("❌ บอทต้องเป็น admin ใน destination group และเปิด Topics ด้วย")
        return
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ล้มเหลว: `{e}`")
        return

    already_sent = db.get_sent(src_key)
    raw_messages = []
    scan_count = 0
    async for msg in user_client.iter_messages(source_entity, reverse=True):
        scan_count += 1
        if scan_count > MAX_SCAN_MESSAGES:
            log.warning(f"clone_group: สแกนถึงขีดจำกัด {MAX_SCAN_MESSAGES} messages")
            break
        if is_supported_media(msg) and msg.id not in already_sent:
            raw_messages.append(msg)

    batches, album_buf, seen_albums = [], {}, []
    for msg in raw_messages:
        gid = msg.grouped_id
        if gid:
            if gid not in album_buf:
                album_buf[gid] = []
                seen_albums.append(gid)
            album_buf[gid].append(msg)
        else:
            for aid in seen_albums: batches.append(album_buf.pop(aid))
            seen_albums.clear()
            batches.append([msg])
    for aid in seen_albums: batches.append(album_buf.pop(aid))

    total_files, total_batches, skipped = len(raw_messages), len(batches), len(already_sent)
    if total_files == 0:
        skip_note = f" (ข้ามไปแล้ว {skipped} ไฟล์)" if skipped else ""
        await status_cb(f"ℹ️ ไม่มี media ใหม่ที่ต้องส่ง{skip_note}")
        return

    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    use_forward = is_public_entity(source_entity)
    mode_label = "⚡ forward" if use_forward else f"📥 download→upload (workers={PARALLEL_WORKERS})"
    skip_note = f" | ข้ามที่ส่งแล้ว {skipped} ไฟล์" if skipped else ""
    
    await status_cb(
        f"📦 พบ media ใหม่ **{total_files}** ไฟล์ ({total_batches} กลุ่ม){skip_note}\n"
        f"📤 mode: {mode_label} → topic **{title}**..."
    )

    sem = asyncio.Semaphore(PARALLEL_WORKERS)
    counters = {"success": 0, "failed": 0, "done": 0}
    lock = asyncio.Lock()
    progress_lock = asyncio.Lock()

    async def _update_progress():
        done_now = counters["done"]
        if done_now % 10 == 0 or done_now == total_batches:
            async with progress_lock:
                await status_cb(
                    f"⏳ Progress: {counters['success'] + counters['failed']}/{total_files} ไฟล์ "
                    f"| {done_now}/{total_batches} กลุ่ม "
                    f"(✓{counters['success']} ✗{counters['failed']})"
                )

    job_dir = DOWNLOAD_DIR / src_key
    if not use_forward: job_dir.mkdir(parents=True, exist_ok=True)

    async def process_batch(b_idx, batch):
        if use_forward:
            async with sem:
                try:
                    msg_ids = [m.id for m in batch]
                    await _forward_to_topic(source_entity, dest_entity, msg_ids, thread_id)
                    async with lock:
                        counters["success"] += len(batch)
                        db.mark_sent_sync(src_key, set(msg_ids))
                except (FloodWaitError, FloodPremiumWaitError) as e:
                    wait_s = _flood_seconds(e)
                    await asyncio.sleep(wait_s + 2)
                    async with lock: counters["failed"] += len(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error(f"[fwd {b_idx}] ✗ {e}")
                    async with lock: counters["failed"] += len(batch)
                finally:
                    async with lock: counters["done"] += 1
                    await _update_progress()
                    await asyncio.sleep(SEND_DELAY / PARALLEL_WORKERS)
        else:
            paths = []
            async with sem:
                try:
                    for msg in batch:
                        for attempt in range(3):
                            try:
                                p = await user_client.download_media(msg, file=str(job_dir) + "/")
                                if p: paths.append(p)
                                break
                            except Exception as e:
                                if attempt == 2:
                                    log.warning(f"[up {b_idx}] skip msg {msg.id}: {e}")
                                else:
                                    await asyncio.sleep(2 * (attempt + 1))
                    if not paths:
                        return
                    caption = batch[-1].text or ""
                    if len(paths) == 1:
                        await user_client.send_file(dest_entity, file=paths[0], caption=caption, reply_to=thread_id, parse_mode="md")
                    elif len(paths) > 1:
                        captions = [caption] + [""] * (len(paths) - 1)
                        await user_client.send_file(dest_entity, file=paths, caption=captions, reply_to=thread_id, parse_mode="md")
                    async with lock:
                        counters["success"] += len(batch)
                        db.mark_sent_sync(src_key, {m.id for m in batch})
                except (FloodWaitError, FloodPremiumWaitError) as e:
                    wait_s = _flood_seconds(e)
                    await asyncio.sleep(wait_s + 2)
                    async with lock: counters["failed"] += len(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error(f"[up {b_idx}] ✗ {e}")
                    async with lock: counters["failed"] += len(batch)
                finally:
                    for p in paths: Path(p).unlink(missing_ok=True)
                    async with lock: counters["done"] += 1
                    await _update_progress()
                    await asyncio.sleep(SEND_DELAY / PARALLEL_WORKERS)

    await asyncio.gather(*[process_batch(i, b) for i, b in enumerate(batches, 1)])
    
    # persist all sent IDs in one write
    await db.save_sent()

    if not use_forward: shutil.rmtree(job_dir, ignore_errors=True)
    
    await status_cb(
        f"✅ Clone เสร็จสิ้น!\n• Source: **{title}**\n• Mode: {mode_label}\n• Topic: `{thread_id}`\n"
        f"• สำเร็จ: {counters['success']}/{total_files} ไฟล์\n• ล้มเหลว: {counters['failed']}/{total_files} ไฟล์"
    )

async def clone_single_message(chat: str | int, msg_id: int, status_cb: StatusCb) -> None:
    try:
        chat_entity = await user_client.get_entity(chat)
    except ValueError:
        await status_cb(f"🔄 กำลังอัปเดตข้อมูลแชทเพื่อค้นหา ID {chat} ...")
        await user_client.get_dialogs()
        try:
            chat_entity = await user_client.get_entity(chat)
        except Exception as e:
            await status_cb(f"❌ ไม่พบ chat `{chat}` (บัญชี User ต้องอยู่ในกลุ่ม/ช่องนี้ด้วย)\n`{e}`")
            return
    except Exception as e:
        await status_cb(f"❌ ไม่พบ chat `{chat}`\n`{e}`")
        return

    if isinstance(chat_entity, TLUser):
        parts = [chat_entity.first_name or "", chat_entity.last_name or ""]
        chat_title = " ".join(p for p in parts if p).strip() or str(chat_entity.id)
    else:
        chat_title = getattr(chat_entity, "title", str(chat))

    try:
        thread_id = await get_or_create_topic(chat_title)
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ล้มเหลว: `{e}`")
        return

    msgs = await user_client.get_messages(chat_entity, ids=msg_id)
    if not msgs:
        await status_cb(f"❌ ไม่พบ message id `{msg_id}` ใน `{chat_title}`")
        return
    msg = msgs if not isinstance(msgs, list) else msgs[0]
    if not msg:
        await status_cb(f"❌ ไม่พบ message id `{msg_id}`")
        return

    batch = []
    if msg.grouped_id:
        async for m in user_client.iter_messages(chat_entity, min_id=msg_id - 20, max_id=msg_id + 20):
            if m.grouped_id == msg.grouped_id and is_supported_media(m):
                batch.append(m)
        batch.sort(key=lambda m: m.id)
    elif is_supported_media(msg):
        batch = [msg]
    else:
        await status_cb(f"⚠️ Message `{msg_id}` ไม่มี media ที่รองรับ")
        return

    await status_cb(f"🔍 พบ: **{chat_title}** — message `{msg_id}`\n📤 กำลังส่ง → topic **{chat_title}**...")
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    use_forward = is_public_entity(chat_entity)
    
    if use_forward:
        try:
            await _forward_to_topic(chat_entity, dest_entity, [m.id for m in batch], thread_id)
            actual_mode = "⚡ forward"
        except Exception:
            use_forward = False

    if not use_forward:
        job_dir = DOWNLOAD_DIR / f"single_{chat_entity.id}_{msg_id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        try:
            for m in batch:
                p = await user_client.download_media(m, file=str(job_dir) + "/")
                if p: paths.append(p)
            caption = batch[-1].text or ""
            if len(paths) == 1:
                await user_client.send_file(dest_entity, file=paths[0], caption=caption, reply_to=thread_id, parse_mode="md")
            elif len(paths) > 1:
                captions = [caption] + [""] * (len(paths) - 1)
                await user_client.send_file(dest_entity, file=paths, caption=captions, reply_to=thread_id, parse_mode="md")
            actual_mode = "📥 upload"
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    await status_cb(f"✅ ส่งสำเร็จ!\n• Chat: **{chat_title}**\n• ไฟล์: {len(batch)}\n• Mode: {actual_mode}")


def _collect_files(job_dir: Path) -> list[str]:
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


async def _upload_to_group(paths: list[str], thread_id: int, site_name: str, status_cb: StatusCb, cleanup_dir: Path | None = None) -> None:
    """Upload downloaded files to Telegram group, with per-file error handling."""
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    try:
        # Try batch first, fallback to individual on failure
        for i in range(0, len(paths), 10):
            batch_paths = paths[i:i+10]
            try:
                if len(batch_paths) == 1:
                    await user_client.send_file(dest_entity, file=batch_paths[0], reply_to=thread_id)
                else:
                    await user_client.send_file(dest_entity, file=batch_paths, reply_to=thread_id)
            except Exception:
                # Fallback: send one by one
                for p in batch_paths:
                    try:
                        await user_client.send_file(dest_entity, file=p, reply_to=thread_id)
                    except Exception as e:
                        log.warning(f"{site_name}: skip file {p}: {e}")
        await status_cb(f"✅ ส่งไฟลจาก {site_name} สำเรจ!\n• Topic: **{site_name} Downloads**\n• จำนวน: {len(paths)} ไฟล")
    except Exception as e:
        log.error(f"{site_name} upload error: {e}")
        await status_cb(f"❌ อปโหลดไฟลจาก {site_name} ลมเหลว: `{e}`")
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


async def clone_x_media(url: str, status_cb: StatusCb) -> None:
    await status_cb(f"🔍 พบลิงก X\n📥 กำลงดาวนโหลด...")
    
    job_id = url.split('/')[-1].split('?')[0]
    job_dir = DOWNLOAD_DIR / f"x_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    cookies = _cookie_args("x")
    ok, err = await _run_gallery_dl(url, job_dir, cookies)
    
    if not ok:
        hint = f"\n\n💡 หากบญชต้ Private คณตองใสไฟล `cookies_x.txt` ไวทโฟลเดอรหลักของบอท" if not cookies else ""
        await status_cb(f"❌ ดาวนโหลด X ลมเหลว:\n`{err}`{hint}")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    paths = _collect_files(job_dir)
    
    if not paths:
        await status_cb(f"⚠️ ไมพบรป/วดโอสานี หรอบทไมมสิทธเข้ถึง (ตองใช Cookies)")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    await status_cb(f"✅ โหลดสำเร็จ {len(paths)} ไฟล\n📤 กำลงอปโหลดลงกลุม...")
    topic_name = "X Downloads"
    try:
        thread_id = await get_or_create_topic(topic_name)
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ลมเหลว: `{e}`")
        return
    await _upload_to_group(paths, thread_id, "X", status_cb, cleanup_dir=job_dir)

async def clone_ig_media(url: str, status_cb: StatusCb) -> None:
    await status_cb(f"🔍 พบลิงก Instagram\n📥 กำลงดาวนโหลด...")
    
    job_id = str(int(time.time()))
    job_dir = DOWNLOAD_DIR / f"ig_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    cookies = _cookie_args("ig")
    if not cookies:
        await status_cb(f"❌ ดาวนโหลดลมเหลว: ไมพบไฟล `cookies_ig.txt`\n💡 บังคบทองใช Cookies สหกรบ Instagram")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
    
    ok, err = await _run_gallery_dl(url, job_dir, cookies)
    if not ok:
        await status_cb(f"❌ ดาวนโหลด Instagram ลมเหลว:\n`{err}`\n\n💡 คกก อาจหมดอายุ ใหลองดงไฟล `cookies_ig.txt` ใหมอกครง")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    paths = _collect_files(job_dir)
    
    if not paths:
        await status_cb(f"⚠️ ไมพบรป/วดโ (อาจเปนปสตทถกกลบไปแลว)")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    await status_cb(f"✅ โหลดสำเร็จ {len(paths)} ไฟล\n📤 กำลงอปโหลดลงกลุม...")
    topic_name = "Instagram Downloads"
    try:
        thread_id = await get_or_create_topic(topic_name)
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ลมเหลว: `{e}`")
        return
    await _upload_to_group(paths, thread_id, "IG", status_cb, cleanup_dir=job_dir)

async def clone_tiktok_media(url: str, status_cb: StatusCb) -> None:
    await status_cb(f"🔍 พบลิงก TikTok\n📥 กำลงดาวนโหลด...")
    
    job_id = str(int(time.time()))
    job_dir = DOWNLOAD_DIR / f"tiktok_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    
    cookies = _cookie_args("tiktok")
    ok, err = await _run_gallery_dl(url, job_dir, cookies)
    
    if not ok:
        hint = f"\n\n💡 หากโดนบล็อก ใหใสไฟล `cookies_tiktok.txt` ไวในโฟลเดอรบอท" if not cookies else ""
        await status_cb(f"❌ ดาวนโหลด TikTok ลมเหลว:\n`{err}`{hint}")
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    paths = _collect_files(job_dir)
    
    if not paths:
        await status_cb(f"⚠️ ไมพบวดโ (ปสตอาจถกกลบ หร ตั้งเปนสวนตัว)")
        shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    await status_cb(f"✅ โหลดสำเร็จ {len(paths)} ไฟล\n📤 กำลงอปโหลดลงกลุม...")
    topic_name = "TikTok Downloads"
    try:
        thread_id = await get_or_create_topic(topic_name)
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ลมเหลว: `{e}`")
        return
    await _upload_to_group(paths, thread_id, "TikTok", status_cb, cleanup_dir=job_dir)
