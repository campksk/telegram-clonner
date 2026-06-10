# tg-clone-bot

Clone media ทั้งหมดจาก Telegram group ไปยัง topic ใน group ปลายทาง

## Flow

```
User พิมพ์: clone -100xxxxxxx
       ↓
Bot Client รับคำสั่ง (Bot Token)
       ↓
User Client เชื่อมต่อ Telegram API
       ↓
ค้นหา / สร้าง Topic ใน DEST_GROUP ที่ชื่อตรงกับ source group
       ↓
วนดึง media ทุกไฟล์ → download → upload → ลบไฟล์ชั่วคราว
       ↓
ส่ง progress กลับให้ user ทุก 10 ไฟล์
```

## ขั้นตอนติดตั้ง

### 1. สมัคร/หา credentials

| สิ่งที่ต้องการ | วิธีหา |
|---|---|
| `API_ID` + `API_HASH` | https://my.telegram.org/apps |
| `BOT_TOKEN` | คุยกับ @BotFather → `/newbot` |
| `DEST_GROUP_ID` | group supergroup ที่เปิด Topics แล้ว |

> **หมายเหตุ:** User client ต้องเป็นบัญชีที่ **อยู่ใน source group** ด้วย

---

### 2. ติดตั้งบน Windows

```powershell
# clone โปรเจกต์
cd C:\Users\you\projects
# วาง folder tg-clone-bot ที่นี่

# สร้าง virtual environment
python -m venv venv
venv\Scripts\activate

# ติดตั้ง dependencies
pip install -r requirements.txt

# ตั้งค่า .env
copy .env.example .env
notepad .env   # แก้ไขค่าต่าง ๆ
```

```powershell
# รันครั้งแรก (จะให้ login User client ผ่าน phone number)
python main.py
```

---

### 3. ติดตั้งบน Raspberry Pi (systemd)

```bash
# clone / วาง folder
cd /home/pi
# วาง folder tg-clone-bot ที่นี่

# สร้าง venv
python3 -m venv /home/pi/tg-clone-bot/venv
/home/pi/tg-clone-bot/venv/bin/pip install -r /home/pi/tg-clone-bot/requirements.txt

# ตั้งค่า .env
cp /home/pi/tg-clone-bot/.env.example /home/pi/tg-clone-bot/.env
nano /home/pi/tg-clone-bot/.env

# รันครั้งแรกเพื่อ login User client
cd /home/pi/tg-clone-bot
./venv/bin/python main.py
# → กรอกเบอร์โทรศัพท์ + OTP → กด Ctrl+C หลัง login สำเร็จ

# ติดตั้ง systemd service
sudo cp tg-clone-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tg-clone-bot
sudo systemctl start tg-clone-bot

# ตรวจสอบ log
sudo journalctl -u tg-clone-bot -f
```

---

## การใช้งาน

เปิด Telegram แล้วพิมพ์ให้บอท:

```
clone -1001234567890
```

บอทจะตอบกลับ progress และแจ้งเมื่อเสร็จ:

```
✅ Clone เสร็จสิ้น!
• Group: ชื่อกลุ่มต้นทาง
• Topic: 123456
• สำเร็จ: 247/250
• ล้มเหลว: 3/250
```

---

## ข้อควรระวัง

- **Flood limit:** ระบบหน่วง `SEND_DELAY` วินาทีระหว่างแต่ละไฟล์ (default 1.5s)  
  ถ้าโดน FloodWait จะหยุดรอตามเวลาที่ Telegram กำหนดอัตโนมัติ
- **Disk space:** ไฟล์จะถูกลบทันทีหลังส่ง ใช้พื้นที่ชั่วคราวตาม 1 ไฟล์ใหญ่สุด
- **User account:** ใช้ User API (ไม่ใช่ Bot API) เพราะ Bot ไม่สามารถอ่าน group ทั่วไปได้
- **Destination group:** ต้องเป็น Supergroup ที่เปิด **Topics** ไว้ และ user_client เป็น admin

## โครงสร้างไฟล์

```
tg-clone-bot/
├── main.py               # โปรแกรมหลัก
├── requirements.txt
├── .env.example          # template ค่า config
├── .env                  # ค่าจริง (ห้าม commit!)
├── tg-clone-bot.service  # systemd สำหรับ Raspberry Pi
├── sessions/             # Telethon session files (auto-generated)
└── downloads/            # temp folder (auto-cleaned)
```
