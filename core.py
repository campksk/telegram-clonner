from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN

# 1. User API (สวมรอยเป็นคุณสำหรับกวาดข้อความ)
# 🌟 เติม no_updates=True เพื่อสั่งให้มันปิดหู ไม่ต้องสนใจข้อความจากกลุ่มอื่นๆ ที่ Telegram ยิงมาให้
user_app = Client(
    "user_session", 
    api_id=API_ID, 
    api_hash=API_HASH,
    no_updates=True  
)

# 2. Bot API (หน้าบ้านรับคำสั่ง)
bot_app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins=dict(root="plugins")
)