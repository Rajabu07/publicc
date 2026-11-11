import logging
log = logging.getLogger(__name__)

from ..config import Config
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, ChatWriteForbidden


@Client.on_message(filters.private, group=-1)
async def force_sub(client, message):
    user  = await client.db.is_user_exist(message.from_user.id)
    if not user:
        await client.db.add_user(message.from_user.id)
        if Config.log_channel:
            try:
                await client.send_message(
                    int(Config.log_channel),
                    f"#NEW_USER: \n\nNew User {message.from_user.mention} started @{client.me.username} 😊"
                )
            except Exception as e:
                log.error(e)

    if not Config.force_sub: 
        return
        
    try:
        try:
            await client.get_chat_member(Config.force_sub, message.from_user.id)
        except UserNotParticipant:
            if isinstance(Config.force_sub, str) and not Config.force_sub.startswith("-"):
                link = f"https://t.me/{Config.force_sub}"
            else:
                chat_info = await client.get_chat(Config.force_sub)
                link = chat_info.invite_link
            try:
                await message.reply(
                    f"You must join [this channel]({link}) to use me. After joining try again !",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✨ Join Channel ✨", url=link)]]),
                    quote=True
                )
                await message.stop_propagation()
            except ChatWriteForbidden:
                pass
    except ChatAdminRequired:
        log.error(f"I'm not admin in the force sub channel: {Config.force_sub} !")