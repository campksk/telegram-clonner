import asyncio
import logging
import sys
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

import db
import worker
from config import bot_client, user_client, BOT_TOKEN, ALLOWED_USERS, active_tasks
import handlers  # Registers events

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("clone-bot.main")

async def main():
    db.load_sent()
    log.info("กำลังเชื่อมต่อ User client...")
    await user_client.start()
    log.info("กำลังเชื่อมต่อ Bot client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    
    await bot_client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="cancel", description="ยกเลิก job (ทั้งหมด หรือระบุ source)"),
            BotCommand(command="jobs", description="ดู job ที่ทำงานอยู่"),
            BotCommand(command="ping", description="เช็คสถานะ server"),
        ]
    ))

    pending = db.load_pending_jobs()
    if pending:
        log.info(f"พบ {len(pending)} pending job — กำลัง resume...")
        for job in pending:
            src_arg = job["source"]
            async def status_cb(text: str): log.info(f"[resume:{src_arg}] {text}")
            
            async def _resume_run(s=src_arg, c=status_cb, k=src_arg):
                try: await worker.clone_group(s, c)
                except asyncio.CancelledError: await c(f"🛑 ยกเลิก `{k}`")
                finally:
                    db.remove_pending_job(k)
                    active_tasks.pop(k, None)

            active_tasks[src_arg] = asyncio.create_task(_resume_run())

    log.info("✅ ระบบพร้อมรับข้อความและลิงก์อัตโนมัติ")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
