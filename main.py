"""
tg-clone-bot — main.py
รันด้วย: python main.py

Flow:
  User พิมพ์ "clone -100xxxxxxx" ให้บอท
  → Bot (BotClient) รับคำสั่ง แล้วส่ง job ให้ UserClient
  → UserClient ดึง media ทั้งหมดจาก source group
  → Upload ไปยัง topic ใน DEST_GROUP_ID (สร้าง topic ถ้ายังไม่มี)
"""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.channels import (
    CreateForumTopicRequest,
    GetForumTopicsRequest,
)
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telethon.errors import FloodWaitError, ChatAdminRequiredError

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────
API_ID          = int(os.getenv("API_ID", "0"))
API_HASH        = os.getenv("API_HASH", "")
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
DEST_GROUP_ID   = int(os.getenv("DEST_GROUP_ID", "0"))
ALLOWED_USERS   = set(
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
)
DOWNLOAD_DIR    = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
SEND_DELAY      = float(os.getenv("SEND_DELAY", "1.5"))

SESSION_DIR = Path("./sessions")
SESSION_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clone-bot")

# ─── Clients ───────────────────────────────────────────────────────────────────
# bot_client  → รับคำสั่งจาก user
# user_client → เข้าถึง group ส่วนตัว / download / upload
bot_client  = TelegramClient(str(SESSION_DIR / "bot"),  API_ID, API_HASH)
user_client = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)

# ─── Media type filter ─────────────────────────────────────────────────────────
SUPPORTED_MIME_PREFIXES = ("image/", "video/", "audio/")
SUPPORTED_MIME_EXACT    = {"application/ogg"}  # voice note บางรูปแบบ

def is_supported_media(msg) -> bool:
    """คืน True ถ้า message มี media ที่ต้องการ clone"""
    if msg.photo:
        return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if any(mime.startswith(p) for p in SUPPORTED_MIME_PREFIXES):
            return True
        if mime in SUPPORTED_MIME_EXACT:
            return True
        # Document/file ทั่วไป (PDF, ZIP, ฯลฯ)
        return True
    return False


# ─── Topic helper ──────────────────────────────────────────────────────────────
async def get_or_create_topic(group_title: str) -> int:
    """
    ค้นหา topic ใน DEST_GROUP ที่ชื่อตรงกับ group_title
    ถ้าไม่มีให้สร้างใหม่ คืน thread_id (message_id ของ topic head)
    """
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)

    # ดึง topic list (max 100 topics)
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
            log.info(f"พบ topic ที่มีอยู่แล้ว: '{topic.title}' (id={topic.id})")
            return topic.id

    # สร้าง topic ใหม่
    log.info(f"ไม่พบ topic '{group_title}' — กำลังสร้างใหม่...")
    created = await user_client(CreateForumTopicRequest(
        channel=dest_entity,
        title=group_title,
    ))
    thread_id = created.updates[0].id
    log.info(f"สร้าง topic สำเร็จ (thread_id={thread_id})")
    return thread_id


# ─── Clone worker ──────────────────────────────────────────────────────────────
async def clone_group(source_id: int, status_cb) -> None:
    """
    ดึง media ทั้งหมดจาก source_id แล้วส่งไปยัง DEST_GROUP_ID/topic
    status_cb(text) ใช้สำหรับส่งข้อความ progress กลับไปยัง bot chat
    """
    # 1. หา entity ของ source group
    try:
        source_entity = await user_client.get_entity(source_id)
    except Exception as e:
        await status_cb(f"❌ ไม่พบ group `{source_id}`\n`{e}`")
        return

    group_title = getattr(source_entity, "title", str(source_id))
    await status_cb(f"🔍 พบ group: **{group_title}**\nกำลังนับ media...")

    # 2. หา/สร้าง topic ปลายทาง
    try:
        thread_id = await get_or_create_topic(group_title)
    except ChatAdminRequiredError:
        await status_cb("❌ บอทต้องเป็น admin ใน destination group และเปิด Topics ด้วย")
        return
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ล้มเหลว: `{e}`")
        return

    # 3. Collect และจัดกลุ่ม messages ด้วย grouped_id
    #    แต่ละ "batch" คือ List[Message] ที่ส่งพร้อมกัน
    #    - album (grouped_id ตรงกัน) → batch เดียว หลายไฟล์
    #    - single media (ไม่มี grouped_id) → batch เดียว 1 ไฟล์
    raw_messages = []
    async for msg in user_client.iter_messages(source_entity, reverse=True):
        if is_supported_media(msg):
            raw_messages.append(msg)

    # จัดกลุ่ม: รักษาลำดับ, album ต้องอยู่ติดกัน (Telegram รับประกันข้อนี้)
    batches: list[list] = []
    album_buf: dict[int, list] = {}   # grouped_id → [msgs]
    seen_albums: list[int] = []       # เก็บ order ของ album

    for msg in raw_messages:
        gid = msg.grouped_id
        if gid:
            if gid not in album_buf:
                album_buf[gid] = []
                seen_albums.append(gid)
            album_buf[gid].append(msg)
        else:
            # flush album ที่ค้างก่อน (ถ้ามี) ตาม order
            for aid in seen_albums:
                batches.append(album_buf.pop(aid))
            seen_albums.clear()
            batches.append([msg])

    # flush album ที่เหลือท้าย
    for aid in seen_albums:
        batches.append(album_buf.pop(aid))

    total_files  = len(raw_messages)
    total_batches = len(batches)

    if total_files == 0:
        await status_cb("ℹ️ ไม่พบ media ใน group นี้")
        return

    await status_cb(
        f"📦 พบ media ทั้งหมด **{total_files}** ไฟล์ ({total_batches} กลุ่ม)\n"
        f"📤 กำลัง clone ไปยัง topic **{group_title}**..."
    )

    # 4. Download → Upload ทีละ batch
    job_dir = DOWNLOAD_DIR / str(source_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)

    success, failed, sent_files = 0, 0, 0
    for b_idx, batch in enumerate(batches, 1):
        paths: list[str] = []
        try:
            # Download ทุกไฟล์ใน batch (album download พร้อมกัน)
            for msg in batch:
                p = await user_client.download_media(msg, file=str(job_dir) + "/")
                if not p:
                    raise ValueError(f"download_media returned None (msg_id={msg.id})")
                paths.append(p)

            # Caption: ใช้ข้อความจาก message สุดท้ายของ album (Telegram convention)
            caption = batch[-1].text or ""

            if len(paths) == 1:
                # Single file
                await user_client.send_file(
                    dest_entity,
                    file=paths[0],
                    caption=caption,
                    reply_to=thread_id,
                    parse_mode="md",
                )
            else:
                # Album — ส่งพร้อมกันในข้อความเดียว
                # captions เป็น list: ไฟล์แรกใส่ caption, ที่เหลือว่าง
                captions = [caption] + [""] * (len(paths) - 1)
                await user_client.send_file(
                    dest_entity,
                    file=paths,
                    caption=captions,
                    reply_to=thread_id,
                    parse_mode="md",
                )
                log.info(f"[batch {b_idx}] album {len(paths)} ไฟล์ ✓")

            success += len(batch)
            sent_files += len(batch)

        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s — รอ...")
            await asyncio.sleep(e.seconds + 2)
            failed += len(batch)

        except Exception as e:
            log.error(f"[batch {b_idx}] ✗ {e}")
            failed += len(batch)

        finally:
            # ลบไฟล์ทั้ง batch ทันที
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except Exception:
                    pass

        # Progress ทุก 10 batch
        if b_idx % 10 == 0 or b_idx == total_batches:
            await status_cb(
                f"⏳ Progress: {sent_files + failed}/{total_files} ไฟล์ "
                f"| {b_idx}/{total_batches} กลุ่ม "
                f"(✓{success} ✗{failed})"
            )

        await asyncio.sleep(SEND_DELAY)

    # 5. Cleanup folder
    shutil.rmtree(job_dir, ignore_errors=True)

    await status_cb(
        f"✅ Clone เสร็จสิ้น!\n"
        f"• Group: **{group_title}**\n"
        f"• Topic: `{thread_id}`\n"
        f"• สำเร็จ: {success}/{total_files} ไฟล์\n"
        f"• ล้มเหลว: {failed}/{total_files} ไฟล์\n"
        f"• Album ที่ส่งพร้อมกัน: {sum(1 for b in batches if len(b) > 1)} กลุ่ม"
    )


# ─── Bot command handler ────────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^clone\s+(-100\d+)$"))
async def handle_clone(event: events.NewMessage.Event):
    sender_id = event.sender_id

    # Access control
    if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
        await event.reply("⛔ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    source_id = int(event.pattern_match.group(1))

    # ส่ง status กลับไปยัง chat เดิม
    status_msg = await event.reply(f"🚀 เริ่ม clone จาก `{source_id}`...")

    async def status_cb(text: str):
        try:
            await status_msg.edit(text, parse_mode="md")
        except Exception:
            await event.respond(text, parse_mode="md")

    # รัน worker ใน background (ไม่ block event loop)
    asyncio.create_task(clone_group(source_id, status_cb))


@bot_client.on(events.NewMessage(pattern=r"^clone\s+"))
async def handle_clone_bad_format(event):
    """จับ clone ที่ format ผิด"""
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return
    await event.reply(
        "⚠️ รูปแบบไม่ถูกต้อง\n"
        "ใช้: `clone -100xxxxxxxxxx`\n"
        "ตัวอย่าง: `clone -1001234567890`",
        parse_mode="md",
    )


# ─── Entry point ───────────────────────────────────────────────────────────────
async def main():
    log.info("กำลังเชื่อมต่อ User client...")
    await user_client.start()
    log.info("User client พร้อม")

    log.info("กำลังเชื่อมต่อ Bot client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    log.info(f"Bot พร้อม: @{me.username}")

    log.info("✅ ระบบพร้อมรับคำสั่ง clone")
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
