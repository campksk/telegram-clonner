"""
config.py — ค่า config, Telethon clients, และ shared state ทั้งหมด

import จากที่นี่เพื่อให้ทุกโมดูลใช้ client / state ตัวเดียวกัน
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
SESSION_DIR  = BASE_DIR / "sessions"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
SENT_DB_PATH = BASE_DIR / "sent_media.json"

SESSION_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ─── Env vars ──────────────────────────────────────────────────────────────────
API_ID        = int(os.getenv("API_ID", "0"))
API_HASH      = os.getenv("API_HASH", "")
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
DEST_GROUP_ID = int(os.getenv("DEST_GROUP_ID", "0"))
SEND_DELAY    = float(os.getenv("SEND_DELAY", "1.5"))
ALLOWED_USERS = set(
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip()
)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ─── Telethon clients ──────────────────────────────────────────────────────────
# bot_client  → รับคำสั่งจาก user (Bot Token)
# user_client → เข้าถึง group / download / upload (User session)
bot_client  = TelegramClient(str(SESSION_DIR / "bot"),  API_ID, API_HASH)
user_client = TelegramClient(str(SESSION_DIR / "user"), API_ID, API_HASH)

# ─── Shared runtime state ──────────────────────────────────────────────────────
# active_tasks: clone job ที่กำลังทำงาน → ใช้สำหรับ cancel / jobs
# key = source arg string, value = asyncio.Task
active_tasks: dict[str, asyncio.Task] = {}
