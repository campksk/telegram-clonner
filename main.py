import asyncio
import logging
import sys
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

import db
import jobs
from config import bot_client, user_client, BOT_TOKEN, ALLOWED_USERS, DEST_GROUP_ID
import handlers  # Registers events

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("clone-bot.main")

async def main():
    # Validate config at startup
    if not DEST_GROUP_ID or DEST_GROUP_ID == 0:
        log.error("DEST_GROUP_ID not set! Set it in .env and restart.")
        sys.exit(1)
    if not ALLOWED_USERS:
        log.error("ALLOWED_USER_IDS not set! The bot controls your Telegram user account, so at least one allowed user ID is required.")
        sys.exit(1)
    log.info(f"Config: DEST_GROUP_ID={DEST_GROUP_ID}")
    
    db.load_sent()
    db.load_sent_urls()
    log.info("Connecting user client...")
    await user_client.start()
    log.info("Connecting bot client...")
    await bot_client.start(bot_token=BOT_TOKEN)
    
    await bot_client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="",
        commands=[
            BotCommand(command="cancel", description="Cancel jobs (all, or name one)"),
            BotCommand(command="jobs", description="List running jobs"),
            BotCommand(command="ping", description="Server status"),
        ]
    ))

    def _resume_status(key):
        async def cb(text: str): log.info(f"[resume:{key}] {text}")
        return cb

    resumed = jobs.resume_pending_jobs(_resume_status)
    if resumed:
        log.info(f"Found {resumed} pending job(s) - resuming...")

    log.info("✅ Ready - send a link or a message")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
