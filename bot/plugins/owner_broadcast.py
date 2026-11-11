import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from bot.config import Config
from helpers import broadcast as bc

log = logging.getLogger(__name__)


@Client.on_message(filters.command("broadcast") & filters.private)
async def cmd_broadcast(client, message):
    """/broadcast  (then send the message)   or   reply to any message with /broadcast"""
    if message.from_user.id not in Config.owner:
        return
    await bc.begin_broadcast(client, message.chat, message.from_user.id, source_msg=message.reply_to_message)


@Client.on_callback_query(filters.regex(r"^bc_t_(.+)_(\d+)$"))
async def cb_timer(client, query):
    if query.from_user.id not in Config.owner:
        return await query.answer("⛔ Owner only.", show_alert=True)
    draft_id, secs = query.matches[0].group(1), int(query.matches[0].group(2))
    draft = bc.get_draft(draft_id)
    if not draft:
        return await query.answer("Draft expired.", show_alert=True)
    draft["delete_secs"] = secs
    total = await client.db.total_users_count()
    try:
        await query.message.edit_reply_markup(bc.controls_for(total, draft_id))
    except MessageNotModified:
        pass
    await query.answer()


@Client.on_callback_query(filters.regex(r"^bc_go_(.+)$"))
async def cb_go(client, query):
    if query.from_user.id not in Config.owner:
        return await query.answer("⛔ Owner only.", show_alert=True)
    draft = bc.pop_draft(query.matches[0].group(1))
    if not draft:
        return await query.answer("Draft expired or already sent.", show_alert=True)
    await query.answer("🚀 Broadcast started")
    await query.message.edit_text("🚀 Broadcast started…")
    asyncio.create_task(bc.run_broadcast(client, query.message.chat, draft))


@Client.on_callback_query(filters.regex(r"^bc_x_(.+)$"))
async def cb_cancel(client, query):
    if query.from_user.id not in Config.owner:
        return await query.answer("⛔ Owner only.", show_alert=True)
    bc.pop_draft(query.matches[0].group(1))
    await query.message.edit_text("❌ Broadcast cancelled.")


@Client.on_callback_query(filters.regex(r"^bc_stop_(.+)$"))
async def cb_stop(client, query):
    if query.from_user.id not in Config.owner:
        return await query.answer("⛔ Owner only.", show_alert=True)
    ok = bc.request_stop(query.matches[0].group(1))
    await query.answer("🛑 Stopping…" if ok else "Already finished.", show_alert=not ok)
