import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from helpers.old_request_helper import approve_batch_requests, issue_suffix

log = logging.getLogger(__name__)


# ==========================================
# 1. CHANNEL COMMANDS: /accept100, /approve, /acceptall
# ==========================================

@Client.on_message(filters.command(["accept100", "acceptall"]) & filters.channel)
async def channel_accept_all(client, message):
    chat_id = message.chat.id
    status_msg = await message.reply("⏳ Processing approval queue & sending welcome messages...")
    
    count = 100 if message.command[0] == "accept100" else 500
    approved = await approve_batch_requests(client, chat_id, count=count)

    if approved > 0:
        await status_msg.edit(f"✅ Successfully approved **{approved}** requests. Userbot has left the channel." + issue_suffix(chat_id))
    else:
        await status_msg.edit("ℹ️ No pending join requests were approved. Userbot has left the channel." + (issue_suffix(chat_id) or " None found for this channel."))


@Client.on_message(filters.command("approve") & filters.channel)
async def channel_approve_custom(client, message):
    chat_id = message.chat.id
    
    if len(message.command) < 2:
        await message.reply("Usage: `/approve <count>` (e.g. `/approve 50` or `/approve 200`)")
        return

    try:
        count = int(message.command[1])
        if count <= 0:
            raise ValueError()
    except ValueError:
        await message.reply("⚠️ Please provide a valid positive number: `/approve 50`")
        return

    status_msg = await message.reply(f"⏳ Approving up to **{count}** requests... Userbot will leave once finished.")
    approved = await approve_batch_requests(client, chat_id, count=count)

    if approved > 0:
        await status_msg.edit(f"✅ Successfully approved **{approved}** requests. Userbot has left the channel." + issue_suffix(chat_id))
    else:
        await status_msg.edit("ℹ️ No pending join requests were approved. Userbot has left the channel." + (issue_suffix(chat_id) or " None found for this channel."))




@Client.on_callback_query(filters.regex(r"^do_appr_(-?\d+)_(\d+)$"))
async def callback_do_approve(client, query):
    chat_id = int(query.matches[0].group(1))
    count = int(query.matches[0].group(2))

    await query.answer("⏳ Processing approval queue...", show_alert=False)
    await query.message.edit_text(f"⏳ Approving up to **{count}** requests for `{chat_id}`...\nUserbot is approving requests and will leave automatically when finished...")

    approved = await approve_batch_requests(client, chat_id, count=count)

    buttons = [[InlineKeyboardButton("🔙 Back to Channel", callback_data=f"manage_ch_{chat_id}")]]

    if approved > 0:
        await query.message.edit_text(
            f"✅ **Batch Approval Completed!**\n\n"
            f"• **Channel:** `{chat_id}`\n"
            f"• **Approved & Welcomed:** `{approved}` users\n"
            f"• **Userbot Status:** 🚪 Left the channel and admin rights removed.\n\n"
            f"Welcome DMs are sent where Telegram allows it." + issue_suffix(chat_id),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await query.message.edit_text(
            f"ℹ️ No requests were approved for channel `{chat_id}`.\n"
            f"• **Userbot Status:** 🚪 Left the channel and admin rights removed." + (issue_suffix(chat_id) or "\nAll incoming requests are currently up to date!"),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
