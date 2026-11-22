# tg-clone-bot

บอท Telegram ทูโคลน Media จากกลุ่่ม/ช่อง และดาวนโหลดจาก X/Instagram/TikTok สู่กลุ่่มปลายทาง

## Setup

```bash
./setup.sh
```

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| API_ID | Telegram API ID |
| API_HASH | Telegram API Hash |
| BOT_TOKEN | Bot Token (from @BotFather) |
| DEST_GROUP_ID | กลุ่่ม/ช่องปลายทาง (admin + Topics) |
| ALLOWED_USER_IDS | **จำเป็น** — ID ผู้ใช้ที่มีสิทธิสั่งบอท (คั่นด้วย ,) ถ้าว่างบอทจะไม่ยอมเริ่มทำงาน |
| SEND_DELAY | ความล่าชั่้าระหว่่างส่่ง (sec) |
| PARALLEL_WORKERS | จำนวน worker ซ้อนกั่่น |
| MAX_SCAN_MESSAGES | จำนวน media ใหม่สูงสุดต่อการรัน 1 ครั้ง (default: 10000) ส่งลิงก์เดิมซ้ำเพื่อทำต่อ |
| DOWNLOAD_DIR | โฟลเดอร์ดาวนโหลดชั่่วคราว |

## Cookies

| ไฟล์ | วัตถุประสงค้ |
|------|------------|
| cookies_ig.txt | Instagram (บังคับ) |
| cookies_x.txt | X/Twitter (สำหร่ับ Private) |
| cookies_tiktok.txt | TikTok (ถ้้าโดนบล็อดก) |

Export: ใช้ extension [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/cclelndahbckbenkjhdbglgdgul_fromc) ใน Chrome → login → Save

## Usage

สั่่งลื่อก/ข้้อนความเข้้าบอท (private chat เท่่านั้่น):

| สั่่ง | ตั่ัวอย่่าง |
|------|---------|
| Clone group | `https://t.me/username` หรืือ `123456789` |
| Clone private | `https://t.me/c/12345/67890` |
| Single msg | `https://t.me/username/12345` |
| X/Twitter | `https://x.com/user/status/123...` |
| Instagram | `https://instagram.com/p/xxxxx` |
| TikTok | `https://tiktok.com/@user/video/123...` |
| Cancel | `/cancel` หรืือ `/cancel <source>` |
| Jobs | `/jobs` |
| Status | `/ping` |
วางลิงก์ปุ๊บ บอทโคลนให้อัตโนมัติ! โครงสร้างใหม่แบบ Clean Architecture ดูแลรักษาง่ายกว่าเดิม

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
