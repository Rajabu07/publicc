import logging
from pyrogram import Client, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)


@Client.on_chat_member_updated()
async def auto_channel_connect(client, chat_member_updated):
    """
    Automatically detects when the bot is added to a Channel or Group as Admin.
    Zero-command setup: registers channel to the user and messages them in PM.
    """
    new_member = chat_member_updated.new_chat_member
    old_member = chat_member_updated.old_chat_member
    
    # Check if this update is about our bot
    if not new_member or new_member.user.id != client.me.id:
        return

    # Check if bot became an administrator
    if new_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
        chat = chat_member_updated.chat
        admin_user = chat_member_updated.from_user

        if not admin_user:
            return

        chat_id = chat.id
        chat_title = chat.title or "Channel"

        # Generate invite link if possible
        invite_link = None
        try:
            invite = await client.create_chat_invite_link(chat_id)
            invite_link = invite.invite_link
        except Exception:
            clean_id = str(chat_id).replace("-100", "")
            invite_link = f"https://t.me/c/{clean_id}/1"

        # Save to database with admin_id attached
        await client.db.new_channel(
            channel_id=chat_id,
            admin_id=admin_user.id,
            title=chat_title,
            link=invite_link
        )
        log.info(f"Bot connected to {chat_title} ({chat_id}) by user {admin_user.first_name} ({admin_user.id})")

        # Notify admin in private chat with inline action buttons
        buttons = [
            [
                InlineKeyboardButton("✏️ Set Welcome Message", callback_data=f"set_wc_{chat_id}"),
                InlineKeyboardButton("⚡ Accept Old Requests", callback_data=f"start_old_req_{chat_id}")
            ],
            [
                InlineKeyboardButton("⚙️ Change Mode (Auto/Manual)", callback_data=f"toggle_mode_{chat_id}"),
                InlineKeyboardButton("📢 All My Channels", callback_data="user_channels")
            ]
        ]

        try:
            await client.send_message(
                chat_id=admin_user.id,
                text=(
                    f"🎉 **Channel Successfully Connected!**\n\n"
                    f"📢 **Title:** [{chat_title}]({invite_link})\n"
                    f"🆔 **ID:** `{chat_id}`\n"
                    f"⚙️ **Default Mode:** `Auto-Approve`\n\n"
                    f"You don't need to type any commands! Use the buttons below to manage this channel:"
                ),
                reply_markup=InlineKeyboardMarkup(buttons),
                disable_web_page_preview=True
            )
        except Exception as e:
            log.warning(f"Could not PM admin {admin_user.id}: {e}")

    # Check if bot was removed, kicked, or demoted from administrator
    elif new_member.status in [enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.RESTRICTED, enums.ChatMemberStatus.MEMBER]:
        chat_id = chat_member_updated.chat.id
        await client.db.delete_channel(chat_id)
        log.info(f"Bot was removed or demoted from channel {chat_id}, removed from database.")
