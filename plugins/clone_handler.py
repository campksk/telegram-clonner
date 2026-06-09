import asyncio
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from config import DESTINATION_GROUP_ID
from core import user_app 

@Client.on_message(filters.command("clone") & filters.private)
async def clone_command(client: Client, message):
    if len(message.command) < 2:
        await message.reply("⚠️ กรุณาพิมพ์คำสั่งในรูปแบบ:\n`/clone [Chat ID หรือ Username ของกลุ่มต้นทาง]`")
        return
    
    source_chat_id = message.command[1]
    
    # 🌟 [เพิ่มใหม่] เช็กว่าถ้าผู้ใช้ลืมใส่เครื่องหมายลบ ให้บอทเติมให้เองอัตโนมัติ
    if source_chat_id.isdigit() and source_chat_id.startswith("100"):
        source_chat_id = f"-{source_chat_id}"
    
    try:
        source_chat_id = int(source_chat_id)
    except ValueError:
        pass 

    # บอทตอบรับทันที
    status_msg = await message.reply("⏳ บอทได้รับคำสั่งแล้ว กำลังตรวจสอบกลุ่มต้นทาง...")

    try:
        # ให้ User API ดึงชื่อกลุ่มต้นทาง
        chat_info = await user_app.get_chat(source_chat_id)
        group_name = chat_info.title or "โคลนกลุ่มที่ไม่ทราบชื่อ"

        await status_msg.edit_text(f"🔍 เจอกลุ่ม: **{group_name}**\n🏗️ กำลังสร้าง Topic ใหม่ในกลุ่มปลายทาง...")

        # ให้ Bot สร้าง Topic
        new_topic = await client.create_forum_topic(DESTINATION_GROUP_ID, title=group_name)
        topic_id = new_topic.id

        await status_msg.edit_text(f"✅ สร้าง Topic: **{group_name}** สำเร็จ!\n🔄 กำลังเริ่มดึงประวัติแชทและคัดลอกไฟล์...")

        cloned_count = 0
        async for msg in user_app.get_chat_history(source_chat_id):
            if msg.media:
                try:
                    await user_app.copy_message(
                        chat_id=DESTINATION_GROUP_ID,
                        from_chat_id=source_chat_id,
                        message_id=msg.id,
                        reply_to_message_id=topic_id
                    )
                    cloned_count += 1
                    
                    if cloned_count % 5 == 0:
                        await status_msg.edit_text(
                            f"📂 กำลังโคลนข้อมูลจากกลุ่ม: **{group_name}**\n"
                            f"⚡ คัดลอกสำเร็จแล้ว: `{cloned_count}` รายการ\n"
                            f"⏳ กรุณารอสักครู่..."
                        )
                        
                    await asyncio.sleep(1.5)
                    
                except FloodWait as e:
                    await status_msg.edit_text(f"⚠️ บอทส่งข้อความเร็วเกินไป ต้องรอระบบ Cool down `{e.value}` วินาที...")
                    await asyncio.sleep(e.value)
                except Exception:
                    pass

        await status_msg.edit_text(
            f"🎉 **โคลนข้อมูลเสร็จสิ้นเรียบร้อยแล้ว!**\n"
            f"🎯 กลุ่มต้นทาง: {group_name}\n"
            f"📦 คัดลอก Media ทั้งหมดไปได้: `{cloned_count}` รายการ"
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ เกิดข้อผิดพลาด:\n`{e}`\n\n💡 บันทึก: ตรวจสอบให้มั่นใจว่าใส่ ID กลุ่มถูกต้องและคุณอยู่ในกลุ่มนั้นแล้ว")