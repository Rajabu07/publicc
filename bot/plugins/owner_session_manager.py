import os
import time
import math
import logging
import asyncio
import shutil
import psutil
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import MessageNotModified
from helpers.str_to_buttons import string_to_buttons
from helpers.humanbytes import humanbytes
from helpers.broadcast import send_broadcast_preview
from helpers.utils import sparkline, progress_bar, format_uptime, safe_pct, START_TIME
from helpers.old_request_helper import get_channel_pending_count, reset_user_client
from helpers.approver_helper import format_welcome_text, PREVIEW_OPTIONS
from bot.config import Config

log = logging.getLogger(__name__)


# ==========================================
# 1. OWNER DASHBOARD MENU & COMMANDS
# ==========================================
def get_owner_panel_markup():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Stats Dashboard", callback_data="owner_stats"),
            InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast")
        ],
        [
            InlineKeyboardButton("📨 Post to Channels", callback_data="owner_post_channels_1_all"),
            InlineKeyboardButton("📑 Posted Messages", callback_data="owner_bot_posts_1")
        ],
        [
            InlineKeyboardButton("⏰ Follow-up Msg", callback_data="owner_fu"),
            InlineKeyboardButton("👋 Bye Message", callback_data="owner_bye")
        ],
        [
            InlineKeyboardButton("✏️ Global Welcome", callback_data="owner_set_global"),
            InlineKeyboardButton("🌐 All Channels", callback_data="owner_all_channels")
        ],
        [
            InlineKeyboardButton("🔐 Userbot Session", callback_data="owner_session_menu"),
            InlineKeyboardButton("🔙 Back Home", callback_data="back_home")
        ]
    ])


@Client.on_callback_query(filters.regex(r"^owner_panel$"))
async def callback_owner_panel(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    await query.message.edit_text(
        "👑 **Bot Owner Control Panel** ✨\n\n"
        "**📨 Channel Publishing Commands:**\n"
        "• `/postchannels` — List postable channels & invite links\n"
        "• `/send -100id1 -100id2 -100id3` — Post to **many channels at once** (or `/send all`)\n"
        "• `/delete <post_id> <channel_id>` — Delete a post\n"
        "• `/posts` — View posted messages\n"
        "• `/broadcast` — Reply to a message to broadcast it\n\n"
        "👇 Or use the interactive buttons below:",
        reply_markup=get_owner_panel_markup()
    )


# ==========================================
# 2. CHECK BOT STATS
# ==========================================
@Client.on_callback_query(filters.regex(r"^owner_stats$"))
async def callback_owner_stats(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Owner only.", show_alert=True)
        return

    try:
        await query.answer("📊 Loading dashboard...")
    except Exception:
        pass

    try:
        st = await client.db.get_dashboard_stats()
        session_status = await check_session_validity(client)

        total_d, used_d, free_d = (humanbytes(x) for x in shutil.disk_usage("."))
        cpu_usage = psutil.cpu_percent()
        ram_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage('.').percent

        today, totals, days = st["today"], st["totals"], st["days"]
        week_approved = sum(int(d.get("approved", 0) or 0) for d in days)
        week_dm_ok = sum(int(d.get("dm_sent", 0) or 0) for d in days)
        week_dm_bad = sum(int(d.get("dm_failed", 0) or 0) for d in days)
        trend = sparkline([d.get("approved", 0) for d in days])
        trend_nums = " ".join(str(int(d.get("approved", 0) or 0)) for d in days)

        stats_text = (
            f"📊 **Stats Dashboard** ✨\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 **Users:** `{st['users_total']}`  (🆕 today `{st['users_today']}` • 7d `{st['users_7d']}`)\n"
            f"📢 **Channels:** `{st['channels_total']}`  (🟡 manual `{st['channels_manual']}`)\n"
            f"⏳ **Pending queue:** `{st['pending_total']}`\n\n"
            f"✅ **Approvals**\n"
            f"• Today: `{int(today.get('approved', 0) or 0)}`\n"
            f"• Last 7 days: `{week_approved}`\n"
            f"• All time: `{totals['approved']}`\n\n"
            f"💌 **Welcome DMs**\n"
            f"• Today: ✅ `{int(today.get('dm_sent', 0) or 0)}`  ❌ `{int(today.get('dm_failed', 0) or 0)}`\n"
            f"• 7d delivery rate: `{safe_pct(week_dm_ok, week_dm_ok + week_dm_bad)}`\n\n"
            f"⏰ **Follow-ups:** pending `{st['followups_pending']}` • sent `{totals['followups_sent']}`\n"
            f"👋 **Bye messages sent:** `{totals['byes_sent']}`\n"
            f"📣 **Broadcast delivered:** `{totals['broadcast_sent']}`\n"
            f"📨 **Live channel posts:** `{st['posts_live']}`\n\n"
            f"📈 **Approvals — last 7 days**\n"
            f"`{trend}`\n"
            f"`{trend_nums}`\n\n"
            f"🔐 **Userbot:** {session_status}\n\n"
            f"💻 **Server**\n"
            f"• CPU `{cpu_usage}%` • RAM `{ram_usage}%` • Disk `{disk_usage}%` ({used_d}/{total_d})\n"
            f"• ⏱ Uptime: `{format_uptime(time.time() - START_TIME)}`"
        )
    except Exception as e:
        log.error(f"Stats dashboard failed: {e}", exc_info=True)
        stats_text = f"⚠️ Could not load stats right now:\n`{str(e)[:200]}`"

    buttons = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="owner_stats")],
        [InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")]
    ]
    try:
        await query.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


# ==========================================
# 3. INTERACTIVE BROADCAST PROMPTER
# ==========================================
@Client.on_callback_query(filters.regex(r"^owner_broadcast$"))
async def callback_owner_broadcast(client, query):
    if query.from_user.id not in Config.owner:
        return

    await query.message.edit_text(
        "📢 **Broadcast to all bot users**\n\n"
        "Send or forward the message you want to broadcast (text, photo, video, buttons — anything).\n\n"
        "👀 You will see a **preview** and must confirm before it is sent.\n\n"
        "*(Send /cancel to abort)*"
    )

    try:
        broadcast_msg = await query.message.chat.ask("⏳ Waiting for your broadcast message...", timeout=180)
        if broadcast_msg.text and broadcast_msg.text.strip().lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Broadcast cancelled.")
            return

        await send_broadcast_preview(client, query.message.chat.id, broadcast_msg)

    except Exception as e:
        log.error(f"Error in interactive broadcast: {e}", exc_info=True)
        await query.message.reply("⚠️ Broadcast timed out or failed.")


# ==========================================
# 4. USERBOT SESSION STATUS & GENERATOR
# ==========================================
async def get_active_session_string(client):
    db_session = await client.db.get_setting("userbot_session")
    if db_session:
        return db_session
    if Config.user_session and len(Config.user_session) > 40:
        return Config.user_session
    return None

async def check_session_validity(client):
    try:
        from helpers.old_request_helper import get_user_client
        user_client, userbot_info = await get_user_client(client)
        if not user_client or not userbot_info:
            return "🔴 Not Configured"
        name = userbot_info.get("first_name", "Userbot")
        return f"🟢 Active ({name})"
    except Exception as e:
        return f"🔴 Error: {str(e)[:30]}"



@Client.on_callback_query(filters.regex(r"^owner_session_menu$"))
async def callback_session_menu(client, query):
    if query.from_user.id not in Config.owner:
        return

    status = await check_session_validity(client)

    text = (
        f"🔐 **Userbot Session Manager**\n\n"
        f"The Userbot session is used to approve **old pre-existing backlog requests**.\n\n"
        f"• **Current Status:** {status}\n\n"
        f"You can generate or renew your session directly here using your Phone Number and OTP without needing a terminal or computer!"
    )

    buttons = [
        [InlineKeyboardButton("📱 Generate Session via Phone & OTP", callback_data="owner_gen_session")],
        [InlineKeyboardButton("🔄 Test Session Connection", callback_data="owner_test_session")],
        [InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")]
    ]

    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^owner_test_session$"))
async def callback_test_session(client, query):
    if query.from_user.id not in Config.owner:
        return
    await query.answer("Testing session connection...", show_alert=False)
    status = await check_session_validity(client)
    await query.answer(f"Status: {status}", show_alert=True)
    await callback_session_menu(client, query)


# ==========================================
# 5. IN-BOT TELEGRAM SESSION GENERATION (OTP)
# ==========================================
@Client.on_callback_query(filters.regex(r"^owner_gen_session$"))
async def callback_generate_session(client, query):
    if query.from_user.id not in Config.owner:
        return

    chat = query.message.chat

    await query.message.edit_text(
        "📱 **Generate Userbot Session (Step 1 of 2)**\n\n"
        "Please send your **Phone Number** with country code (e.g. `+919876543210`):\n\n"
        "*(Send /cancel to abort)*"
    )

    try:
        phone_msg = await chat.ask("Waiting for phone number...", timeout=120)
        phone = phone_msg.text.strip().replace(" ", "")
        if phone.lower() in ["/cancel", "/exit"]:
            await client.send_message(chat.id, "❌ Cancelled session generation.")
            return

        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import (
            SessionPasswordNeededError,
            PhoneCodeInvalidError,
            PhoneCodeExpiredError,
            PasswordHashInvalidError
        )

        # Create new temporary telethon client
        temp_client = TelegramClient(StringSession(), Config.api_id, Config.api_hash)
        await temp_client.connect()

        status_msg = await client.send_message(chat.id, f"⏳ Requesting login code from Telegram for `{phone}`...")
        sent_code = await temp_client.send_code_request(phone)

        await status_msg.edit_text(
            f"📩 **Login Code Sent to Telegram! (Step 2 of 2)**\n\n"
            f"Please enter the **OTP code** you received:\n\n"
            f"*(Tip: You can add spaces like `1 2 3 4 5` or send directly. Send /cancel to abort)*"
        )

        otp_msg = await chat.ask("Waiting for OTP code...", timeout=180)
        raw_code = otp_msg.text.strip().replace(" ", "").replace("-", "")
        if raw_code.lower() in ["/cancel", "/exit"]:
            await temp_client.disconnect()
            await client.send_message(chat.id, "❌ Cancelled session generation.")
            return

        try:
            await temp_client.sign_in(phone=phone, code=raw_code, phone_code_hash=sent_code.phone_code_hash)
        except SessionPasswordNeededError:
            pwd_msg = await chat.ask("🔐 Two-Step Verification (2FA) is enabled! Please enter your 2FA password:", timeout=120)
            pwd = pwd_msg.text.strip()
            try:
                await temp_client.sign_in(password=pwd)
            except PasswordHashInvalidError:
                await temp_client.disconnect()
                await client.send_message(chat.id, "❌ Invalid 2FA password. Please restart session generation.")
                return
        except PhoneCodeInvalidError:
            await temp_client.disconnect()
            await client.send_message(chat.id, "❌ The OTP code entered is invalid. Please try again.")
            return
        except PhoneCodeExpiredError:
            await temp_client.disconnect()
            await client.send_message(chat.id, "❌ The OTP code has expired. Please request a new code.")
            return

        # Successfully logged in!
        me = await temp_client.get_me()
        saved_session = temp_client.session.save()
        await temp_client.disconnect()

        # Save to database
        await client.db.set_setting("userbot_session", saved_session)
        reset_user_client()

        buttons = [[InlineKeyboardButton("👑 Owner Dashboard", callback_data="owner_panel")]]
        await client.send_message(
            chat.id,
            f"🎉 **Userbot Session Successfully Generated & Activated!**\n\n"
            f"• **Account:** {me.first_name} (`{me.id}`)\n"
            f"• **Status:** 🟢 Active\n\n"
            f"The bot is now fully armed to approve all old and backlogged join requests seamlessly!",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    except Exception as e:
        log.error(f"Error during in-bot session generation: {e}")
        await client.send_message(chat.id, f"❌ Failed to generate session: `{e}`\nPlease try again.")


# ==========================================
# 6. ALL CHANNELS & GLOBAL WELCOME SHORTCUTS
# ==========================================
@Client.on_callback_query(filters.regex(r"^owner_all_channels(?:_(\d+))?$"))
async def callback_owner_all_channels(client, query):
    if query.from_user.id not in Config.owner:
        return
    page = int(query.matches[0].group(1)) if query.matches[0].group(1) else 1

    raw_channels = await (await client.db.get_all_channels()).to_list(length=1000)
    all_channels = [ch for ch in raw_channels if ch.get("channel_id") and int(ch.get("channel_id")) != 0]
    if not all_channels:
        await query.answer("No channels in database.", show_alert=True)
        return

    per_page = 8
    total_pages = max(1, math.ceil(len(all_channels) / per_page))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_channels = all_channels[start_idx:end_idx]

    buttons = []
    pending_counts = await asyncio.gather(
        *[get_channel_pending_count(client, ch.get("channel_id")) for ch in page_channels],
        return_exceptions=True
    )
    for ch, pending in zip(page_channels, pending_counts):
        cid = ch.get("channel_id")
        title = ch.get("title") or f"Channel {cid}"
        mode = ch.get("mode", "auto").capitalize()
        p_count = pending if isinstance(pending, int) else 0
        buttons.append([InlineKeyboardButton(f"📢 {title} [{mode}] ({p_count} Pending)", callback_data=f"manage_ch_{cid}")])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"owner_all_channels_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop_pages"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"owner_all_channels_{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")])

    try:
        await query.message.edit_text(
            f"🌐 **All Connected Channels (Owner View - Page {page}/{total_pages}):**\n\n"
            f"• **Total Registered Channels:** `{len(all_channels)}`\n\n"
            "As bot owner, you can manage any channel connected to the bot just like a channel admin.\n"
            "Tap any channel below to configure welcome message, toggle mode, or batch approve requests:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^noop_pages$"))
async def callback_noop_pages(client, query):
    await query.answer()


@Client.on_callback_query(filters.regex(r"^owner_set_global$"))
async def callback_owner_set_global(client, query):
    if query.from_user.id not in Config.owner:
        return

    if hasattr(client.db, "get_global_welcome"):
        text, buttons_data = await client.db.get_global_welcome()
    else:
        text, buttons_data = await client.db.get_welcome(0)

    if text and text.strip():
        display_text = (
            f"✏️ **Global Owner Welcome Message**\n\n"
            f"**Current Text:**\n{text}\n\n"
            f"**Buttons:** `{buttons_data or 'None'}`\n\n"
            "This message is automatically delivered to approved users across all channels, "
            "even if a channel has its own custom welcome message."
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit Global Welcome", callback_data="owner_edit_global")],
            [InlineKeyboardButton("🗑️ Remove Global Welcome", callback_data="owner_del_global")],
            [InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")]
        ])
    else:
        display_text = (
            "✏️ **Global Owner Welcome Message**\n\n"
            "Currently, **no global owner welcome message** is configured.\n"
            "Tap below to set a global welcome message to be shared across all channels:"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set Global Welcome", callback_data="owner_edit_global")],
            [InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")]
        ])

    try:
        await query.message.edit_text(display_text, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^owner_edit_global$"))
async def callback_owner_edit_global(client, query):
    if query.from_user.id not in Config.owner:
        return

    back_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Global Welcome", callback_data="owner_set_global")]])

    await query.message.edit_text(
        "📝 **Setting Global Owner Welcome**\n\n"
        "Please send the message you want new members to receive when approved in channels without a custom welcome.\n\n"
        "**Available formatting tags:**\n"
        "• `{name}` or `{first_name}` — User's name\n"
        "• `{chat}` or `{title}` — Channel title\n"
        "• `{mention}` — User mention link\n"
        "• `{id}` — User's Telegram ID\n\n"
        "*(Send /cancel to abort)*",
        reply_markup=back_markup
    )

    try:
        welcome_resp = await query.message.chat.ask("Waiting for global welcome text...", timeout=120)
        raw_welcome = (welcome_resp.text or welcome_resp.caption or "").strip()

        if not raw_welcome:
            await query.message.reply("⚠️ No text received. Setup aborted.", reply_markup=back_markup)
            return

        if raw_welcome.lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Cancelled setting global welcome.", reply_markup=back_markup)
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
            await query.message.reply("❌ Cancelled setting global welcome.", reply_markup=back_markup)
            return

        if raw_buttons.lower() in ["/pass", "/skip", "none", "skip", "no"]:
            buttons_text = None
        else:
            buttons_text = raw_buttons

        if hasattr(client.db, "update_global_welcome"):
            await client.db.update_global_welcome(welcome_text, buttons_text)
        else:
            await client.db.update_welcome(0, welcome_text, buttons_text)

        user_name = (query.from_user.first_name if query.from_user else None) or "Friend"
        preview_markup = await string_to_buttons(buttons_text, include_mandatory=True)
        preview_text = format_welcome_text(welcome_text, user_name, "Sample Channel", query.from_user.id if query.from_user else 0)

        await query.message.reply(
            "✅ **Global Owner Welcome Saved!**\n\n**Preview of what users will receive:**",
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
        log.error(f"Error in interactive set global welcome: {e}")
        await query.message.reply("⚠️ Setup timed out or failed. Please try again.", reply_markup=back_markup)


@Client.on_callback_query(filters.regex(r"^owner_del_global$"))
async def callback_owner_del_global(client, query):
    if query.from_user.id not in Config.owner:
        return

    if hasattr(client.db, "update_global_welcome"):
        await client.db.update_global_welcome(None, None)
    else:
        await client.db.update_welcome(0, None, None)

    await query.answer("✅ Global owner welcome removed.", show_alert=True)
    await callback_owner_set_global(client, query)


# ==========================================
# 7. PROACTIVE USERBOT SESSION HEALTH MONITOR
# ==========================================
_last_expiry_alert = 0

async def monitor_userbot_session(client):
    """
    Background worker that periodically monitors Userbot session health.
    If the session expires or is revoked, it immediately notifies the bot owners
    with a direct inline button to generate a new session via Phone & OTP.
    """
    global _last_expiry_alert
    from helpers.old_request_helper import notify_owners_session_expired

    await asyncio.sleep(20)  # Wait for bot client to start completely
    while True:
        try:
            status = await check_session_validity(client)
            if "Expired" in status or "Error" in status or "unauthorized" in status.lower():
                now = time.time()
                # Alert at most once every 6 hours
                if now - _last_expiry_alert > 21600:
                    _last_expiry_alert = now
                    await notify_owners_session_expired(client, f"Health check report: {status}")
        except Exception as e:
            log.warning(f"Error in session monitor: {e}")
        await asyncio.sleep(1800)  # Recheck every 30 minutes
