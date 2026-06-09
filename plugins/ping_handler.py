from pyrogram import Client, filters

@Client.on_message(filters.command("ping") & filters.private)
async def ping_command(client: Client, message):
    await message.reply("🏓 **Pong!** บอทออนไลน์และพร้อมใช้งานปกติครับ")