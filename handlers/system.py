"""
handlers/system.py — system commands: ping, jobs, cancel
"""

import logging
import platform
import time

import psutil
from telethon import events

import db
from config import bot_client, ALLOWED_USERS, active_tasks

log = logging.getLogger("clone-bot.system")


# ─── ping ──────────────────────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^ping$"))
async def handle_ping(event: events.NewMessage.Event):
    """ตรวจสอบสถานะ server"""
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        return

    cpu    = psutil.cpu_percent(interval=0.5)
    ram    = psutil.virtual_memory()
    disk   = psutil.disk_usage("/")
    uptime = int(time.time() - psutil.boot_time())
    h, rem = divmod(uptime, 3600)
    m, s   = divmod(rem, 60)

    total_files, total_sources = db.total_tracked()

    await event.reply(
        f"🟢 **Server Status**\n"
        f"├ 🖥 OS: `{platform.system()} {platform.machine()}`\n"
        f"├ ⏱ Uptime: `{h}h {m}m {s}s`\n"
        f"├ 🔥 CPU: `{cpu:.1f}%`\n"
        f"├ 🧠 RAM: `{ram.used/1024**2:.0f} / {ram.total/1024**2:.0f} MB ({ram.percent:.1f}%)`\n"
        f"├ 💾 Disk: `{disk.used/1024**3:.1f} / {disk.total/1024**3:.1f} GB ({disk.percent:.1f}%)`\n"
        f"└ 📦 Media tracked: `{total_files}` ไฟล์ จาก {total_sources} กลุ่ม",
        parse_mode="md",
    )


# ─── jobs ──────────────────────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^jobs$"))
async def handle_jobs(event: events.NewMessage.Event):
    """แสดง clone job ที่กำลังทำงานอยู่"""
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


# ─── cancel ────────────────────────────────────────────────────────────────────
@bot_client.on(events.NewMessage(pattern=r"^cancel(?:\s+(.*))?$"))
async def handle_cancel(event: events.NewMessage.Event):
    """ยกเลิก clone job — cancel ทั้งหมด หรือ cancel <source> เฉพาะอัน"""
    if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS:
        await event.reply("⛔ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    arg     = (event.pattern_match.group(1) or "").strip()
    running = {k: t for k, t in active_tasks.items() if not t.done()}

    if not running:
        await event.reply("ℹ️ ไม่มี job ที่กำลังทำงานอยู่")
        return

    if arg:
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
        count = len(running)
        for task in running.values():
            task.cancel()
        await event.reply(f"🛑 ส่งสัญญาณยกเลิกทั้งหมด {count} job แล้ว")
