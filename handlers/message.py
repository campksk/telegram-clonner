import asyncio
import logging
import re
from telethon import events

import db
import worker
from config import bot_client, ALLOWED_USERS, active_tasks

log = logging.getLogger("clone-bot.message")

def parse_tg_link(url: str):
    m = re.search(r"https?://t\.me/(?:(?P<username>[^/c][^/]*)/(?P<msg_id>\d+)|c/(?P<chat_id>\d+)/(?P<priv_msg_id>\d+))", url)
    if not m: return None
    if m.group("username"): return m.group("username"), int(m.group("msg_id"))
    else: return int("-100" + m.group("chat_id")), int(m.group("priv_msg_id"))

@bot_client.on(events.NewMessage(pattern=r"^(?!/)(.+)$"))
async def handle_message(event: events.NewMessage.Event):
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return

    arg = (event.pattern_match.group(1) or "").strip()
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

    source = None
    priv_match = re.search(r"t\.me/c/(\d+)", arg)
    pub_match = re.search(r"t\.me/([^/]+)/?$", arg)

    if priv_match:
        source = int("-100" + priv_match.group(1))
    elif pub_match and not pub_match.group(1).startswith("+") and pub_match.group(1) != "joinchat":
        source = pub_match.group(1)
    elif re.fullmatch(r"-?\d+", arg):
        source = int(arg)
    else:
        source = arg

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
