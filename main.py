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
import json
import logging
import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.channels import (
    CreateForumTopicRequest,
    GetForumTopicsRequest,
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
SENT_DB = Path("./sent_media.json")

# ─── Sent-media DB (persist across restarts) ───────────────────────────────────
def load_sent_db() -> dict[str, set]:
    """โหลด {source_group_id_str: {msg_id, ...}} จากไฟล์"""
    if not SENT_DB.exists():
        return {}
    try:
        raw = json.loads(SENT_DB.read_text())
        return {k: set(v) for k, v in raw.items()}
    except Exception:
        return {}

def save_sent_db(db: dict[str, set]) -> None:
    SENT_DB.write_text(json.dumps({k: list(v) for k, v in db.items()}))

sent_db: dict[str, set] = load_sent_db()

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
async def clone_group(source: str | int, status_cb) -> None:
    """
    ดึง media จาก source (group_id, username, ชื่อกลุ่ม, หรือ user id/username)
    แล้วส่งไปยัง DEST_GROUP_ID/topic
    """
    # 1. Resolve entity — รองรับ int id, @username, ชื่อกลุ่ม/user
    try:
        source_entity = await user_client.get_entity(source)
    except Exception as e:
        await status_cb(f"❌ ไม่พบ `{source}`\n`{e}`")
        return

    # ดึงชื่อแสดงผล: group/channel ใช้ title, user ใช้ first_name [+ last_name]
    from telethon.tl.types import User as TLUser, Channel, Chat
    if isinstance(source_entity, TLUser):
        parts = [source_entity.first_name or "", source_entity.last_name or ""]
        group_title = " ".join(p for p in parts if p).strip() or str(source_entity.id)
        entity_type = "user"
    else:
        group_title = getattr(source_entity, "title", str(source))
        entity_type = "group/channel"

    # canonical key สำหรับ sent_db → ใช้ numeric id เสมอ
    src_key = str(source_entity.id)

    await status_cb(f"🔍 พบ {entity_type}: **{group_title}**\nกำลังนับ media...")

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
    already_sent: set = sent_db.get(src_key, set())

    raw_messages = []
    async for msg in user_client.iter_messages(source_entity, reverse=True):
        if is_supported_media(msg) and msg.id not in already_sent:
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

    skipped = len(already_sent)
    if total_files == 0:
        msg_skip = f" (ข้ามไปแล้ว {skipped} ไฟล์)" if skipped else ""
        await status_cb(f"ℹ️ ไม่มี media ใหม่ที่ต้องส่ง{msg_skip}")
        return

    skip_note = f" | ข้ามที่ส่งแล้ว {skipped} ไฟล์" if skipped else ""
    await status_cb(
        f"📦 พบ media ใหม่ **{total_files}** ไฟล์ ({total_batches} กลุ่ม){skip_note}\n"
        f"📤 กำลัง clone ไปยัง topic **{group_title}**..."
    )

    # 4. Download → Upload ทีละ batch
    job_dir = DOWNLOAD_DIR / str(source_entity.id)
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
            # บันทึก msg_id ที่ส่งสำเร็จแล้ว
            already_sent.update(m.id for m in batch)
            sent_db[src_key] = already_sent
            save_sent_db(sent_db)

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


# ─── Bot command handlers ──────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^clone(?:\s+(.*))?$"))
async def handle_clone(event: events.NewMessage.Event):
    """
    รับคำสั่ง clone ทั้งหมด แล้วแยก branch เองในตัว
    เหตุผล: Telethon match handler แรกที่ตรง ถ้าแยกเป็น 2 handler
    pattern แรก "^clone (arg)$" match ก่อน แต่ Telethon
    ยังคง fire handler ที่สองด้วย ทำให้ส่ง error message ซ้อน
    """
    sender_id = event.sender_id

    # Access control
    if ALLOWED_USERS and sender_id not in ALLOWED_USERS:
        await event.reply("⛔ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    arg = (event.pattern_match.group(1) or "").strip()

    # ต้องมี argument
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

    # แปลง arg → ชนิดที่ถูกต้องสำหรับ get_entity
    # - ตัวเลขล้วน หรือ -100xxx → int
    # - @username หรือ ชื่อ → str (Telethon จัดการให้)
    import re as _re
    source: str | int
    if _re.fullmatch(r"-?\d+", arg):
        source = int(arg)
    else:
        source = arg  # @username หรือ ชื่อ/phone

    status_msg = await event.reply(f"🚀 กำลังค้นหา `{arg}`...")

    async def status_cb(text: str):
        try:
            await status_msg.edit(text, parse_mode="md")
        except Exception:
            await event.respond(text, parse_mode="md")

    asyncio.create_task(clone_group(source, status_cb))


@bot_client.on(events.NewMessage(pattern=r"^ping$"))
async def handle_ping(event: events.NewMessage.Event):
    """ตรวจสอบสถานะ server"""
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return

    import platform, psutil
    cpu     = psutil.cpu_percent(interval=0.5)
    ram     = psutil.virtual_memory()
    disk    = psutil.disk_usage("/")
    uptime  = int(time.time() - psutil.boot_time())
    h, rem  = divmod(uptime, 3600)
    m, s    = divmod(rem, 60)

    # จำนวน source group ที่เคย clone แล้ว
    total_tracked = sum(len(v) for v in sent_db.values())

    await event.reply(
        f"🟢 **Server Status**\n"
        f"├ 🖥 OS: `{platform.system()} {platform.machine()}`\n"
        f"├ ⏱ Uptime: `{h}h {m}m {s}s`\n"
        f"├ 🔥 CPU: `{cpu:.1f}%`\n"
        f"├ 🧠 RAM: `{ram.used/1024**2:.0f} / {ram.total/1024**2:.0f} MB ({ram.percent:.1f}%)`\n"
        f"├ 💾 Disk: `{disk.used/1024**3:.1f} / {disk.total/1024**3:.1f} GB ({disk.percent:.1f}%)`\n"
        f"└ 📦 Media tracked: `{total_tracked}` ไฟล์ จาก {len(sent_db)} กลุ่ม",
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
