import logging
import math
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import (
    MessageNotModified, UserNotParticipant,
    ChatAdminRequired, ChannelPrivate,
    ChannelInvalid, ChatInvalid, PeerIdInvalid,
    FloodWait
)
from helpers.str_to_buttons import string_to_buttons
from helpers.old_request_helper import (
    get_channel_pending_count,
    remove_userbot_from_channel,
    ensure_userbot_admin,
    get_last_issue,
    approve_batch_requests,
    issue_suffix
)
from helpers.approver_helper import format_welcome_text, PREVIEW_OPTIONS, _send_dm_safe
from helpers.utils import extract_media, MEDIA_LABELS
from bot.ui import home_text, home_markup
from bot.config import Config

log = logging.getLogger(__name__)


async def verify_channel_status(client, chat_id, user_id=None):
    """
    Verifies that the bot is still an active administrator in chat_id,
    and (for non-owners) that user_id is still an administrator/owner.
    Returns: (is_valid: bool, updated_title: str or None)
    """
    try:
        cid = int(chat_id)
        if cid == 0:
            return False, None

        # Check bot's status in the channel
        bot_member = await client.get_chat_member(cid, client.me.id)
        if bot_member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return False, None

        # Check user's status in the channel (unless user is bot owner)
        if user_id and user_id not in Config.owner:
            try:
                user_member = await client.get_chat_member(cid, int(user_id))
                if user_member.status not in [enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR]:
                    return False, None
            except Exception:
                return False, None

        chat = await client.get_chat(cid)
        return True, chat.title

    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant, ChannelInvalid, ChatInvalid, PeerIdInvalid):
        return False, None
    except FloodWait as e:
        log.warning(f"FloodWait {e.value}s while checking channel {chat_id}")
        return True, None
    except Exception as e:
        err_str = str(e).upper()
        if any(x in err_str for x in ["CHAT_ADMIN_REQUIRED", "CHANNEL_PRIVATE", "USER_NOT_PARTICIPANT", "PEER_ID_INVALID", "CHAT_WRITE_FORBIDDEN"]):
            return False, None
        log.warning(f"Transient error verifying channel {chat_id}: {e}")
        return True, None


async def get_verified_user_channels(client, user_id):
    """
    Fetches user's channels from DB and verifies each one with Telegram.
    If a channel is no longer active (bot kicked/demoted or user not admin/channel deleted),
    it is automatically deleted from the database so old channels won't show.
    """
    raw_channels = await client.db.get_user_channels(user_id)
    if not raw_channels:
        return []

    async def check_channel(ch):
        cid = ch.get("channel_id")
        if not cid or cid == 0:
            return None
        is_valid, title = await verify_channel_status(client, cid, user_id)
        if not is_valid:
            # NOTE: We do NOT delete from database; we preserve the channel records
            return None
        if title and title != ch.get("title"):
            ch["title"] = title
            await client.db.update_channel(cid, title=title)
        return ch

    results = await asyncio.gather(*[check_channel(ch) for ch in raw_channels], return_exceptions=True)
    return [ch for ch in results if ch and not isinstance(ch, Exception)]


# ==========================================
# 1. LIST USER'S CONNECTED CHANNELS
# ==========================================
@Client.on_callback_query(filters.regex(r"^user_channels(?:_(\d+))?$"))
async def callback_user_channels(client, query):
    user_id = query.from_user.id
    page = int(query.matches[0].group(1)) if query.matches[0].group(1) else 1

    channels = await get_verified_user_channels(client, user_id)
    
    bot_username = client.me.username
    add_channel_url = Config.get_add_channel_url(bot_username)
    add_group_url = Config.get_add_group_url(bot_username)

    if not channels:
        buttons = [
            [
                InlineKeyboardButton("➕ Add to Channel", url=add_channel_url),
                InlineKeyboardButton("➕ Add to Group", url=add_group_url)
            ],
            [
                InlineKeyboardButton("🔙 Back Home", callback_data="back_home")
            ]
        ]
        await query.message.edit_text(
            "⚠️ **You have no connected channels yet.**\n\n"
            "To connect a channel or group, click below and add the bot as Admin with Ban Users & Add Admin permissions:\n"
            "*(The bot will automatically recognize you as the manager of that channel!)*",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    per_page = 8
    total_pages = max(1, math.ceil(len(channels) / per_page))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_channels = channels[start_idx:end_idx]

    buttons = []
    for ch in page_channels:
        cid = ch.get("channel_id")
        title = ch.get("title") or f"Channel {cid}"
        mode = ch.get("mode", "auto").capitalize()
        buttons.append([InlineKeyboardButton(f"📢 {title} [{mode}]", callback_data=f"manage_ch_{cid}")])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"user_channels_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop_pages"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"user_channels_{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton("➕ Add Channel", url=add_channel_url),
        InlineKeyboardButton("➕ Add Group", url=add_group_url)
    ])
    buttons.append([InlineKeyboardButton("🔙 Back Home", callback_data="back_home")])

    try:
        await query.message.edit_text(
            f"📢 **Your Connected Channels & Groups (Page {page}/{total_pages}):**\nSelect a channel below to configure welcome messages, buttons, or approval mode:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass


# ==========================================
# 2. MANAGE SPECIFIC CHANNEL MENU
# ==========================================
@Client.on_callback_query(filters.regex(r"^manage_ch_(-?\d+)$"))
async def callback_manage_channel(client, query):
    chat_id = int(query.matches[0].group(1))

    # Verify channel is still active and bot is admin
    is_valid, title = await verify_channel_status(client, chat_id, query.from_user.id)
    if not is_valid:
        await query.answer("⚠️ Bot is currently not an admin or cannot access this channel.", show_alert=False)

    channel = await client.db.get_channel(chat_id)
    
    title = title or (channel.get("title") if channel else f"Channel {chat_id}")
    mode = channel.get("mode", "auto") if channel else "auto"
    mode_display = "🟢 Auto-Approve" if mode == "auto" else "🟡 Manual Approval"

    # Display whether custom welcome is set
    wc_text, _ = await client.db.get_welcome(chat_id)
    wc_display = "Custom" if (wc_text and wc_text.strip()) else "Preset"
    media_type, _media_id = await client.db.get_welcome_media(chat_id)
    media_display = MEDIA_LABELS.get(media_type, "None") if media_type else "None"

    buttons = [
        [
            InlineKeyboardButton(f"✉️ Welcome: [{wc_display}]", callback_data=f"set_wc_{chat_id}"),
            InlineKeyboardButton(f"Toggle Mode: {mode_display}", callback_data=f"toggle_mode_{chat_id}")
        ],
        [
            InlineKeyboardButton("⚡ Accept Old Requests", callback_data=f"start_old_req_{chat_id}"),
            InlineKeyboardButton("❌ Disconnect", callback_data=f"disconnect_ch_{chat_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back to Channels", callback_data="user_channels")
        ]
    ]

    if query.from_user.id in Config.owner:
        buttons.append([InlineKeyboardButton("👑 Back to Owner Panel", callback_data="owner_panel")])

    await query.message.edit_text(
        f"⚙️ **Channel Management:** **{title}**\n\n"
        f"• **Channel ID:** `{chat_id}`\n"
        f"• **Current Mode:** {mode_display}\n"
        f"• **Welcome Message:** `{wc_display}`\n"
        f"• **Welcome Media:** `{media_display}`\n\n"
        f"💡 *To inspect Telegram pending join requests and approve backlog, tap **⚡ Accept Old Requests** below.*",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@Client.on_callback_query(filters.regex(r"^disconnect_ch_(-?\d+)$"))
async def callback_disconnect_channel(client, query):
    chat_id = int(query.matches[0].group(1))
    await client.db.delete_channel(chat_id)
    await query.answer("✅ Channel disconnected and removed from your list.", show_alert=True)
    await callback_user_channels(client, query)


# ==========================================
# 3. TOGGLE AUTO / MANUAL MODE
# ==========================================
@Client.on_callback_query(filters.regex(r"^toggle_mode_(-?\d+)$"))
async def callback_toggle_mode(client, query):
    chat_id = int(query.matches[0].group(1))
    current_mode = await client.db.get_channel_mode(chat_id)
    new_mode = "manual" if current_mode == "auto" else "auto"
    await client.db.set_channel_mode(chat_id, new_mode)

    await query.answer(f"Switched mode to {new_mode.upper()}!", show_alert=False)
    await callback_manage_channel(client, query)


# ==========================================
# 4. INTERACTIVE WELCOME MESSAGE MANAGEMENT
# ==========================================
@Client.on_callback_query(filters.regex(r"^set_wc_(-?\d+)$"))
async def callback_view_welcome(client, query):
    chat_id = int(query.matches[0].group(1))
    channel = await client.db.get_channel(chat_id)
    title = (channel.get("title") if channel else None) or f"Channel {chat_id}"

    text, buttons_data = await client.db.get_welcome(chat_id)
    media_type, _media_id = await client.db.get_welcome_media(chat_id)
    media_line = f"🖼 **Welcome Media:** {MEDIA_LABELS.get(media_type, 'None')}" if media_type else "🖼 **Welcome Media:** None"

    media_row = [InlineKeyboardButton("🖼 Change Media" if media_type else "🖼 Add Photo/Video/GIF", callback_data=f"edit_wcm_{chat_id}")]
    if media_type:
        media_row.append(InlineKeyboardButton("🗑 Remove Media", callback_data=f"del_wcm_{chat_id}"))

    if text and text.strip():
        display_text = (
            f"✉️ **Custom Welcome for:** **{title}**\n\n"
            f"**Current Text:**\n{text}\n\n"
            f"**Attached Buttons:** `{buttons_data or 'None'}`\n"
            f"{media_line}\n\n"
            "💌 This message is automatically sent to users whose join requests are approved."
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit Welcome Message", callback_data=f"edit_wc_{chat_id}")],
            media_row,
            [InlineKeyboardButton("🗑️ Reset Text to Default", callback_data=f"del_wc_{chat_id}")],
            [InlineKeyboardButton("🔙 Back to Channel", callback_data=f"manage_ch_{chat_id}")]
        ])
    else:
        display_text = (
            f"✉️ **Welcome Message for:** **{title}**\n\n"
            "Currently, **no custom welcome text** is configured for this channel.\n"
            "The bot falls back to the **Global / Default Preset** text.\n"
            f"{media_line}\n\n"
            "💡 *A media-only welcome works too: the media is sent with the global text as caption.*"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set Custom Welcome", callback_data=f"edit_wc_{chat_id}")],
            media_row,
            [InlineKeyboardButton("🔙 Back to Channel", callback_data=f"manage_ch_{chat_id}")]
        ])

    try:
        await query.message.edit_text(display_text, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^del_wc_(-?\d+)$"))
async def callback_del_channel_welcome(client, query):
    chat_id = int(query.matches[0].group(1))
    await client.db.update_welcome(chat_id, None, None)
    await query.answer("✅ Custom welcome removed! Bot will use default preset.", show_alert=True)
    await callback_view_welcome(client, query)


@Client.on_callback_query(filters.regex(r"^edit_wc_(-?\d+)$"))
async def callback_set_welcome(client, query):
    chat_id = int(query.matches[0].group(1))
    channel = await client.db.get_channel(chat_id)
    title = (channel.get("title") if channel else None) or f"Channel {chat_id}"

    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Welcome Settings", callback_data=f"set_wc_{chat_id}")]])

    await query.message.edit_text(
        f"📝 **Setting Custom Welcome for:** **{title}**\n\n"
        f"Please send the message text you want newly approved members to receive.\n\n"
        f"**Available formatting tags:**\n"
        f"• `{{name}}` or `{{first_name}}` — User's name\n"
        f"• `{{chat}}` or `{{title}}` — Channel title\n"
        f"• `{{mention}}` — User mention link\n"
        f"• `{{id}}` — User's Telegram ID\n\n"
        f"*(Send /cancel to abort)*",
        reply_markup=back_markup
    )

    try:
        welcome_resp = await query.message.chat.ask("Waiting for your welcome text...", timeout=120)
        raw_welcome = (welcome_resp.text or welcome_resp.caption or "").strip()

        if not raw_welcome:
            await query.message.reply("⚠️ No text received. Setup aborted.", reply_markup=back_markup)
            return

        if raw_welcome.lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Cancelled welcome message setup.", reply_markup=back_markup)
            return

        welcome_text = raw_welcome

        ask_buttons_text = (
            "Now send the buttons to be attached in this format:\n\n"
            "`Button 1 - https://t.me/example | Button 2 - https://t.me/example`\n\n"
            "Send `/pass` or `/skip` if you do not want to add buttons.\n"
            "Send `/cancel` to abort."
        )
        buttons_resp = await query.message.chat.ask(ask_buttons_text, timeout=120)
        raw_buttons = (buttons_resp.text or buttons_resp.caption or "").strip()

        if raw_buttons.lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Cancelled welcome message setup.", reply_markup=back_markup)
            return

        if raw_buttons.lower() in ["/pass", "/skip", "none", "skip", "no"]:
            buttons_text = None
        else:
            buttons_text = raw_buttons

        await client.db.update_welcome(chat_id, welcome_text, buttons_text)
        
        user_name = (query.from_user.first_name if query.from_user else None) or "Friend"
        preview_markup = await string_to_buttons(buttons_text, include_mandatory=True)
        preview_text = format_welcome_text(welcome_text, user_name, title, query.from_user.id if query.from_user else 0)

        await query.message.reply(
            "✅ **Welcome Message Successfully Saved!**\n\n**Here is a preview of what new members will receive:**",
            reply_markup=back_markup
        )
        try:
            await query.message.reply(
                text=preview_text,
                reply_markup=preview_markup,
                **PREVIEW_OPTIONS
            )
        except Exception:
            await query.message.reply(
                text=preview_text,
                parse_mode=None,
                reply_markup=preview_markup,
                **PREVIEW_OPTIONS
            )

    except Exception as e:
        log.error(f"Error in interactive set welcome: {e}")
        await query.message.reply("⚠️ Setup timed out or failed. Please try again.", reply_markup=back_markup)


@Client.on_callback_query(filters.regex(r"^del_wcm_(-?\d+)$"))
async def callback_del_welcome_media(client, query):
    chat_id = int(query.matches[0].group(1))
    await client.db.clear_welcome_media(chat_id)
    await query.answer("🗑 Welcome media removed.", show_alert=True)
    await callback_view_welcome(client, query)


@Client.on_callback_query(filters.regex(r"^edit_wcm_(-?\d+)$"))
async def callback_set_welcome_media(client, query):
    chat_id = int(query.matches[0].group(1))
    channel = await client.db.get_channel(chat_id)
    title = (channel.get("title") if channel else None) or f"Channel {chat_id}"
    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Welcome Settings", callback_data=f"set_wc_{chat_id}")]])

    await query.message.edit_text(
        f"🖼 **Welcome Media for:** **{title}**\n\n"
        f"Send the **Photo, Video, GIF or Document** that new members should receive with the welcome message.\n\n"
        f"💡 Each channel can have its own media.\n\n"
        f"*(Send /cancel to abort)*",
        reply_markup=back_markup
    )

    try:
        resp = await query.message.chat.ask("⏳ Waiting for your media...", timeout=180)
        if resp.text and resp.text.strip().lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Cancelled.", reply_markup=back_markup)
            return

        media_type, file_id = extract_media(resp)
        if not file_id:
            await query.message.reply("⚠️ That is not a photo, video, GIF or document. Please try again.", reply_markup=back_markup)
            return

        await client.db.set_welcome_media(chat_id, media_type, file_id)

        wc_text, wc_buttons = await client.db.get_welcome(chat_id)
        if not (wc_text and wc_text.strip()):
            wc_text, wc_buttons = await client.db.get_global_welcome()
            if not (wc_text and wc_text.strip()):
                from helpers.approver_helper import PRESET_TEXT, PRESET_BUTTONS
                wc_text, wc_buttons = PRESET_TEXT, PRESET_BUTTONS

        user_name = (query.from_user.first_name if query.from_user else None) or "Friend"
        preview_text = format_welcome_text(wc_text, user_name, title, query.from_user.id)
        preview_markup = await string_to_buttons(wc_buttons, include_mandatory=True)

        await query.message.reply(
            f"✅ **{MEDIA_LABELS.get(media_type, 'Media')} saved for {title}!** 🎉\n\n👇 Preview of the welcome DM:",
            reply_markup=back_markup
        )
        await _send_dm_safe(client, query.from_user.id, preview_text, preview_markup, media_type, file_id)

    except Exception as e:
        log.error(f"Error in set welcome media: {e}")
        await query.message.reply("⚠️ Setup timed out or failed. Please try again.", reply_markup=back_markup)


# ==========================================
# 5. DEDICATED BATCH APPROVE PANEL
# ==========================================
@Client.on_callback_query(filters.regex(r"^batch_panel(?:_(\d+))?$"))
async def callback_batch_panel(client, query):
    user_id = query.from_user.id
    page = int(query.matches[0].group(1)) if query.matches[0].group(1) else 1

    channels = await get_verified_user_channels(client, user_id)

    bot_username = client.me.username
    add_channel_url = Config.get_add_channel_url(bot_username)
    add_group_url = Config.get_add_group_url(bot_username)

    if not channels:
        buttons = [
            [
                InlineKeyboardButton("➕ Add to Channel", url=add_channel_url),
                InlineKeyboardButton("➕ Add to Group", url=add_group_url)
            ],
            [
                InlineKeyboardButton("🔙 Back Home", callback_data="back_home")
            ]
        ]
        await query.message.edit_text(
            "⚠️ **No channels connected yet.**\n\n"
            "Add the bot to your channel or group first using the buttons below:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    per_page = 8
    total_pages = max(1, math.ceil(len(channels) / per_page))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_channels = channels[start_idx:end_idx]

    buttons = []
    for ch in page_channels:
        cid = ch.get("channel_id")
        title = ch.get("title") or f"Channel {cid}"
        buttons.append([InlineKeyboardButton(f"⚡ {title}", callback_data=f"start_old_req_{cid}")])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"batch_panel_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop_pages"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"batch_panel_{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔙 Back Home", callback_data="back_home")])

    try:
        await query.message.edit_text(
            f"⚡ **Batch Approval Center (Page {page}/{total_pages}):**\n\n"
            "Select your channel below to connect the userbot, inspect pending requests, and approve backlog:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^(?:sel_batch_|start_old_req_)(-?\d+)$"))
async def callback_select_batch_count(client, query):
    chat_id = int(query.matches[0].group(1))
    channel = await client.db.get_channel(chat_id)
    title = channel.get("title") if channel else f"Channel {chat_id}"

    try:
        await query.message.edit_text(
            f"⏳ **Connecting Userbot to {title}...**\n\n"
            f"Adding Userbot as Administrator to inspect Telegram's pending join requests.\n"
            f"Please wait a few moments..."
        )
    except Exception:
        pass

    # 1. Connect userbot as admin
    is_admin = await ensure_userbot_admin(client, chat_id)
    if not is_admin:
        last_err = get_last_issue(chat_id) or "Could not add or promote userbot. Make sure this bot has 'Add New Admins' rights in your channel."
        buttons = [[InlineKeyboardButton("🔙 Back to Channel", callback_data=f"manage_ch_{chat_id}")]]
        await query.message.edit_text(
            f"⚠️ **Could Not Connect Userbot**\n\n"
            f"• **Channel:** **{title}** (`{chat_id}`)\n"
            f"• **Reason:** `{last_err}`\n\n"
            f"Please ensure the bot has permission to add admins, or check that your userbot session is active in the Owner Dashboard.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # 2. Query Telegram backlog count with allow_add_userbot=True
    pending = await get_channel_pending_count(client, chat_id, force_refresh=True, allow_add_userbot=True)
    all_count = pending if pending > 0 else 1000

    buttons = [
        [
            InlineKeyboardButton("⚡ Approve 50", callback_data=f"do_appr_{chat_id}_50"),
            InlineKeyboardButton("⚡ Approve 100", callback_data=f"do_appr_{chat_id}_100")
        ],
        [
            InlineKeyboardButton(f"⚡ Approve All ({pending})", callback_data=f"do_appr_{chat_id}_{all_count}"),
            InlineKeyboardButton("📝 Custom Amount", callback_data=f"custom_appr_{chat_id}")
        ],
        [
            InlineKeyboardButton("🚪 Leave & Back to Channel", callback_data=f"cancel_old_req_{chat_id}")
        ]
    ]

    await query.message.edit_text(
        f"⚡ **Accept Old Join Requests:** **{title}**\n\n"
        f"• **Channel ID:** `{chat_id}`\n"
        f"• **Pending Requests in Queue:** `{pending}`\n"
        f"• **Userbot Status:** 🟢 Temporarily Connected as Admin\n\n"
        f"Select how many requests to approve.\n"
        f"*(Userbot will automatically leave the channel after approving)*",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@Client.on_callback_query(filters.regex(r"^cancel_old_req_(-?\d+)$"))
async def callback_cancel_old_req(client, query):
    chat_id = int(query.matches[0].group(1))
    await query.answer("🚪 Removing userbot from channel...", show_alert=False)
    try:
        await remove_userbot_from_channel(client, chat_id)
    except Exception as e:
        log.warning(f"Error removing userbot on cancel for {chat_id}: {e}")
    await callback_manage_channel(client, query)


@Client.on_callback_query(filters.regex(r"^custom_appr_(-?\d+)$"))
async def callback_custom_approve(client, query):
    chat_id = int(query.matches[0].group(1))

    await query.message.edit_text(
        "📝 **Enter Custom Approval Count:**\n\n"
        "Send the number of requests you want to approve (e.g. `75` or `300`):\n\n"
        "*(Send /cancel to abort and remove userbot)*"
    )

    try:
        resp = await query.message.chat.ask("Waiting for count...", timeout=60)
        txt = resp.text.strip()
        if txt.lower() in ["/cancel", "/exit"]:
            await remove_userbot_from_channel(client, chat_id)
            await query.message.reply("❌ Cancelled custom batch approval. Userbot has left the channel.")
            return

        if not txt.isdigit() or int(txt) <= 0:
            await remove_userbot_from_channel(client, chat_id)
            await query.message.reply("⚠️ Invalid number. Custom approval cancelled. Userbot has left the channel.")
            return

        count = int(txt)
        status_msg = await query.message.reply(f"⏳ Approving up to **{count}** requests for `{chat_id}`...\nUserbot will leave once finished.")
        approved = await approve_batch_requests(client, chat_id, count=count)

        buttons = [[InlineKeyboardButton("🔙 Back to Channel", callback_data=f"manage_ch_{chat_id}")]]
        if approved > 0:
            await status_msg.edit(
                f"✅ **Batch Approval Completed!**\n\n"
                f"• **Channel:** `{chat_id}`\n"
                f"• **Approved & Welcomed:** `{approved}` users\n"
                f"• **Userbot Status:** 🚪 Left the channel and admin rights removed.\n\n"
                f"Welcome DMs are sent where Telegram allows it." + issue_suffix(chat_id),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await status_msg.edit(
                f"ℹ️ No requests were approved for channel `{chat_id}`.\n"
                f"• **Userbot Status:** 🚪 Left the channel and admin rights removed." + issue_suffix(chat_id),
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    except Exception as e:
        log.error(f"Error in custom batch approve: {e}")
        try:
            await remove_userbot_from_channel(client, chat_id)
        except Exception:
            pass
        await query.message.reply("⚠️ Timed out or failed. Userbot has left the channel.")


# ==========================================
# 6. BACK HOME CALLBACK
# ==========================================
@Client.on_callback_query(filters.regex(r"^back_home$"))
async def callback_back_home(client, query):
    await query.message.edit_text(
        home_text(query.from_user.first_name),
        reply_markup=home_markup(client.me.username, query.from_user.id)
    )


# ==========================================
# 7. HELP & USAGE CALLBACK
# ==========================================
@Client.on_callback_query(filters.regex(r"^help_info$"))
async def callback_help(client, query):
    help_text = (
        "📖 **How to Use this Bot** ✨\n\n"
        "1. Click **➕ Add to Channel** or **➕ Add to Group** and select your chat.\n"
        "2. The bot will automatically connect and message you here!\n"
        "3. Customize your DM welcome message, 🖼 photo/video/GIF and 🔘 buttons for **each channel**.\n"
        "4. In your channel or via the **⚡ Accept Old Requests** button, you can:\n"
        "   • Tap **⚡ Accept Old Requests** to temporarily connect userbot, inspect pending requests, and approve 50, 100, or all requests. The userbot leaves the channel automatically after approval!\n"
        "   • Channel commands: `/accept100`, `/approve 50`, `/acceptall`\n\n"
        "⚡ Every approved user automatically receives your custom welcome message!"
    )
    buttons = [[InlineKeyboardButton("🔙 Back Home", callback_data="back_home")]]
    await query.message.edit_text(help_text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^noop_pages$"))
async def callback_noop_pages(client, query):
    await query.answer()
