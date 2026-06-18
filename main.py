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
# จำนวน batch ที่ download+upload พร้อมกันได้ (ป้องกัน flood)
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "3"))

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

JOBS_DB = Path("./pending_jobs.json")

# ─── Pending jobs DB (resume หลัง crash/restart) ───────────────────────────────
def load_pending_jobs() -> list[dict]:
    """โหลด jobs ที่ค้างอยู่ก่อน crash"""
    if not JOBS_DB.exists():
        return []
    try:
        return json.loads(JOBS_DB.read_text())
    except Exception:
        return []

def save_pending_jobs(jobs: list[dict]) -> None:
    JOBS_DB.write_text(json.dumps(jobs, ensure_ascii=False))

def add_pending_job(source_arg: str) -> None:
    jobs = load_pending_jobs()
    if not any(j["source"] == source_arg for j in jobs):
        jobs.append({"source": source_arg})
        save_pending_jobs(jobs)

def remove_pending_job(source_arg: str) -> None:
    jobs = [j for j in load_pending_jobs() if j["source"] != source_arg]
    save_pending_jobs(jobs)


# task ที่กำลังทำงานอยู่ → ใช้สำหรับ cancel
# key = label string (เช่น source arg), value = asyncio.Task
active_tasks: dict[str, asyncio.Task] = {}

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clone-bot")

# ปิด verbose log ของ Telethon (updates, gaps, connection noise)
for _tl in (
    "telethon",
    "telethon.client.updates",
    "telethon.client.uploads",
    "telethon.network.mtprotosender",
    "telethon.extensions.messagepacker",
):
    logging.getLogger(_tl).setLevel(logging.WARNING)

# ─── Clients ───────────────────────────────────────────────────────────────────
# bot_client  → รับคำสั่งจาก user
# user_client → เข้าถึง group ส่วนตัว / download / upload
bot_client  = TelegramClient(str(SESSION_DIR / "bot"),  API_ID, API_HASH)
user_client = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)

# ─── Media type filter ─────────────────────────────────────────────────────────
# MIME ที่ถือเป็นสติกเกอร์ → ข้ามเสมอ
STICKER_MIMES = {"image/webp", "application/x-tgsticker"}

def is_supported_media(msg) -> bool:
    """คืน True ถ้า message มี media ที่ต้องการ clone (ยกเว้นสติกเกอร์)"""
    if msg.sticker:          # attribute ตรง
        return False
    if msg.photo:
        return True
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime in STICKER_MIMES:
            return False     # ข้ามสติกเกอร์ทุกรูปแบบ
        # Photo, Video, Audio, Voice, Document/file ทั่วไป → รับหมด
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


# ─── Public group detection ────────────────────────────────────────────────────
def is_public_entity(entity) -> bool:
    """
    คืน True ถ้า forward ได้โดยไม่ต้อง download:
    - ต้องมี username (public)
    - ต้องไม่มี noforwards flag (content protection)
    """
    from telethon.tl.types import User as TLUser
    if isinstance(entity, TLUser):
        return False
    has_username  = bool(getattr(entity, "username", None))
    no_fwd_flag   = bool(getattr(entity, "noforwards", False))
    result = has_username and not no_fwd_flag
    log.debug(
        f"is_public_entity({getattr(entity,'title', entity.__class__.__name__)!r}): "
        f"username={has_username} noforwards={no_fwd_flag} → {result}"
    )
    return result


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

    # 4. เลือก mode: forward (public) หรือ download→upload (private)
    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    use_forward = is_public_entity(source_entity)
    mode_label  = "⚡ forward" if use_forward else f"📥 download→upload (workers={PARALLEL_WORKERS})"
    await status_cb(
        f"📦 พบ media ใหม่ **{total_files}** ไฟล์ ({total_batches} กลุ่ม){skip_note}\n"
        f"📤 mode: {mode_label} → topic **{group_title}**..."
    )

    sem           = asyncio.Semaphore(PARALLEL_WORKERS)
    counters      = {"success": 0, "failed": 0, "done": 0}
    lock          = asyncio.Lock()
    progress_lock = asyncio.Lock()

    async def _update_progress() -> None:
        done_now = counters["done"]
        if done_now % 10 == 0 or done_now == total_batches:
            async with progress_lock:
                await status_cb(
                    f"⏳ Progress: {counters['success'] + counters['failed']}/{total_files} ไฟล์ "
                    f"| {done_now}/{total_batches} กลุ่ม "
                    f"(✓{counters['success']} ✗{counters['failed']})"
                )

    async def process_batch_forward(b_idx: int, batch: list) -> None:
        """
        Forward mode (public source) — server-side copy, เร็วมาก
        forward_messages() ส่งได้ครั้งละหลาย id พร้อมกัน (album-safe)
        """
        async with sem:
            try:
                msg_ids = [m.id for m in batch]
                await user_client.forward_messages(
                    entity=dest_entity,
                    messages=msg_ids,
                    from_peer=source_entity,
                    # reply_to ใน forward_messages ต้องใช้ SendMessageRequest
                    # workaround: pin thread ด้วย reply ทีหลังไม่ได้
                    # → ใช้ message thread_id ผ่าน top_msg_id
                    top_msg_id=thread_id,
                )
                async with lock:
                    counters["success"] += len(batch)
                    already_sent.update(m.id for m in batch)
                    sent_db[src_key] = already_sent
                    save_sent_db(sent_db)
                log.info(f"[fwd batch {b_idx}] {len(batch)} ไฟล์ ✓")

            except FloodWaitError as e:
                log.warning(f"[fwd batch {b_idx}] FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds + 2)
                async with lock:
                    counters["failed"] += len(batch)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                log.error(f"[fwd batch {b_idx}] ✗ {e}")
                async with lock:
                    counters["failed"] += len(batch)

            finally:
                async with lock:
                    counters["done"] += 1
                await _update_progress()
                await asyncio.sleep(SEND_DELAY / PARALLEL_WORKERS)

    async def process_batch_upload(b_idx: int, batch: list) -> None:
        """Download → upload 1 batch ภายใต้ semaphore"""
        paths: list[str] = []
        async with sem:
            try:
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
                    log.info(f"[up batch {b_idx}] album {len(paths)} ไฟล์ ✓")

                async with lock:
                    counters["success"] += len(batch)
                    already_sent.update(m.id for m in batch)
                    sent_db[src_key] = already_sent
                    save_sent_db(sent_db)

            except FloodWaitError as e:
                log.warning(f"[up batch {b_idx}] FloodWait {e.seconds}s — รอ...")
                await asyncio.sleep(e.seconds + 2)
                async with lock:
                    counters["failed"] += len(batch)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                log.error(f"[up batch {b_idx}] ✗ {e}")
                async with lock:
                    counters["failed"] += len(batch)

            finally:
                for p in paths:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
                async with lock:
                    counters["done"] += 1
                await _update_progress()
                await asyncio.sleep(SEND_DELAY / PARALLEL_WORKERS)

    # เลือก coroutine ตาม mode แล้วรันพร้อมกัน
    process_fn = process_batch_forward if use_forward else process_batch_upload
    job_dir    = DOWNLOAD_DIR / str(source_entity.id)
    if not use_forward:
        job_dir.mkdir(parents=True, exist_ok=True)

    await asyncio.gather(*[
        process_fn(i, batch)
        for i, batch in enumerate(batches, 1)
    ])

    # 5. Cleanup (upload mode เท่านั้น)
    if not use_forward:
        shutil.rmtree(job_dir, ignore_errors=True)

    album_count = sum(1 for b in batches if len(b) > 1)
    await status_cb(
        f"✅ Clone เสร็จสิ้น!\n"
        f"• Group: **{group_title}**\n"
        f"• Mode: {mode_label}\n"
        f"• Topic: `{thread_id}`\n"
        f"• สำเร็จ: {counters['success']}/{total_files} ไฟล์\n"
        f"• ล้มเหลว: {counters['failed']}/{total_files} ไฟล์\n"
        f"• Album: {album_count} กลุ่ม"
    )


# ─── Single message clone (จากลิงก์) ──────────────────────────────────────────
import re as _link_re

# รองรับ:
#   https://t.me/username/123
#   https://t.me/c/1234567890/123        (private)
#   https://t.me/username/123?single     (single item ใน album)
_TG_LINK_RE = _link_re.compile(
    r"https?://t\.me/"
    r"(?:(?P<username>[^/c][^/]*)/(?P<msg_id>\d+)"   # public
    r"|c/(?P<chat_id>\d+)/(?P<priv_msg_id>\d+))"     # private
)

def parse_tg_link(url: str) -> tuple[str | int, int] | None:
    """
    แยก (chat_identifier, msg_id) จากลิงก์ Telegram
    คืน None ถ้าไม่ใช่ลิงก์ที่รู้จัก
    """
    m = _TG_LINK_RE.search(url)
    if not m:
        return None
    if m.group("username"):
        return m.group("username"), int(m.group("msg_id"))
    else:
        # private link: chat_id เป็น bare id (ไม่มี -100 prefix)
        return int("-100" + m.group("chat_id")), int(m.group("priv_msg_id"))


async def clone_single_message(
    chat: str | int,
    msg_id: int,
    dest_topic_override: str | None,
    status_cb,
) -> None:
    """
    ดึงเฉพาะ message เดียว (หรือ album ที่มี msg_id นั้น) แล้วส่งไปยัง dest group/topic
    dest_topic_override: ถ้า None → ใช้ชื่อ chat เป็นชื่อ topic
    """
    # Resolve chat entity
    try:
        chat_entity = await user_client.get_entity(chat)
    except Exception as e:
        await status_cb(f"❌ ไม่พบ chat `{chat}`\n`{e}`")
        return

    from telethon.tl.types import User as TLUser
    if isinstance(chat_entity, TLUser):
        parts = [chat_entity.first_name or "", chat_entity.last_name or ""]
        chat_title = " ".join(p for p in parts if p).strip() or str(chat_entity.id)
    else:
        chat_title = getattr(chat_entity, "title", str(chat))

    topic_name = dest_topic_override or chat_title

    # หา/สร้าง topic
    try:
        thread_id = await get_or_create_topic(topic_name)
    except ChatAdminRequiredError:
        await status_cb("❌ บอทต้องเป็น admin ใน destination group และเปิด Topics ด้วย")
        return
    except Exception as e:
        await status_cb(f"❌ สร้าง/หา topic ล้มเหลว: `{e}`")
        return

    # ดึง message
    msgs = await user_client.get_messages(chat_entity, ids=msg_id)
    if not msgs:
        await status_cb(f"❌ ไม่พบ message id `{msg_id}` ใน `{chat_title}`")
        return

    msg = msgs if not isinstance(msgs, list) else msgs[0]
    if not msg:
        await status_cb(f"❌ ไม่พบ message id `{msg_id}`")
        return

    # ถ้า message นี้เป็นส่วนหนึ่งของ album → ดึงทั้ง album
    batch: list = []
    if msg.grouped_id:
        async for m in user_client.iter_messages(
            chat_entity,
            min_id=msg_id - 20,   # album มักอยู่ใกล้กัน
            max_id=msg_id + 20,
        ):
            if m.grouped_id == msg.grouped_id and is_supported_media(m):
                batch.append(m)
        batch.sort(key=lambda m: m.id)   # เรียงตาม id
    elif is_supported_media(msg):
        batch = [msg]
    else:
        await status_cb(f"⚠️ Message `{msg_id}` ไม่มี media ที่รองรับ")
        return

    await status_cb(
        f"🔍 พบ: **{chat_title}** — message `{msg_id}`\n"
        f"📦 {'album ' + str(len(batch)) + ' ไฟล์' if len(batch) > 1 else '1 ไฟล์'}\n"
        f"📤 กำลังส่ง → topic **{topic_name}**..."
    )

    dest_entity = await user_client.get_entity(DEST_GROUP_ID)
    use_forward = is_public_entity(chat_entity)
    log.info(
        f"[single] chat={chat_title!r} "
        f"username={getattr(chat_entity,'username',None)!r} "
        f"noforwards={getattr(chat_entity,'noforwards',None)} "
        f"→ mode={'forward' if use_forward else 'upload'}"
    )

    async def _do_upload(dest, batch_, thread_id_, chat_entity_, msg_id_):
        """Download → upload fallback"""
        job_dir = DOWNLOAD_DIR / f"single_{chat_entity_.id}_{msg_id_}"
        job_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        try:
            for m in batch_:
                p = await user_client.download_media(m, file=str(job_dir) + "/")
                if p:
                    paths.append(p)
            caption = batch_[-1].text or ""
            if len(paths) == 1:
                await user_client.send_file(
                    dest, file=paths[0],
                    caption=caption, reply_to=thread_id_, parse_mode="md",
                )
            else:
                captions = [caption] + [""] * (len(paths) - 1)
                await user_client.send_file(
                    dest, file=paths,
                    caption=captions, reply_to=thread_id_, parse_mode="md",
                )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    try:
        actual_mode = "📥 upload"
        if use_forward:
            try:
                await user_client.forward_messages(
                    entity=dest_entity,
                    messages=[m.id for m in batch],
                    from_peer=chat_entity,
                    top_msg_id=thread_id,
                )
                actual_mode = "⚡ forward"
            except Exception as fwd_err:
                # forward ล้มเหลว (เช่น noforwards หรือ permission) → fallback upload
                log.warning(f"[single] forward failed ({fwd_err}) — falling back to upload")
                await _do_upload(dest_entity, batch, thread_id, chat_entity, msg_id)
                actual_mode = "📥 upload (forward fallback)"
        else:
            await _do_upload(dest_entity, batch, thread_id, chat_entity, msg_id)

        await status_cb(
            f"✅ ส่งสำเร็จ!\n"
            f"• Chat: **{chat_title}**\n"
            f"• Message ID: `{msg_id}`\n"
            f"• ไฟล์: {len(batch)}\n"
            f"• Mode: {actual_mode}\n"
            f"• Topic: **{topic_name}**"
        )

    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 2)
        await status_cb(f"❌ FloodWait {e.seconds}s — ลองใหม่อีกครั้ง")
    except Exception as e:
        await status_cb(f"❌ ส่งล้มเหลว: `{e}`")


# ─── Bot command handlers ──────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^/clone(?:\s+(.*))?$"))
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
            "• `/clone -1001234567890` — group/channel ID\n"
            "• `/clone @username` — username ของ group หรือ user\n"
            "• `/clone ชื่อกลุ่ม` — ค้นหาจากชื่อ\n"
            "• `/clone https://t.me/username/123` — เฉพาะ message นั้น\n"
            "• `/clone https://t.me/c/123456/789` — private message",
            parse_mode="md",
        )
        return

    import re as _re

    status_msg = await event.reply(f"🚀 กำลังค้นหา `{arg}`...")

    async def status_cb(text: str):
        try:
            await status_msg.edit(text, parse_mode="md")
        except Exception:
            await event.respond(text, parse_mode="md")

    # ── ตรวจว่าเป็นลิงก์ t.me หรือเปล่า ──────────────────────────────────────
    link_parsed = parse_tg_link(arg)
    if link_parsed:
        chat_ref, msg_id = link_parsed
        # single message clone — ไม่ใส่ active_tasks/pending (เร็วมาก)
        asyncio.create_task(
            clone_single_message(chat_ref, msg_id, None, status_cb)
        )
        return

    # ── Clone ทั้ง group/user (เดิม) ──────────────────────────────────────────
    source: str | int
    if _re.fullmatch(r"-?\d+", arg):
        source = int(arg)
    else:
        source = arg

    task_key = str(arg)

    if task_key in active_tasks and not active_tasks[task_key].done():
        await event.reply(f"⚠️ `{arg}` กำลัง clone อยู่แล้ว พิมพ์ `/cancel` เพื่อหยุด")
        return

    async def _run():
        add_pending_job(task_key)
        try:
            await clone_group(source, status_cb)
        except asyncio.CancelledError:
            await status_cb(f"🛑 ยกเลิก clone `{arg}` แล้ว")
        finally:
            remove_pending_job(task_key)
            active_tasks.pop(task_key, None)

    task = asyncio.create_task(_run())
    active_tasks[task_key] = task


@bot_client.on(events.NewMessage(pattern=r"^/cancel(?:\s+(.*))?$"))
async def handle_cancel(event: events.NewMessage.Event):
    """ยกเลิก clone job ที่กำลังทำงานอยู่"""
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        await event.reply("⛔ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    arg = (event.pattern_match.group(1) or "").strip()
    running = {k: t for k, t in active_tasks.items() if not t.done()}

    # ไม่มี job ทำงานอยู่เลย
    if not running:
        await event.reply("ℹ️ ไม่มี job ที่กำลังทำงานอยู่")
        return

    if arg:
        # cancel เฉพาะ job ที่ระบุ
        if arg not in running:
            job_list = "\n".join(f"• `{k}`" for k in running)
            await event.reply(
                f"⚠️ ไม่พบ job `{arg}`\n\nJob ที่กำลังทำงาน:\n{job_list}",
                parse_mode="md",
            )
            return
        running[arg].cancel()
        await event.reply(f"🛑 ส่งสัญญาณยกเลิก `{arg}` แล้ว")
    else:
        # cancel ทุก job
        count = len(running)
        for task in running.values():
            task.cancel()
        await event.reply(f"🛑 ส่งสัญญาณยกเลิกทั้งหมด {count} job แล้ว")


@bot_client.on(events.NewMessage(pattern=r"^/jobs$"))
async def handle_jobs(event: events.NewMessage.Event):
    """แสดง job ที่กำลังทำงานอยู่"""
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return

    running = [k for k, t in active_tasks.items() if not t.done()]
    if not running:
        await event.reply("ℹ️ ไม่มี job ที่กำลังทำงานอยู่")
        return

    lines = "\n".join(f"• `{k}`" for k in running)
    await event.reply(
        f"⚙️ **Job ที่กำลังทำงาน ({len(running)})**\n{lines}\n\n"
        f"ยกเลิกทั้งหมด: `cancel`\n"
        f"ยกเลิกเฉพาะ: `cancel <source>`",
        parse_mode="md",
    )


@bot_client.on(events.NewMessage(pattern=r"^/ping$"))
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

    # Register slash commands (แสดงใน menu ของ Telegram)
    from telethon.tl.functions.bots import SetBotCommandsRequest
    from telethon.tl.types import BotCommand, BotCommandScopeDefault
    await bot_client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="clone",  description="Clone media จาก group/user"),
            BotCommand(command="cancel", description="ยกเลิก job (ทั้งหมด หรือระบุ source)"),
            BotCommand(command="jobs",   description="ดู job ที่กำลังทำงานอยู่"),
            BotCommand(command="ping",   description="ตรวจสอบสถานะ server"),
        ],
    ))
    log.info("Slash commands registered")

    # Resume pending jobs ที่ค้างจาก crash/restart
    pending = load_pending_jobs()
    if pending:
        log.info(f"พบ {len(pending)} pending job — กำลัง resume...")
        for job in pending:
            src_arg = job["source"]
            import re as _re
            source: str | int = int(src_arg) if _re.fullmatch(r"-?\d+", src_arg) else src_arg

            # สร้าง status_cb ที่ log แทน reply (ไม่มี chat context)
            async def make_log_cb(label: str):
                async def _cb(text: str):
                    log.info(f"[resume:{label}] {text}")
                return _cb

            cb = await make_log_cb(src_arg)

            # แจ้ง ALLOWED_USERS คนแรก (ถ้ามี) ว่า resume แล้ว
            if ALLOWED_USERS:
                notify_user = next(iter(ALLOWED_USERS))
                try:
                    notify_msg = await bot_client.send_message(
                        notify_user,
                        f"♻️ **Resume job:** `{src_arg}`\nระบบ restart — กำลังทำงานต่อจากเดิม...",
                        parse_mode="md",
                    )
                    async def make_chat_cb(msg):
                        async def _cb(text: str):
                            try:
                                await msg.edit(text, parse_mode="md")
                            except Exception:
                                await bot_client.send_message(notify_user, text, parse_mode="md")
                        return _cb
                    cb = await make_chat_cb(notify_msg)
                except Exception as e:
                    log.warning(f"ไม่สามารถแจ้ง user ได้: {e}")

            task_key = src_arg
            async def _resume_run(s=source, c=cb, k=task_key):
                add_pending_job(k)
                try:
                    await clone_group(s, c)
                except asyncio.CancelledError:
                    await c(f"🛑 ยกเลิก clone `{k}` แล้ว")
                finally:
                    remove_pending_job(k)
                    active_tasks.pop(k, None)

            task = asyncio.create_task(_resume_run())
            active_tasks[task_key] = task
            log.info(f"Resume: {src_arg}")

    log.info("✅ ระบบพร้อมรับคำสั่ง")
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
