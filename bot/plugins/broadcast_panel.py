import logging
from pyrogram import Client, filters

from bot.config import Config
from helpers.broadcast import (
    pop_draft, run_broadcast, request_cancel, send_broadcast_preview, is_running
)
from helpers.utils import spawn

log = logging.getLogger(__name__)


@Client.on_callback_query(filters.regex(r"^bc_go_(\w+)$"))
async def cb_broadcast_go(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Owner only.", show_alert=True)
        return
    if is_running():
        await query.answer("⏳ A broadcast is already running.", show_alert=True)
        return

    msg = pop_draft(query.matches[0].group(1))
    if msg is None:
        await query.answer("⚠️ Draft expired. Please create the broadcast again.", show_alert=True)
        return

    await query.answer("🚀 Starting broadcast...")
    try:
        await query.message.edit_text("🚀 **Starting broadcast...**")
    except Exception:
        pass
    # Run in background so this callback returns immediately
    spawn(run_broadcast(client, msg, query.message.chat.id), name="broadcast")


@Client.on_callback_query(filters.regex(r"^bc_cancel_(\w+)$"))
async def cb_broadcast_cancel(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Owner only.", show_alert=True)
        return
    pop_draft(query.matches[0].group(1))
    await query.message.edit_text("❌ Broadcast cancelled. Nothing was sent.")


@Client.on_callback_query(filters.regex(r"^bc_stop_(\w+)$"))
async def cb_broadcast_stop(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Owner only.", show_alert=True)
        return
    if request_cancel(query.matches[0].group(1)):
        await query.answer("🛑 Stopping after the current batch...", show_alert=True)
    else:
        await query.answer("ℹ️ This broadcast already finished.", show_alert=True)


@Client.on_message(filters.command("broadcast") & filters.private)
async def cmd_broadcast(client, message):
    if message.from_user.id not in Config.owner:
        return
    if not message.reply_to_message:
        await message.reply(
            "📣 **Broadcast**\n\nReply to any message (text, photo, video, forward...) with `/broadcast`.\n"
            "Or use **👑 Owner Dashboard → 📢 Broadcast Message**.",
            quote=True
        )
        return
    await send_broadcast_preview(client, message.chat.id, message.reply_to_message)
