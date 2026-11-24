import platform
import time
import psutil
from telethon import events

import db
from config import bot_client, is_allowed, active_tasks

@bot_client.on(events.NewMessage(pattern=r"^/ping$"))
async def handle_ping(event: events.NewMessage.Event):
    if not is_allowed(event.sender_id): return
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    import os
    disk = psutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
    uptime = int(time.time() - psutil.boot_time())
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    total_files, total_sources = db.total_tracked()
    await event.reply(
        f"🟢 **Server Status**\n├ 🖥 OS: `{platform.system()} {platform.machine()}`\n"
        f"├ ⏱ Uptime: `{h}h {m}m {s}s`\n├ 🔥 CPU: `{cpu:.1f}%`\n"
        f"├ 🧠 RAM: `{ram.used/1024**2:.0f} MB ({ram.percent:.1f}%)`\n"
        f"├ 💾 Disk: `{disk.used/1024**3:.1f} GB ({disk.percent:.1f}%)`\n"
        f"└ 📦 Media tracked: `{total_files}` files from {total_sources} sources",
        parse_mode="md"
    )

@bot_client.on(events.NewMessage(pattern=r"^/jobs$"))
async def handle_jobs(event: events.NewMessage.Event):
    if not is_allowed(event.sender_id): return
    running = [k for k, t in active_tasks.items() if not t.done()]
    if not running:
        await event.reply("ℹ️ No jobs are running")
        return
    lines = "\n".join(f"• `{k}`" for k in running)
    await event.reply(f"⚙️ **Running jobs ({len(running)})**\n{lines}\n\nCancel all: `/cancel`\nCancel one: `/cancel <job name>`", parse_mode="md")

@bot_client.on(events.NewMessage(pattern=r"^/cancel(?:\s+(.*))?$"))
async def handle_cancel(event: events.NewMessage.Event):
    if not is_allowed(event.sender_id): return
    arg = (event.pattern_match.group(1) or "").strip()
    running = {k: t for k, t in active_tasks.items() if not t.done()}
    if not running:
        await event.reply("ℹ️ No jobs are running")
        return
    if arg:
        if arg not in running:
            await event.reply(f"⚠️ No running job `{arg}`")
            return
        running[arg].cancel()
        db.remove_pending_job(arg)  # explicit cancel: do not resume after restart
        await event.reply(f"🛑 Cancelling `{arg}`")
    else:
        for k, task in running.items():
            task.cancel()
            db.remove_pending_job(k)
        await event.reply(f"🛑 Cancelling all {len(running)} jobs")
