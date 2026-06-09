import asyncio
from core import bot_app, user_app
from config import DESTINATION_GROUP_ID

async def main():
    print("🚀 Starting Bot and User Client...")
    
    # สตาร์ทระบบทั้งหน้าบ้านและหลังบ้าน
    await bot_app.start()
    await user_app.start()
    
    try:
        # บังคับให้ระบบดึงข้อมูลกลุ่มเป้าหมายมาเก็บไว้ใน Cache ทันที
        await bot_app.get_chat(DESTINATION_GROUP_ID)
        await user_app.get_chat(DESTINATION_GROUP_ID)
        print("✅ ซิงค์ฐานข้อมูลกลุ่มเป้าหมายสำเร็จ!")
    except Exception as e:
        print(f"⚠️ คำเตือน: ยังไม่รู้จักกลุ่มเป้าหมาย ({e}) ให้ลองพิมพ์แชทในกลุ่มดูนะ")

    print("🤖 ระบบพร้อมใช้งานแล้ว! กด Ctrl+C เพื่อหยุดการทำงาน")
    
    # 🌟 เปลี่ยนมาใช้ Infinite Loop แบบปกติแทน idle() ของ Pyrogram เพื่อเลี่ยงบั๊กบน Windows
    try:
        while True:
            await asyncio.sleep(3600)  # ให้ระบบนอนรอรับ Event ทุกๆ 1 ชั่วโมงแบบวนลูปไปเรื่อยๆ
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n🛑 กำลังปิดระบบอย่างปลอดภัย...")
    finally:
        # เคลียร์และปิดสัญญานการเชื่อมต่ออย่างถูกต้อง
        await bot_app.stop()
        await user_app.stop()
        print("👋 ปิดระบบเรียบร้อยแล้ว!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass