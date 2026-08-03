"""
worker.py — clone logic ทั้งหมด

- is_supported_media()   ตรวจว่า message มี media ที่ต้องการ
- get_or_create_topic()  หา/สร้าง forum topic ใน DEST_GROUP
- clone_group()          worker หลัก: collect → batch → download → upload
"""

import asyncio
import logging
import shutil
from pathlib import Path

from telethon.errors import FloodWaitError, ChatAdminRequiredError
from telethon.errors.rpcbaseerrors import FloodError
try:
    from telethon.errors import FloodPremiumWaitError
except ImportError:
    FloodPremiumWaitError = FloodError

import re


def _flood_seconds(e: Exception, default: int = 5) -> int:
    if hasattr(e, "seconds"):
        return e.seconds
    m = re.search(r"(\d+)\s*seconds?", str(e))
    return int(m.group(1)) if m else default


try:
    from telethon.tl.functions.channels import CreateForumTopicRequest, GetForumTopicsRequest
except ImportError:
    from telethon.tl.functions.messages import CreateForumTopicRequest, GetForumTopicsRequest
from telethon.tl.types import User as TLUser

import database
from config import (
    DEST_GROUP_ID,
    DOWNLOAD_DIR,
    SEND_DELAY,
    log,
    user_client,
)

# ─── Media filter ──────────────────────────────────────────────────────────────
_MIME_PREFIXES = ("image/", "video/", "audio/")
_MIME_EXACT    = {"application/ogg"}


def is_supported_media(msg) -> bool:
    """คืน True ถ้า message มี media ที่ต้องการ clone"""
    if msg.photo:
        return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if any(mime.startswith(p) for p in _MIME_PREFIXES):
            return True
        if mime in _MIME_EXACT:
            return True
        return True  # Document ทั่วไป (PDF, ZIP, ฯลฯ)
    return False


# ─── Topic helper ──────────────────────────────────────────────────────────────
async def get_or_create_topic(group_title: str) -> int:
    """
    ค้นหา forum topic ใน DEST_GROUP ที่ชื่อตรงกับ group_title
    ถ้าไม่มีให้สร้างใหม่ คืน thread_id
    """
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)

    result = await user_client(GetForumTopicsRequest(
        channel=dest_entity,
        offset_date=0,
        offset_id=0,
        offset_topic=0,
        limit=100,
        q=group_title,
    ))

    for topic in result.topics:
        if topic.title.lower() == group_title.lower():
            log.info(f"พบ topic: '{topic.title}' (id={topic.id})")
            return topic.id

    log.info(f"ไม่พบ topic '{group_title}' — กำลังสร้างใหม่...")
    created = await user_client(CreateForumTopicRequest(
        channel=dest_entity,
        title=group_title,
    ))
    thread_id = created.updates[0].id
    log.info(f"สร้าง topic สำเร็จ (thread_id={thread_id})")
    return thread_id


# ─── Album grouping ────────────────────────────────────────────────────────────
def _group_into_batches(messages: list) -> list[list]:
    """
    จัดกลุ่ม messages เป็น batches โดยใช้ grouped_id
    - messages ที่มี grouped_id เดียวกัน → batch เดียว (album)
    - messages ที่ไม่มี grouped_id → batch ละ 1 ไฟล์
    """
    batches: list[list] = []
    album_buf: dict[int, list] = {}
    seen_albums: list[int] = []

    for msg in messages:
        gid = msg.grouped_id
        if gid:
            if gid not in album_buf:
                album_buf[gid] = []
                seen_albums.append(gid)
            album_buf[gid].append(msg)
        else:
            for aid in seen_albums:
                batches.append(album_buf.pop(aid))
            seen_albums.clear()
            batches.append([msg])

    for aid in seen_albums:
        batches.append(album_buf.pop(aid))

    return batches


# ─── Main worker ───────────────────────────────────────────────────────────────
async def clone_group(source: str | int, status_cb) -> None:
    """
    ดึง media จาก source แล้วส่งไปยัง DEST_GROUP_ID/topic

    source   : group_id (int), @username, ชื่อกลุ่ม, หรือ user id/username
    status_cb: async callable(text) สำหรับส่ง progress กลับไปยัง chat
    """
    # ── 1. Resolve entity ──────────────────────────────────────────────────────
    try:
        source_entity = await user_client.get_entity(source)
    except Exception as e:
        await status_cb(f"❌ ไม่พบ `{source}`\n`{e}`")
        return

    if isinstance(source_entity, TLUser):
        parts = [source_entity.first_name or "", source_entity.last_name or ""]
        display_name = " ".join(p for p in parts if p).strip() or str(source_entity.id)
        entity_type  = "user"
    else:
        display_name = getattr(source_entity, "title", str(source))
        entity_type  = "group/channel"

    src_id = source_entity.id
    await status_cb(f"🔍 พบ {entity_type}: **{display_name}**\nกำลังนับ media...")

    # ── 2. หา/สร้าง topic ปลายทาง ─────────────────────────────────────────────
    try:
        thread_id = await get_or_create_topic(display_name)
    except ChatAdminRequiredError:
        await status_cb("❌ บอทต้องเป็น admin ใน destination group และเปิด Topics ด้วย")
        return
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ล้มเหลว: `{e}`")
        return

    # ── 3. Collect messages ที่ยังไม่เคยส่ง ────────────────────────────────────
    already_sent = database.get_sent(src_id)

    raw_messages = []
    async for msg in user_client.iter_messages(source_entity, reverse=True):
        if is_supported_media(msg) and msg.id not in already_sent:
            raw_messages.append(msg)

    batches       = _group_into_batches(raw_messages)
    total_files   = len(raw_messages)
    total_batches = len(batches)
    skipped       = len(already_sent)

    if total_files == 0:
        skip_note = f" (ข้ามไปแล้ว {skipped} ไฟล์)" if skipped else ""
        await status_cb(f"ℹ️ ไม่มี media ใหม่ที่ต้องส่ง{skip_note}")
        return

    skip_note = f" | ข้ามที่ส่งแล้ว {skipped} ไฟล์" if skipped else ""
    await status_cb(
        f"📦 พบ media ใหม่ **{total_files}** ไฟล์ ({total_batches} กลุ่ม){skip_note}\n"
        f"📤 กำลัง clone ไปยัง topic **{display_name}**..."
    )

    # ── 4. Download → Upload ──────────────────────────────────────────────────
    job_dir = DOWNLOAD_DIR / str(src_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)

    success, failed, sent_files = 0, 0, 0

    for b_idx, batch in enumerate(batches, 1):
        paths: list[str] = []
        try:
            # Download
            for msg in batch:
                p = await user_client.download_media(msg, file=str(job_dir) + "/")
                if not p:
                    raise ValueError(f"download_media returned None (msg_id={msg.id})")
                paths.append(p)

            caption = batch[-1].text or ""

            if len(paths) == 1:
                await user_client.send_file(
                    dest_entity,
                    file=paths[0],
                    caption=caption,
                    reply_to=thread_id,
                    parse_mode="md",
                )
            else:
                captions = [caption] + [""] * (len(paths) - 1)
                await user_client.send_file(
                    dest_entity,
                    file=paths,
                    caption=captions,
                    reply_to=thread_id,
                    parse_mode="md",
                )
                log.info(f"[batch {b_idx}] album {len(paths)} ไฟล์ ✓")

            success    += len(batch)
            sent_files += len(batch)
            database.mark_sent(src_id, {m.id for m in batch})

        except (FloodWaitError, FloodPremiumWaitError) as e:
            wait_s = _flood_seconds(e)
            log.warning(f"FloodWait {wait_s}s — รอ...")
            await asyncio.sleep(wait_s + 2)
            failed += len(batch)

        except asyncio.CancelledError:
            raise  # ส่งต่อให้ handler ด้านนอกจัดการ

        except Exception as e:
            log.error(f"[batch {b_idx}] ✗ {e}")
            failed += len(batch)

        finally:
            for p in paths:
                Path(p).unlink(missing_ok=True)

        if b_idx % 10 == 0 or b_idx == total_batches:
            await status_cb(
                f"⏳ Progress: {sent_files + failed}/{total_files} ไฟล์ "
                f"| {b_idx}/{total_batches} กลุ่ม "
                f"(✓{success} ✗{failed})"
            )

        await asyncio.sleep(SEND_DELAY)

    # ── 5. Cleanup ────────────────────────────────────────────────────────────
    shutil.rmtree(job_dir, ignore_errors=True)

    await status_cb(
        f"✅ Clone เสร็จสิ้น!\n"
        f"• {entity_type.capitalize()}: **{display_name}**\n"
        f"• Topic: `{thread_id}`\n"
        f"• สำเร็จ: {success}/{total_files} ไฟล์\n"
        f"• ล้มเหลว: {failed}/{total_files} ไฟล์\n"
        f"• Album ที่ส่งพร้อมกัน: {sum(1 for b in batches if len(b) > 1)} กลุ่ม"
    )