"""
Owner Dashboard: Follow-up message (after N days) and Bye message settings.
Everything is set from inside the bot - no code / env changes needed.
"""
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import MessageNotModified

from bot.config import Config
from helpers.str_to_buttons import string_to_buttons
from helpers.utils import format_welcome_text
from helpers.followup_helper import DEFAULT_FOLLOWUP_TEXT, DEFAULT_BYE_TEXT, is_followup_enabled

log = logging.getLogger(__name__)

MAX_TEXT = 3500

KINDS = {
    "fu": {
        "title": "⏰ Follow-up Message",
        "prefix": "followup",
        "default": DEFAULT_FOLLOWUP_TEXT,
        "default_enabled": lambda db: is_followup_enabled(db),
        "blurb": lambda: f"Sent automatically **{Config.followup_days:g} day(s)** after a user is approved & welcomed.",
    },
    "bye": {
        "title": "👋 Bye Message",
        "prefix": "bye",
        "default": DEFAULT_BYE_TEXT,
        "default_enabled": None,
        "blurb": lambda: "Sent when a member **leaves** one of your connected channels/groups (only if they started the bot).",
    },
}


async def _enabled(db, kind):
    cfg = KINDS[kind]
    if kind == "fu":
        return await is_followup_enabled(db)
    return bool(await db.get_setting(f"{cfg['prefix']}_enabled", False))


async def _render(client, kind):
    cfg = KINDS[kind]
    db = client.db
    enabled = await _enabled(db, kind)
    text = await db.get_setting(f"{cfg['prefix']}_text")
    buttons_raw = await db.get_setting(f"{cfg['prefix']}_buttons")
    is_custom = bool(text)
    shown = text or cfg["default"]

    extra = ""
    if kind == "fu":
        extra = f"\n⏳ **Pending follow-ups:** `{await db.followups.count_documents({})}`"

    body = (
        f"{cfg['title']}\n\n"
        f"{cfg['blurb']()}\n\n"
        f"🔌 **Status:** {'🟢 ON' if enabled else '🔴 OFF'}\n"
        f"📝 **Text:** {'✏️ Custom' if is_custom else '📄 Default preset'}"
        f"{extra}\n\n"
        f"**Current message:**\n{shown}\n\n"
        f"🔘 **Buttons:** `{buttons_raw or 'None'}`"
    )
    short = kind
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Turn OFF" if enabled else "🟢 Turn ON", callback_data=f"owner_{short}_toggle")],
        [
            InlineKeyboardButton("✏️ Edit Message", callback_data=f"owner_{short}_edit"),
            InlineKeyboardButton("🧪 Send Test", callback_data=f"owner_{short}_test"),
        ],
        [InlineKeyboardButton("♻️ Reset to Default", callback_data=f"owner_{short}_reset")],
        [InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")],
    ])
    return body, markup


async def _show(client, query, kind):
    body, markup = await _render(client, kind)
    try:
        await query.message.edit_text(body, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception:
        # Markdown in the owner's text could be invalid -> show it without parsing
        try:
            await query.message.edit_text(body, reply_markup=markup, parse_mode=None, disable_web_page_preview=True)
        except MessageNotModified:
            pass


@Client.on_callback_query(filters.regex(r"^owner_(fu|bye)(?:_(toggle|edit|reset|test))?$"))
async def callback_owner_extras(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    kind, action = query.matches[0].group(1), query.matches[0].group(2)
    cfg = KINDS[kind]
    prefix = cfg["prefix"]
    db = client.db

    if action == "toggle":
        now = await _enabled(db, kind)
        await db.set_setting(f"{prefix}_enabled", not now)
        await query.answer("🟢 Turned ON" if not now else "🔴 Turned OFF")

    elif action == "reset":
        await db.set_setting(f"{prefix}_text", None)
        await db.set_setting(f"{prefix}_buttons", None)
        await query.answer("♻️ Reset to default preset", show_alert=True)

    elif action == "test":
        from helpers.approver_helper import _send_dm_safe
        text = await db.get_setting(f"{prefix}_text") or cfg["default"]
        buttons = await db.get_setting(f"{prefix}_buttons")
        formatted = format_welcome_text(text, query.from_user.first_name, "Sample Channel", query.from_user.id)
        markup = await string_to_buttons(buttons, include_mandatory=True)
        sent, _ = await _send_dm_safe(client, query.from_user.id, formatted, markup)
        await query.answer("🧪 Test message sent!" if sent else "⚠️ Could not send test message", show_alert=not sent)
        return

    elif action == "edit":
        await _edit_flow(client, query, kind)
        return

    await _show(client, query, kind)


async def _edit_flow(client, query, kind):
    cfg = KINDS[kind]
    prefix = cfg["prefix"]
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"owner_{kind}")]])

    await query.message.edit_text(
        f"📝 **Set {cfg['title']}**\n\n"
        f"Send the message text.\n\n"
        f"**Tags you can use:**\n"
        f"• `{{name}}` — user's first name\n"
        f"• `{{chat}}` — channel title\n"
        f"• `{{mention}}` — clickable mention\n"
        f"• `{{id}}` — user ID\n\n"
        f"*(Send /cancel to abort)*",
        reply_markup=back
    )
    try:
        resp = await query.message.chat.ask("⏳ Waiting for the message text...", timeout=180)
        raw = (resp.text or resp.caption or "").strip()
        if not raw or raw.lower() in ("/cancel", "/exit"):
            await query.message.reply("❌ Cancelled.", reply_markup=back)
            return
        if len(raw) > MAX_TEXT:
            await query.message.reply(f"⚠️ Too long ({len(raw)} chars). Max is {MAX_TEXT}.", reply_markup=back)
            return

        btn_resp = await query.message.chat.ask(
            "🔘 Now send buttons:\n`Button 1 - https://t.me/example | Button 2 - https://t.me/example`\n\n"
            "Send `/skip` for no buttons, `/cancel` to abort.",
            timeout=180
        )
        raw_btn = (btn_resp.text or "").strip()
        if raw_btn.lower() in ("/cancel", "/exit"):
            await query.message.reply("❌ Cancelled.", reply_markup=back)
            return
        buttons_text = None if raw_btn.lower() in ("/skip", "/pass", "skip", "none", "no", "") else raw_btn

        await client.db.set_setting(f"{prefix}_text", raw)
        await client.db.set_setting(f"{prefix}_buttons", buttons_text)
        await client.db.set_setting(f"{prefix}_enabled", True)  # saving a message switches the feature ON

        preview = format_welcome_text(raw, query.from_user.first_name, "Sample Channel", query.from_user.id)
        markup = await string_to_buttons(buttons_text, include_mandatory=True)
        await query.message.reply(f"✅ **{cfg['title']} saved & turned ON!** 🎉\n\n👇 Preview:", reply_markup=back)
        try:
            await query.message.reply(preview, reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            await query.message.reply(preview, parse_mode=None, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        log.error(f"owner {kind} edit flow failed: {e}")
        await query.message.reply("⚠️ Timed out or failed. Please try again.", reply_markup=back)
