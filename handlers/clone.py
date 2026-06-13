"""
handlers/clone.py — clone command handler และ worker logic

คำสั่ง: clone <source>
  source รองรับ: group_id (-100xxx), @username, ชื่อกลุ่ม, user_id, @user
"""

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Callable, Awaitable

from telethon import events
from telethon.errors import FloodWaitError, ChatAdminRequiredError
from telethon.tl.functions.channels import CreateForumTopicRequest, GetForumTopicsRequest
from telethon.tl.types import User as TLUser

import db
from config import (
    bot_client,
    user_client,
    ALLOWED_USERS,
    DEST_GROUP_ID,
    DOWNLOAD_DIR,
    SEND_DELAY,
    active_tasks,
)

log = logging.getLogger("clone-bot.clone")

StatusCb = Callable[[str], Awaitable[None]]

# ─── Media filter ──────────────────────────────────────────────────────────────
_MIME_PREFIXES = ("image/", "video/", "audio/")
_MIME_EXACT    = {"application/ogg"}


def _is_media(msg) -> bool:
    if msg.photo:
        return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        return (
            any(mime.startswith(p) for p in _MIME_PREFIXES)
            or mime in _MIME_EXACT
            or True   # รับ document ทุกประเภท
        )
    return False


# ─── Topic helper ──────────────────────────────────────────────────────────────
async def _get_or_create_topic(title: str) -> int:
    """หา topic ใน DEST_GROUP ที่ชื่อตรงกับ title ถ้าไม่มีให้สร้างใหม่"""
    dest = await user_client.get_entity(DEST_GROUP_ID)
    result = await user_client(GetForumTopicsRequest(
        channel=dest, offset_date=0, offset_id=0,
        offset_topic=0, limit=100, q=title,
    ))
    for topic in result.topics:
        if topic.title.lower() == title.lower():
            log.info(f"พบ topic '{topic.title}' (id={topic.id})")
            return topic.id

    log.info(f"สร้าง topic ใหม่: '{title}'")
    created = await user_client(CreateForumTopicRequest(channel=dest, title=title))
    thread_id = created.updates[0].id
    log.info(f"สร้าง topic สำเร็จ (thread_id={thread_id})")
    return thread_id


# ─── Album batcher ─────────────────────────────────────────────────────────────
def _build_batches(messages: list) -> list[list]:
    """จัดกลุ่ม messages เป็น batches โดยใช้ grouped_id (album)"""
    batches: list[list] = []
    album_buf: dict[int, list] = {}
    seen: list[int] = []

    for msg in messages:
        gid = msg.grouped_id
        if gid:
            if gid not in album_buf:
                album_buf[gid] = []
                seen.append(gid)
            album_buf[gid].append(msg)
        else:
            for aid in seen:
                batches.append(album_buf.pop(aid))
            seen.clear()
            batches.append([msg])

    for aid in seen:
        batches.append(album_buf.pop(aid))

    return batches


# ─── Clone worker ──────────────────────────────────────────────────────────────
async def clone_group(source: str | int, status_cb: StatusCb) -> None:
    """
    ดึง media จาก source แล้วส่งไปยัง topic ใน DEST_GROUP_ID

    source รองรับ: int id, @username, ชื่อกลุ่ม/user
    """
    # 1. Resolve entity
    try:
        entity = await user_client.get_entity(source)
    except Exception as e:
        await status_cb(f"❌ ไม่พบ `{source}`\n`{e}`")
        return

    if isinstance(entity, TLUser):
        parts = [entity.first_name or "", entity.last_name or ""]
        title = " ".join(p for p in parts if p).strip() or str(entity.id)
        entity_type = "user"
    else:
        title = getattr(entity, "title", str(source))
        entity_type = "group/channel"

    src_key = str(entity.id)
    await status_cb(f"🔍 พบ {entity_type}: **{title}**\nกำลังนับ media...")

    # 2. หา/สร้าง topic ปลายทาง
    try:
        thread_id = await _get_or_create_topic(title)
    except ChatAdminRequiredError:
        await status_cb("❌ บอทต้องเป็น admin ใน destination group และเปิด Topics ด้วย")
        return
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ล้มเหลว: `{e}`")
        return

    # 3. Collect + filter already-sent
    already_sent = db.get_sent(src_key)
    raw: list = []
    async for msg in user_client.iter_messages(entity, reverse=True):
        if _is_media(msg) and msg.id not in already_sent:
            raw.append(msg)

    batches = _build_batches(raw)
    total_files   = len(raw)
    total_batches = len(batches)
    skipped       = len(already_sent)

    if total_files == 0:
        skip_note = f" (ข้ามไปแล้ว {skipped} ไฟล์)" if skipped else ""
        await status_cb(f"ℹ️ ไม่มี media ใหม่ที่ต้องส่ง{skip_note}")
        return

    skip_note = f" | ข้ามที่ส่งแล้ว {skipped} ไฟล์" if skipped else ""
    await status_cb(
        f"📦 พบ media ใหม่ **{total_files}** ไฟล์ ({total_batches} กลุ่ม){skip_note}\n"
        f"📤 กำลัง clone ไปยัง topic **{title}**..."
    )

    # 4. Download → Upload ทีละ batch
    job_dir = DOWNLOAD_DIR / src_key
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = await user_client.get_entity(DEST_GROUP_ID)

    success, failed, sent_files = 0, 0, 0
    newly_sent: set[int] = set()

    for b_idx, batch in enumerate(batches, 1):
        paths: list[str] = []
        try:
            for msg in batch:
                p = await user_client.download_media(msg, file=str(job_dir) + "/")
                if not p:
                    raise ValueError(f"download_media คืน None (msg_id={msg.id})")
                paths.append(p)

            caption = batch[-1].text or ""

            if len(paths) == 1:
                await user_client.send_file(
                    dest, file=paths[0], caption=caption,
                    reply_to=thread_id, parse_mode="md",
                )
            else:
                captions = [caption] + [""] * (len(paths) - 1)
                await user_client.send_file(
                    dest, file=paths, caption=captions,
                    reply_to=thread_id, parse_mode="md",
                )
                log.info(f"[batch {b_idx}] album {len(paths)} ไฟล์ ✓")

            success += len(batch)
            sent_files += len(batch)
            newly_sent.update(m.id for m in batch)
            db.mark_sent(src_key, newly_sent)
            newly_sent.clear()

        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s — รอ...")
            await asyncio.sleep(e.seconds + 2)
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

    # 5. Cleanup
    shutil.rmtree(job_dir, ignore_errors=True)

    await status_cb(
        f"✅ Clone เสร็จสิ้น!\n"
        f"• Source: **{title}**\n"
        f"• Topic: `{thread_id}`\n"
        f"• สำเร็จ: {success}/{total_files} ไฟล์\n"
        f"• ล้มเหลว: {failed}/{total_files} ไฟล์\n"
        f"• Album ที่ส่งพร้อมกัน: {sum(1 for b in batches if len(b) > 1)} กลุ่ม"
    )


# ─── Command handler ───────────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^clone(?:\s+(.*))?$"))
async def handle_clone(event: events.NewMessage.Event):
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        await event.reply("⛔ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    arg = (event.pattern_match.group(1) or "").strip()

    if not arg:
        await event.reply(
            "⚠️ ระบุ source ด้วย\n\n"
            "รองรับ:\n"
            "• `clone -1001234567890` — group/channel ID\n"
            "• `clone @username` — username ของ group หรือ user\n"
            "• `clone ชื่อกลุ่ม` — ค้นหาจากชื่อ (ต้องอยู่ใน group นั้นแล้ว)",
            parse_mode="md",
        )
        return

    source: str | int = int(arg) if re.fullmatch(r"-?\d+", arg) else arg

    task_key = str(arg)
    if task_key in active_tasks and not active_tasks[task_key].done():
        await event.reply(f"⚠️ `{arg}` กำลัง clone อยู่แล้ว พิมพ์ `cancel` เพื่อหยุด")
        return

    status_msg = await event.reply(f"🚀 กำลังค้นหา `{arg}`...")

    async def status_cb(text: str):
        try:
            await status_msg.edit(text, parse_mode="md")
        except Exception:
            await event.respond(text, parse_mode="md")

    async def _run():
        try:
            await clone_group(source, status_cb)
        except asyncio.CancelledError:
            await status_cb(f"🛑 ยกเลิก clone `{arg}` แล้ว")
        finally:
            active_tasks.pop(task_key, None)

    task = asyncio.create_task(_run())
    active_tasks[task_key] = task
