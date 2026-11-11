from pyrogram import Client, filters


@Client.on_message(filters.command('about') & filters.private & filters.incoming)
async def about(client, message):
    text = '''🤖 **Hello, I'm the Auto Approve Join Request Bot** ✨

⚡ I automatically approve Join Requests for your Channels and Groups.

💌 Send custom welcome DMs with 🖼 media & 🔘 buttons

Made with ❤️ — enjoy! 😊
'''
    await message.reply(text, quote=True)
