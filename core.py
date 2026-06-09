from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN

# 1. User API (สวมรอยเป็นคุณสำหรับกวาดข้อความ)
user_app = Client("user_session", api_id=API_ID, api_hash=API_HASH)

# 2. Bot API (หน้าบ้านรับคำสั่ง)
# 🌟 ตั้งค่า plugins=dict(root="plugins") เพื่อให้บอทวิ่งไปหาไฟล์คำสั่งในโฟลเดอร์ plugins อัตโนมัติ
bot_app = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    plugins=dict(root="plugins")
)