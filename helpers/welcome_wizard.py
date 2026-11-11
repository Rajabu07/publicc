import logging
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from helpers.welcome_helper import extract_welcome, has_content
from helpers.approver_helper import _deliver_welcome

log = logging.getLogger(__name__)

_CANCEL = {"/cancel", "/exit"}
_SKIP = {"/pass", "/skip", "none", "skip", "no"}

TAGS_HELP = (
    "**Placeholders:** `{name}`, `{chat}`, `{mention}`, `{id}`\n"
    "**Formatting** (bold, italic, links, spoiler…) is kept as you send it."
)

BUTTONS_HELP = (
    "Now send the **buttons** (as many as you like):\n\n"
    "`Button 1 - https://t.me/a | Button 2 - https://t.me/b`\n"
    "`Button 3 - https://example.com`\n\n"
    "• `|` puts buttons side by side in one row\n"
    "• a new line starts a new row\n\n"
    "Send `/pass` to skip buttons, `/cancel` to abort."
)


async def run_welcome_wizard(client, query, chat_id: int, title: str, back_markup):
    """
    Interactive flow shared by per-channel welcome (chat_id != 0) and the owner's global welcome (chat_id == 0).
    Step 1: send text OR photo/video/GIF/document (with caption).
    Step 2: send buttons (multiple rows/columns) or /pass.
    """
    chat = query.message.chat
    try:
        resp = await chat.ask(
            "📝 Send the welcome message now.\n\n"
            "Supported: **text**, **photo**, **video**, **GIF**, or **file** with caption.\n\n" + TAGS_HELP +
            "\n\n*(Send /cancel to abort)*",
            timeout=300
        )
        if resp.text and resp.text.strip().lower() in _CANCEL:
            await query.message.reply("❌ Cancelled.", reply_markup=back_markup)
            return

        wc = extract_welcome(resp)
        if not has_content(wc):
            await query.message.reply(
                "⚠️ I need text, or a photo/video/GIF/file. Setup aborted.", reply_markup=back_markup
            )
            return

        buttons_resp = await chat.ask(BUTTONS_HELP, timeout=300)
        raw_buttons = (buttons_resp.text or buttons_resp.caption or "").strip()
        if raw_buttons.lower() in _CANCEL:
            await query.message.reply("❌ Cancelled.", reply_markup=back_markup)
            return
        buttons_text = None if (not raw_buttons or raw_buttons.lower() in _SKIP) else raw_buttons

        await client.db.update_welcome(
            chat_id, wc["text"], buttons_text, wc["media_type"], wc["media_file_id"], "html"
        )

        saved = dict(wc, buttons=buttons_text)
        user = query.from_user
        await query.message.reply(
            "✅ **Welcome saved!** Here's exactly what new members will receive:",
            reply_markup=back_markup
        )
        sent, _ = await _deliver_welcome(
            client, user.id, saved, (user.first_name or "Friend"), title or "Sample Channel"
        )
        if not sent:
            await query.message.reply("⚠️ Saved, but I couldn't render the preview here (check the formatting/buttons).")

    except Exception as e:
        log.error(f"Error in welcome wizard: {e}", exc_info=True)
        await query.message.reply("⚠️ Setup timed out or failed. Please try again.", reply_markup=back_markup)
