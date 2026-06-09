import os
from dotenv import load_dotenv

# โหลดค่าจากไฟล์ .env
load_dotenv()

# แปลงค่าให้เป็น Type ที่ถูกต้อง
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DESTINATION_GROUP_ID = int(os.getenv("DESTINATION_GROUP_ID"))