import asyncio
import hashlib
import logging
import re
from telethon import events

import db
import worker
from config import bot_client, ALLOWED_USERS, active_tasks
from parsing import MESSAGE_PATTERN, extract_target, parse_source, parse_tg_link

log = logging.getLogger("clone-bot.message")

@bot_client.on(events.NewMessage(pattern=MESSAGE_PATTERN))
async def handle_message(event: events.NewMessage.Event):
    # 🌟 เพิ่ม 2 บรรทัดนี้: ถ้าข้อความไม่ได้มาจากแชทส่วนตัว ให้หยุดการทำงานทันที
    if not event.is_private:
        return

    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return

    # Use only the first URL of the message (links are often pasted with extra text or on
    # several lines); a bare ID/username with no URL is kept as-is.
    arg = extract_target(event.pattern_match.group(1) or "")
    if not arg: return
    
    status_msg = await event.reply(f"🚀 กำลังประมวลผล `{arg}`...")
    async def status_cb(text: str):
        try: await status_msg.edit(text, parse_mode="md")
        except Exception: await event.respond(text, parse_mode="md")

    link_parsed = parse_tg_link(arg)
    if link_parsed:
        chat_ref, msg_id = link_parsed
        asyncio.create_task(worker.clone_single_message(chat_ref, msg_id, status_cb))
        return

    # 🌟 เพิ่มส่วนนี้เข้าไป: ตรวจสอบว่าเป็นลิงก์ X / Twitter หรือไม่
    x_match = re.search(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", arg)
    if x_match:
        task_key = f"x_{x_match.group(2)}"
        if task_key in active_tasks and not active_tasks[task_key].done():
            await event.reply(f"⚠️ กำลังโหลดลิงก์ X นี้อยู่...")
            return

        async def _run_x():
            try:
                await worker.clone_x_media(arg, status_cb)
                await db.mark_url_sent(arg)
            except asyncio.CancelledError:
                await status_cb(f"🛑 ยกเลิกโหลด X แล้ว")
            finally:
                active_tasks.pop(task_key, None)

        active_tasks[task_key] = asyncio.create_task(_run_x())
        return
    # (สิ้นสุดส่วนที่เพิ่ม)

    # 🌟 เพิ่มส่วนดักจับ Instagram (รองรับทั้ง Post, Reel, TV และ Story)
    ig_match = re.search(r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv|stories)/([^/?#&]+)", arg)
    if ig_match:
        task_key = f"ig_{ig_match.group(1)}"
        if task_key in active_tasks and not active_tasks[task_key].done():
            await event.reply(f"⚠️ กำลังโหลดลิงก์ Instagram นี้อยู่...")
            return

        async def _run_ig():
            try:
                await worker.clone_ig_media(arg, status_cb)
                await db.mark_url_sent(arg)
            except asyncio.CancelledError:
                await status_cb(f"🛑 ยกเลิกโหลด Instagram แล้ว")
            finally:
                active_tasks.pop(task_key, None)

        active_tasks[task_key] = asyncio.create_task(_run_ig())
        return
    # (สิ้นสุดส่วน Instagram)

    # 🌟 เพิ่มส่วนดักจับ TikTok (รองรับทั้งเว็บปกติและลิงก์แชร์แบบสั้น)
    tiktok_match = re.search(r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/\S+", arg)
    if tiktok_match:
        task_key = f"tiktok_{hashlib.md5(arg.encode()).hexdigest()[:12]}"
        
        async def _run_tiktok():
            try:
                await worker.clone_tiktok_media(arg, status_cb)
                await db.mark_url_sent(arg)
            except asyncio.CancelledError:
                await status_cb(f"🛑 ยกเลิกโหลด TikTok แล้ว")
            finally:
                active_tasks.pop(task_key, None)

        active_tasks[task_key] = asyncio.create_task(_run_tiktok())
        return
    # (สิ้นสุดส่วน TikTok)

    # Whole group / channel / user
    source = parse_source(arg)
    if source is None:
        await status_cb(f"⚠️ not recognized: `{arg}`\nSend a t.me link, numeric ID, or username")
        return

    task_key = str(arg)
    if task_key in active_tasks and not active_tasks[task_key].done():
        await event.reply(f"⚠️ `{arg}` กำลัง clone อยู่ พิมพ์ `/cancel` เพื่อหยุด")
        return

    async def _run():
        db.add_pending_job(task_key)
        try:
            await worker.clone_group(source, status_cb)
        except asyncio.CancelledError:
            await status_cb(f"🛑 ยกเลิก clone `{arg}` แล้ว")
        finally:
            db.remove_pending_job(task_key)
            active_tasks.pop(task_key, None)

    active_tasks[task_key] = asyncio.create_task(_run())
