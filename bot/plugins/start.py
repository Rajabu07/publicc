import logging
from pyrogram import Client, filters
from bot.ui import home_text, home_markup

log = logging.getLogger(__name__)


@Client.on_message(filters.command("start") & filters.private & filters.incoming)
async def start(client, message):
    try:
        user_id = message.from_user.id
        user_name = message.from_user.first_name or "Friend"
        bot_username = getattr(getattr(client, 'me', None), 'username', '') or 'bot'

        if not await client.db.is_user_exist(user_id):
            await client.db.add_user(user_id)

        await message.reply(
            text=home_text(user_name),
            reply_markup=home_markup(bot_username, user_id),
            quote=True
        )
    except Exception as e:
        log.exception(e)
