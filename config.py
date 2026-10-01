import asyncio
import logging
import os
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

BASE_DIR     = Path(__file__).parent
SESSION_DIR  = BASE_DIR / "sessions"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))

SESSION_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

API_ID        = int(os.getenv("API_ID", "0"))
API_HASH      = os.getenv("API_HASH", "")
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
DEST_GROUP_ID = int(os.getenv("DEST_GROUP_ID", "0"))
SEND_DELAY    = float(os.getenv("SEND_DELAY", "1.5"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "3"))
ALLOWED_USERS = set(int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

for _tl in ("telethon", "telethon.client.updates", "telethon.client.uploads", "telethon.network.mtprotosender", "telethon.extensions.messagepacker"):
    logging.getLogger(_tl).setLevel(logging.WARNING)

bot_client  = TelegramClient(str(SESSION_DIR / "bot"),  API_ID, API_HASH)
user_client = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)

active_tasks: dict[str, asyncio.Task] = {}
