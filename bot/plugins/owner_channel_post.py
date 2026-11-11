import time
import math
import logging
import asyncio
import datetime
import re
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyrogram.errors import MessageNotModified, ChatAdminRequired, FloodWait, RPCError
from helpers.str_to_buttons import string_to_buttons
from helpers.channel_post_helper import (
    get_bot_channel_permissions,
    get_all_verified_channels
)
from bot.config import Config
from helpers.utils import parse_timer_seconds, format_duration, parse_channel_ids, chunked

try:
    from pyrogram.types import LinkPreviewOptions
    PREVIEW_OPTIONS = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except (ImportError, AttributeError):
    PREVIEW_OPTIONS = {"disable_web_page_preview": True}

log = logging.getLogger(__name__)

# Temporary store for post drafts in memory: {draft_id: draft_dict}
_POST_DRAFTS = {}


async def schedule_in_memory_delete(client: Client, chat_id: int, message_id: int, delay_seconds: float):
    """
    Precision in-memory timer task that deletes the post right on time.
    Runs alongside the database background worker for dual-layer reliability.
    """
    try:
        await asyncio.sleep(delay_seconds)
        await client.delete_messages(chat_id=chat_id, message_ids=message_id)
        await client.db.mark_post_deleted(chat_id, message_id)
        log.info(f"In-memory timer deleted post #{message_id} in channel {chat_id}")
    except Exception as e:
        log.debug(f"In-memory timer delete task completed/handled for #{message_id} in {chat_id}: {e}")


# ==============================================================================
# 1. PAGINATED CHANNEL LIST WITH DIRECT INVITE LINKS & POSTABILITY FILTER
# ==============================================================================
@Client.on_callback_query(filters.regex(r"^owner_post_channels(?:_(\d+))?(?:_(all|postable))?$"))
async def callback_owner_post_channels(client: Client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    match = query.matches[0]
    page = int(match.group(1)) if match.group(1) else 1
    filter_mode = match.group(2) if match.group(2) else "all"
    postable_only = (filter_mode == "postable")

    # Inform user while loading if it takes a moment
    try:
        await query.answer("🔄 Loading channels...", show_alert=False)
    except Exception:
        pass

    channels = await get_all_verified_channels(client, postable_only=postable_only)

    if not channels:
        if postable_only:
            empty_msg = (
                "⚠️ **No Postable Channels Found!**\n\n"
                "The bot is added to some chats, but it does NOT have the **'Post Messages'** "
                "admin permission in any of them.\n\n"
                "Please make sure the bot is promoted as Admin with **Post Messages** enabled."
            )
            buttons = [
                [InlineKeyboardButton("📋 Show All Connected Channels", callback_data="owner_post_channels_1_all")],
                [InlineKeyboardButton("🔙 Back to Owner Dashboard", callback_data="owner_panel")]
            ]
        else:
            empty_msg = (
                "⚠️ **No Channels Connected Yet!**\n\n"
                "Add the bot as an Admin to your Telegram channels to start managing and posting."
            )
            buttons = [
                [InlineKeyboardButton("➕ Add to Channel", url=Config.get_add_channel_url(client.me.username))],
                [InlineKeyboardButton("🔙 Back to Owner Dashboard", callback_data="owner_panel")]
            ]

        try:
            await query.message.edit_text(empty_msg, reply_markup=InlineKeyboardMarkup(buttons))
        except MessageNotModified:
            pass
        return

    per_page = getattr(Config, "channels_per_page", 10)
    total_pages = max(1, math.ceil(len(channels) / per_page))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_channels = channels[start_idx:end_idx]

    # Build channel buttons and text listing
    list_lines = []
    channel_buttons = []

    for idx, ch in enumerate(page_channels, start=start_idx + 1):
        cid = ch.get("channel_id")
        title = ch.get("title") or f"Channel {cid}"
        can_post = ch.get("can_post", False)
        direct_link = ch.get("direct_link") or f"https://t.me/c/{abs(cid)}/1"
        status_icon = "🟢" if can_post else "🔴"
        status_text = "Post Allowed" if can_post else "No Post Perm"

        list_lines.append(
            f"**{idx}.** [{title}]({direct_link}) `[{cid}]`\n"
            f"   └ Status: {status_icon} **{status_text}**"
        )

        btn_label = f"{status_icon} {title[:28]}"
        channel_buttons.append([InlineKeyboardButton(btn_label, callback_data=f"select_post_ch_{cid}")])

    # Navigation buttons
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"owner_post_channels_{page - 1}_{filter_mode}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop_pages"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"owner_post_channels_{page + 1}_{filter_mode}"))

    buttons = channel_buttons

    if nav_row:
        buttons.append(nav_row)

    # Filter toggle row
    if postable_only:
        filter_toggle_btn = InlineKeyboardButton("📋 Show All Channels", callback_data=f"owner_post_channels_1_all")
    else:
        filter_toggle_btn = InlineKeyboardButton("🔍 Filter: Postable Only", callback_data=f"owner_post_channels_1_postable")

    buttons.append([
        filter_toggle_btn,
        InlineKeyboardButton("🔄 Refresh", callback_data=f"owner_post_channels_{page}_{filter_mode}")
    ])
    buttons.append([
        InlineKeyboardButton("📑 Posted Messages", callback_data="owner_bot_posts_1"),
        InlineKeyboardButton("🔙 Owner Dashboard", callback_data="owner_panel")
    ])

    filter_title = "Postable Channels Only" if postable_only else "All Connected Channels"
    message_text = (
        f"📢 **Channel Publisher & Manager ({filter_title})**\n"
        f"**Page:** `{page}/{total_pages}` | **Total:** `{len(channels)}` channels\n\n"
        f"Direct Invite Links are generated via Bot's invite permissions.\n\n"
        + "\n\n".join(list_lines)
        + "\n\n👇 **Select a channel below to write & publish a post:**"
    )

    try:
        await query.message.edit_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True
        )
    except MessageNotModified:
        pass


# ==============================================================================
# 2. CHANNEL SELECTION & PERMISSIONS DETAILS VIEW
# ==============================================================================
@Client.on_callback_query(filters.regex(r"^select_post_ch_(-?\d+)$"))
async def callback_select_post_channel(client: Client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    chat_id = int(query.matches[0].group(1))
    perms = await get_bot_channel_permissions(client, chat_id, force_refresh=True)

    if not perms.get("is_valid"):
        await query.answer("⚠️ Bot is currently not an admin or cannot access this channel. Showing channel details.", show_alert=False)

    title = perms["title"]
    direct_link = perms["direct_link"] or f"https://t.me/c/{abs(chat_id)}/1"
    can_post = perms["can_post"]
    can_delete = perms["can_delete"]
    can_invite = perms["can_invite"]
    chat_type = perms["chat_type"].capitalize()

    details_text = (
        f"📢 **Channel Details & Publishing Hub**\n\n"
        f"• **Title:** [{title}]({direct_link})\n"
        f"• **Chat ID:** `{chat_id}`\n"
        f"• **Type:** `{chat_type}`\n"
        f"• **Direct Link:** [Click to Open]({direct_link})\n\n"
        f"⚙️ **Bot Administrative Permissions:**\n"
        f"• **Can Post Messages:** {'🟢 Yes (Allowed)' if can_post else '🔴 No (Forbidden)'}\n"
        f"• **Can Delete Messages:** {'🟢 Yes (Allowed)' if can_delete else '🔴 No (Forbidden)'}\n"
        f"• **Can Invite Users:** {'🟢 Yes (Allowed)' if can_invite else '🔴 No (Forbidden)'}\n\n"
    )

    buttons = []
    if can_post:
        details_text += "✅ **The bot has permission to post here!** Click below to compose your post."
        buttons.append([InlineKeyboardButton("✍️ Write & Publish Post", callback_data=f"compose_post_{chat_id}")])
    else:
        details_text += (
            "⚠️ **The bot CANNOT post in this channel.**\n"
            "Please open channel settings on Telegram, promote the bot or enable the **'Post Messages'** privilege, "
            "then click **🔄 Recheck Permissions** below."
        )
        buttons.append([InlineKeyboardButton("🔄 Recheck Permissions", callback_data=f"select_post_ch_{chat_id}")])

    buttons.append([
        InlineKeyboardButton("🔗 Direct Invite Link", url=direct_link),
        InlineKeyboardButton("⚙️ Channel Welcome & Mode", callback_data=f"manage_ch_{chat_id}")
    ])
    buttons.append([
        InlineKeyboardButton("🔙 Back to Channels", callback_data="owner_post_channels_1_all"),
        InlineKeyboardButton("👑 Owner Dashboard", callback_data="owner_panel")
    ])

    try:
        await query.message.edit_text(
            details_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True
        )
    except MessageNotModified:
        pass


# ==============================================================================
# 3. INTERACTIVE POST COMPOSITION WIZARD (FULL MEDIA, FORWARD, BUTTONS, AUTO-DELETE)
# ==============================================================================
@Client.on_callback_query(filters.regex(r"^compose_post_(-?\d+)$"))
async def callback_compose_post(client: Client, query):
    owner_id = query.from_user.id
    if owner_id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    chat_id = int(query.matches[0].group(1))
    perms = await get_bot_channel_permissions(client, chat_id)

    if not perms.get("can_post"):
        await query.answer("❌ Bot does not have posting permissions in this channel!", show_alert=True)
        return

    title = perms.get("title", f"Channel {chat_id}")

    prompt_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Wizard", callback_data=f"select_post_ch_{chat_id}")]
    ])

    await query.message.edit_text(
        f"📝 **Composing Post for:** **{title}** (`{chat_id}`)\n\n"
        f"**Step 1 of 3:** Send the post content.\n\n"
        f"You can send:\n"
        f"• Plain text or formatted Markdown/HTML\n"
        f"• Media (Photo, Video, Animation/GIF, Document, Audio, Sticker)\n"
        f"• Any forwarded message from another channel\n\n"
        f"*(Send /cancel to abort)*",
        reply_markup=prompt_markup
    )

    try:
        # Step 1: Wait for post message
        post_response: Message = await query.message.chat.ask(
            "⏳ Waiting for your post content...",
            timeout=300
        )

        if post_response.text and post_response.text.strip().lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Post creation cancelled.")
            return

        # Determine message type and snippet for logging/display
        msg_type = "text"
        snippet = ""
        if post_response.text:
            msg_type = "text"
            snippet = post_response.text[:80]
        elif post_response.photo:
            msg_type = "photo"
            snippet = post_response.caption[:80] if post_response.caption else "[Photo]"
        elif post_response.video:
            msg_type = "video"
            snippet = post_response.caption[:80] if post_response.caption else "[Video]"
        elif post_response.animation:
            msg_type = "animation"
            snippet = post_response.caption[:80] if post_response.caption else "[GIF]"
        elif post_response.document:
            msg_type = "document"
            snippet = post_response.caption[:80] if post_response.caption else "[Document]"
        elif post_response.audio:
            msg_type = "audio"
            snippet = post_response.caption[:80] if post_response.caption else "[Audio]"
        elif post_response.sticker:
            msg_type = "sticker"
            snippet = "[Sticker]"
        else:
            msg_type = "custom"
            snippet = "[Media]"

        # Step 2: Inline Buttons
        btn_prompt_text = (
            "🔘 **Step 2 of 3: Inline URL Buttons (Optional)**\n\n"
            "Send the buttons to attach to the post in this format:\n"
            "`Button 1 - https://example.com | Button 2 - https://example.com`\n\n"
            "Or send `/pass` to proceed without buttons.\n"
            "*(Send /cancel to abort)*"
        )
        btn_response: Message = await query.message.chat.ask(btn_prompt_text, timeout=180)
        btn_text = btn_response.text.strip() if btn_response.text else ""

        if btn_text.lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Post creation cancelled.")
            return

        custom_buttons = None
        if btn_text and btn_text.lower() != "/pass":
            try:
                custom_buttons = await string_to_buttons(btn_text, include_mandatory=False)
            except Exception as e:
                log.warning(f"Could not parse custom buttons: {e}")
                await query.message.reply("⚠️ Could not parse buttons format. Proceeding with no buttons.")

        # Step 3: Auto-Delete Timer
        del_prompt_text = (
            "⏱️ **Step 3 of 3: Auto-Delete Timer (Optional)**\n\n"
            "Do you want this post to be deleted automatically after a duration?\n\n"
            "Send the duration in **minutes**:\n"
            "• `10` = 10 minutes\n"
            "• `60` = 1 hour\n"
            "• `1440` = 24 hours\n\n"
            "Or send `/pass` to keep the post permanently.\n"
            "*(Send /cancel to abort)*"
        )
        del_response: Message = await query.message.chat.ask(del_prompt_text, timeout=180)
        del_text = del_response.text.strip() if del_response.text else ""

        if del_text.lower() in ["/cancel", "/exit"]:
            await query.message.reply("❌ Post creation cancelled.")
            return

        auto_delete_minutes = None
        if del_text and del_text.lower() != "/pass":
            if del_text.isdigit() and int(del_text) > 0:
                auto_delete_minutes = int(del_text)
            else:
                await query.message.reply("ℹ️ Invalid number received. Setting post to permanent (no auto-delete).")

        # Save draft in memory
        draft_id = f"{owner_id}_{chat_id}_{int(asyncio.get_event_loop().time())}"
        _POST_DRAFTS[draft_id] = {
            "channel_id": chat_id,
            "channel_title": title,
            "source_chat_id": post_response.chat.id,
            "source_message_id": post_response.id,
            "msg_type": msg_type,
            "snippet": snippet,
            "buttons_text": btn_text if btn_text != "/pass" else None,
            "auto_delete_minutes": auto_delete_minutes
        }

        # Show Confirmation & Preview
        auto_del_label = f"⏳ In {auto_delete_minutes} minute(s)" if auto_delete_minutes else "♾️ Permanent (No auto-delete)"
        confirm_text = (
            f"👀 **Post Preview & Confirmation**\n\n"
            f"• **Target Channel:** **{title}** (`{chat_id}`)\n"
            f"• **Type:** `{msg_type.upper()}`\n"
            f"• **Auto-Delete:** `{auto_del_label}`\n\n"
            f"Here is how your post will look:"
        )

        confirm_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 Publish to Channel Now", callback_data=f"publish_post_{draft_id}"),
                InlineKeyboardButton("❌ Discard", callback_data="discard_post_draft")
            ]
        ])

        await query.message.reply(confirm_text)

        # Send preview copy to owner chat
        try:
            await post_response.copy(
                chat_id=owner_id,
                reply_markup=custom_buttons
            )
        except Exception:
            pass

        await query.message.reply(
            "Ready to broadcast? Click below:",
            reply_markup=confirm_markup
        )

    except asyncio.TimeoutError:
        await query.message.reply("⚠️ Post composition timed out. Please try again.")
    except Exception as e:
        log.error(f"Error in compose post wizard: {e}", exc_info=True)
        await query.message.reply(f"⚠️ An error occurred during post composition: `{e}`")


# ==============================================================================
# 4. CONFIRM & PUBLISH POST VIA BOT CLIENT ONLY
# ==============================================================================
@Client.on_callback_query(filters.regex(r"^publish_post_(.+)$"))
async def callback_publish_post(client: Client, query):
    owner_id = query.from_user.id
    if owner_id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    draft_id = query.matches[0].group(1)
    draft = _POST_DRAFTS.pop(draft_id, None)

    if not draft:
        await query.answer("⚠️ Draft expired or already published.", show_alert=True)
        return

    chat_id = draft["channel_id"]
    channel_title = draft["channel_title"]
    source_chat_id = draft["source_chat_id"]
    source_message_id = draft["source_message_id"]
    buttons_text = draft["buttons_text"]
    auto_delete_minutes = draft["auto_delete_minutes"]
    msg_type = draft["msg_type"]
    snippet = draft["snippet"]

    custom_buttons = None
    if buttons_text:
        custom_buttons = await string_to_buttons(buttons_text, include_mandatory=False)

    status_msg = await query.message.edit_text("⏳ Publishing post to channel via Bot...")

    try:
        # Strictly send via Bot API Client
        sent_message = await client.copy_message(
            chat_id=chat_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
            reply_markup=custom_buttons
        )

        sent_msg_id = sent_message.id

        # Calculate auto-delete time if requested
        auto_delete_at = None
        auto_delete_ts = None
        if auto_delete_minutes:
            delay_sec = auto_delete_minutes * 60
            auto_delete_ts = time.time() + delay_sec
            auto_delete_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=auto_delete_minutes)
            # Precision in-memory timer
            asyncio.create_task(schedule_in_memory_delete(client, chat_id, sent_msg_id, delay_sec))

        # Check if bot can delete messages in this channel & fetch direct invite link
        perms = await get_bot_channel_permissions(client, chat_id)
        can_delete = perms.get("can_delete", False)
        direct_link = perms.get("direct_link", f"https://t.me/c/{abs(chat_id)}/1")

        # Store post record in MongoDB
        await client.db.save_bot_post(
            channel_id=chat_id,
            message_id=sent_msg_id,
            channel_title=channel_title,
            post_type=msg_type,
            text_snippet=snippet,
            auto_delete_at=auto_delete_at,
            auto_delete_ts=auto_delete_ts,
            direct_link=direct_link
        )

        success_buttons = []
        if can_delete:
            success_buttons.append([
                InlineKeyboardButton("🗑️ Delete Post Now", callback_data=f"del_post_{chat_id}_{sent_msg_id}")
            ])

        success_buttons.append([
            InlineKeyboardButton("🔗 View in Channel", url=f"{direct_link}/{sent_msg_id}"),
            InlineKeyboardButton("📢 All Channels", callback_data="owner_post_channels_1_postable")
        ])
        success_buttons.append([
            InlineKeyboardButton("👑 Owner Dashboard", callback_data="owner_panel")
        ])

        auto_del_note = (
            f"⏳ Will auto-delete in **{auto_delete_minutes} minute(s)**"
            if auto_delete_minutes else "♾️ Permanent"
        )

        await status_msg.edit_text(
            f"🎉 **Post Successfully Published to Channel!**\n\n"
            f"• **Channel:** [{channel_title}]({direct_link})\n"
            f"• **Channel ID:** `{chat_id}`\n"
            f"• **Message ID:** `{sent_msg_id}`\n"
            f"• **Auto-Delete:** {auto_del_note}\n\n"
            f"You can delete this message anytime using the button below:",
            reply_markup=InlineKeyboardMarkup(success_buttons),
            disable_web_page_preview=True
        )

    except FloodWait as e:
        await status_msg.edit_text(f"⚠️ Telegram FloodWait: Please wait {e.value} seconds before posting again.")
    except (ChatAdminRequired, RPCError) as e:
        log.error(f"Failed to post to {chat_id}: {e}")
        await status_msg.edit_text(
            f"❌ **Failed to Publish Post!**\n\n"
            f"Telegram Error: `{e}`\n"
            f"Ensure the bot is still an Admin with **Post Messages** permission."
        )
    except Exception as e:
        log.error(f"Unexpected error publishing post: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Unexpected error publishing post: `{e}`")


@Client.on_callback_query(filters.regex(r"^discard_post_draft$"))
async def callback_discard_draft(client: Client, query):
    await query.message.edit_text("❌ Post draft discarded.")


# ==============================================================================
# 5. ON-DEMAND POST DELETION (BOT CLIENT STRICTLY)
# ==============================================================================
@Client.on_callback_query(filters.regex(r"^del_post_(-?\d+)_(\d+)$"))
async def callback_delete_post(client: Client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    chat_id = int(query.matches[0].group(1))
    message_id = int(query.matches[0].group(2))

    try:
        # Strictly delete via Bot client
        await client.delete_messages(chat_id=chat_id, message_ids=message_id)
        await client.db.mark_post_deleted(chat_id, message_id)
        await query.answer("✅ Post successfully deleted from channel!", show_alert=True)

        try:
            await query.message.edit_text(
                f"🗑️ **Post Deleted Successfully!**\n\n"
                f"• **Channel ID:** `{chat_id}`\n"
                f"• **Message ID:** `{message_id}`\n"
                f"• **Status:** Removed from Telegram & marked deleted in database.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📑 Back to Posted Messages", callback_data="owner_bot_posts_1")],
                    [InlineKeyboardButton("👑 Owner Dashboard", callback_data="owner_panel")]
                ])
            )
        except MessageNotModified:
            pass

    except (ChatAdminRequired, RPCError) as e:
        log.error(f"Could not delete message {message_id} in {chat_id}: {e}")
        await query.answer(f"❌ Failed to delete: {e}", show_alert=True)
    except Exception as e:
        log.error(f"Unexpected error deleting post {message_id}: {e}")
        await query.answer(f"❌ Error: {e}", show_alert=True)


# ==============================================================================
# 6. OWNER MENU: VIEW POSTED MESSAGES VIA BOT & DELETE
# ==============================================================================
@Client.on_callback_query(filters.regex(r"^owner_bot_posts(?:_(\d+))?$"))
async def callback_owner_bot_posts(client: Client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    page = int(query.matches[0].group(1)) if query.matches[0].group(1) else 1
    per_page = getattr(Config, "posts_per_page", 10)

    total_posts = await client.db.count_bot_posts(include_deleted=False)

    if total_posts == 0:
        buttons = [
            [InlineKeyboardButton("📢 Go to Postable Channels", callback_data="owner_post_channels_1_postable")],
            [InlineKeyboardButton("🔙 Back to Owner Dashboard", callback_data="owner_panel")]
        ]
        await query.message.edit_text(
            "ℹ️ **No active posts sent by the bot found.**\n\n"
            "When you publish posts to channels using this bot, they will appear here with one-click delete buttons!",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    total_pages = max(1, math.ceil(total_posts / per_page))
    page = max(1, min(page, total_pages))
    skip = (page - 1) * per_page

    posts = await client.db.get_bot_posts(limit=per_page, skip=skip, include_deleted=False)

    lines = []
    post_buttons = []

    # Resolve direct invite links (creates_join_request=False) for channels on this page
    channel_links = {}
    unique_cids = list({post.get("channel_id") for post in posts if post.get("channel_id")})

    async def resolve_channel_link(cid):
        # 1. Check in post doc if cached
        for p in posts:
            if p.get("channel_id") == cid and p.get("direct_link"):
                return cid, p.get("direct_link")

        # 2. Check cached direct_link in MongoDB channel collection
        try:
            cached_link = await client.db.get_channel_direct_link(cid)
            if cached_link and ("t.me/+" in cached_link or "t.me/joinchat/" in cached_link or "t.me/" in cached_link):
                return cid, cached_link
        except Exception:
            pass

        # 3. Check permissions & generate direct invite link strictly via bot
        try:
            perms = await get_bot_channel_permissions(client, cid)
            link = perms.get("direct_link")
            if link:
                return cid, link
        except Exception:
            pass

        # 4. Fallback clean internal ID link
        clean_id = str(cid).replace("-100", "").replace("-", "")
        return cid, f"https://t.me/c/{clean_id}/1"

    link_results = await asyncio.gather(*[resolve_channel_link(cid) for cid in unique_cids], return_exceptions=True)
    for res in link_results:
        if isinstance(res, tuple):
            channel_links[res[0]] = res[1]

    for idx, post in enumerate(posts, start=skip + 1):
        cid = post.get("channel_id")
        mid = post.get("message_id")
        raw_title = post.get("channel_title") or f"Channel {cid}"
        title = raw_title.replace("[", "(").replace("]", ")")
        clean_cid_str = str(cid).replace("-100", "").replace("-", "")
        direct_link = channel_links.get(cid) or f"https://t.me/c/{clean_cid_str}/1"
        p_type = post.get("post_type", "text").upper()
        snippet = post.get("text_snippet") or ""
        created_at = post.get("created_at")
        date_str = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "Unknown"

        auto_del = post.get("auto_delete_at")
        auto_del_str = f" | ⏳ Auto: {auto_del.strftime('%H:%M')}" if auto_del else ""

        snippet_disp = f" - _{snippet[:30]}..._" if snippet else ""
        lines.append(f"**{idx}.** [{title}]({direct_link}) (`#{mid}`)\n   └ [{p_type}] {date_str}{auto_del_str}{snippet_disp}")

        # Delete button per post
        btn_label = f"🗑️ Delete #{mid} in {raw_title[:20]}"
        post_buttons.append([InlineKeyboardButton(btn_label, callback_data=f"del_post_{cid}_{mid}")])

    # Navigation
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"owner_bot_posts_{page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop_pages"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"owner_bot_posts_{page + 1}"))

    buttons = post_buttons
    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton("📢 Post to Channels", callback_data="owner_post_channels_1_postable"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"owner_bot_posts_{page}")
    ])
    buttons.append([InlineKeyboardButton("🔙 Back to Owner Dashboard", callback_data="owner_panel")])

    message_text = (
        f"📑 **Posted Messages Hub (Active: {total_posts})**\n"
        f"**Page:** `{page}/{total_pages}`\n\n"
        + "\n\n".join(lines)
        + "\n\n👇 **Tap any button below to instantly delete that post from the channel:**"
    )

    try:
        await query.message.edit_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True
        )
    except MessageNotModified:
        pass


# ==============================================================================
# 7. BACKGROUND WORKER: AUTO-DELETE SCHEDULED POSTS
# ==============================================================================
async def auto_delete_worker(client: Client):
    """
    Background worker running periodically to delete scheduled posts strictly via Bot client.
    Checks MongoDB every 15 seconds to ensure posts are deleted across bot restarts.
    """
    await asyncio.sleep(10)  # Wait for startup
    log.info("Auto-delete background worker initialized.")

    while True:
        try:
            pending_posts = await client.db.get_pending_auto_delete_posts()
            for post in pending_posts:
                cid = post.get("channel_id")
                mid = post.get("message_id")
                try:
                    await client.delete_messages(chat_id=cid, message_ids=mid)
                    log.info(f"Auto-deleted scheduled post #{mid} in channel {cid}")
                except Exception as e:
                    log.warning(f"Could not auto-delete post #{mid} in {cid}: {e}")
                finally:
                    # Always mark as processed so it doesn't loop
                    await client.db.mark_post_deleted(cid, mid)
        except Exception as e:
            log.warning(f"Error in auto_delete_worker: {e}")

        await asyncio.sleep(15)  # Check every 15 seconds


# ==============================================================================
# 8. DIRECT OWNER COMMANDS: /postchannels, /send, /delete, /posts
# ==============================================================================

@Client.on_message(filters.command(["postchannels", "channels"]) & filters.private)
async def cmd_postchannels_text(client: Client, message: Message):
    """
    Lists ONLY channels where the bot (not userbot) has permission to post messages.
    Includes Channel Title, Channel ID, and direct invite link (NOT request link).
    Plain text output (no inline buttons).
    """
    if message.from_user.id not in Config.owner:
        return

    status_msg = await message.reply("🔄 Fetching postable channels and generating direct invite links...", quote=True)

    channels = await get_all_verified_channels(client, postable_only=True)

    if not channels:
        await status_msg.edit_text(
            "⚠️ **No Postable Channels Found!**\n\n"
            "The bot is added to some chats, but it does NOT have the **'Post Messages'** "
            "admin privilege in any of them.\n\n"
            "Please promote the bot as an Administrator with **Post Messages** enabled, "
            "then run `/postchannels` again."
        )
        return

    lines = [
        "📢 **Postable Channels (Bot Can Post Messages):**\n"
        "*(Direct invite links shown — NOT join request links)*\n"
    ]

    for idx, ch in enumerate(channels, start=1):
        cid = ch.get("channel_id")
        title = ch.get("title") or f"Channel {cid}"
        direct_link = ch.get("direct_link") or f"https://t.me/c/{abs(cid)}/1"

        lines.append(
            f"**{idx}. {title}**\n"
            f"• **Channel ID:** `{cid}`\n"
            f"• **Invite Link:** {direct_link}\n"
        )

    lines.append(
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 **Total Postable Channels:** `{len(channels)}`\n\n"
        f"💡 **Send to one:** `/send {channels[0].get('channel_id')}`\n"
        f"💡 **Send to many:** `/send id1 id2 id3` or `/send all`"
    )

    full_text = "\n".join(lines)

    if len(full_text) <= 4000:
        await status_msg.edit_text(full_text, disable_web_page_preview=True)
    else:
        await status_msg.delete()
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 2 > 3800:
                await message.reply(chunk, disable_web_page_preview=True)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk.strip():
            await message.reply(chunk, disable_web_page_preview=True)


async def _check_targets(client, ids):
    """Verifies the bot can post in every channel. Returns (ok [(id, title)], bad [(id, reason)])."""
    ok, bad = [], []

    async def check(cid):
        try:
            perms = await get_bot_channel_permissions(client, cid, force_refresh=True)
        except Exception as e:
            return cid, None, f"error: {str(e)[:60]}"
        if perms.get("can_post"):
            return cid, perms.get("title") or f"Channel {cid}", None
        return cid, None, "bot is not admin / no 'Post Messages' right"

    for group in chunked(list(ids), 8):
        for cid, title, reason in await asyncio.gather(*[check(c) for c in group]):
            if reason:
                bad.append((cid, reason))
            else:
                ok.append((cid, title))
    return ok, bad


async def _publish_once(client, chat_id, draft, custom_buttons):
    """Sends the draft to ONE channel (FloodWait-safe). Returns the sent message."""
    msg_type, post_text = draft["msg_type"], draft.get("post_text")
    for attempt in range(3):
        try:
            if msg_type == "text" and post_text:
                return await client.send_message(
                    chat_id=chat_id, text=post_text, entities=draft.get("post_entities"),
                    reply_markup=custom_buttons, **PREVIEW_OPTIONS
                )
            return await client.copy_message(
                chat_id=chat_id, from_chat_id=draft["source_chat_id"],
                message_id=draft["source_message_id"], reply_markup=custom_buttons
            )
        except FloodWait as e:
            if attempt == 2:
                raise
            log.warning(f"FloodWait {e.value}s while posting to {chat_id}")
            await asyncio.sleep(min(e.value + 1, 120))


# Remembers the messages of the last multi-channel posts so the owner can delete them all: {batch_id: [(chat_id, msg_id)]}
_BATCHES = {}


@Client.on_message(filters.command("send") & filters.private)
async def cmd_send_post(client: Client, message: Message):
    """
    /send <channel_id> [<channel_id> ...]  - post the SAME message to one or many channels.
    /send all                               - every channel where the bot can post.
    """
    if message.from_user.id not in Config.owner:
        return

    owner_id = message.from_user.id
    raw = (message.text or "").split(None, 1)
    raw_args = raw[1] if len(raw) > 1 else ""
    ids, invalid, wants_all = parse_channel_ids(raw_args)

    if not ids and not wants_all:
        await message.reply(
            "⚠️ **Missing Channel ID!**\n\n"
            "📨 **One channel:** `/send -100123456789`\n"
            "📨 **Many channels at once:**\n"
            "`/send -100111111111 -100222222222 -100333333333`\n"
            "📨 **All postable channels:** `/send all`\n\n"
            "*(Tip: run `/postchannels` to see all postable channels and IDs)*",
            quote=True
        )
        return

    if invalid:
        await message.reply(
            "⚠️ **Invalid channel ID(s):** " + ", ".join(f"`{t}`" for t in invalid[:10]) +
            "\n\nIDs must look like `-100123456789`. Nothing was sent.",
            quote=True
        )
        return

    checking = await message.reply("🔍 Checking channel permissions...", quote=True)

    if wants_all:
        channels = await get_all_verified_channels(client, postable_only=True)
        for ch in channels:
            cid = ch.get("channel_id")
            if cid and cid not in ids:
                ids.append(cid)

    ok, bad = await _check_targets(client, ids)

    if not ok:
        reasons = "\n".join(f"• `{cid}` — {why}" for cid, why in bad[:15]) or "• No postable channels found."
        await checking.edit_text(f"❌ **Cannot Post!**\n\n{reasons}\n\nPromote the bot with **Post Messages** and try again.")
        return

    skipped = ""
    if bad:
        skipped = f"\n\n⚠️ **Skipped {len(bad)}:**\n" + "\n".join(f"• `{cid}` — {why}" for cid, why in bad[:10])
    shown = "\n".join(f"• **{t}** (`{c}`)" for c, t in ok[:15])
    more = f"\n• ...and {len(ok) - 15} more" if len(ok) > 15 else ""
    await checking.edit_text(f"✅ **{len(ok)} channel(s) ready:**\n{shown}{more}{skipped}")

    # Step 1: Ask for content
    await message.reply(
        f"📝 **Step 1 of 3:** Send the post message.\n\n"
        f"• Supported: Text, Photo, Video, GIF, Document, Audio, Sticker, or Forward.\n"
        f"*(Send /cancel to abort)*",
        quote=True
    )

    try:
        post_response: Message = await message.chat.ask("⏳ Waiting for post content...", timeout=300)

        if post_response.text and post_response.text.strip().lower() in ["/cancel", "/exit"]:
            await message.reply("❌ Post composition cancelled.")
            return

        # Determine type & snippet
        msg_type = "text"
        snippet = ""
        post_text = post_response.text or ""
        post_caption = post_response.caption or ""
        post_entities = post_response.entities or post_response.caption_entities

        if post_response.text:
            msg_type = "text"
            snippet = post_response.text[:80]
        elif post_response.photo:
            msg_type = "photo"
            snippet = post_caption[:80] if post_caption else "[Photo]"
        elif post_response.video:
            msg_type = "video"
            snippet = post_caption[:80] if post_caption else "[Video]"
        elif post_response.animation:
            msg_type = "animation"
            snippet = post_caption[:80] if post_caption else "[GIF]"
        elif post_response.document:
            msg_type = "document"
            snippet = post_caption[:80] if post_caption else "[Document]"
        elif post_response.audio:
            msg_type = "audio"
            snippet = post_caption[:80] if post_caption else "[Audio]"
        elif post_response.sticker:
            msg_type = "sticker"
            snippet = "[Sticker]"
        else:
            msg_type = "custom"
            snippet = "[Media]"

        # Step 2: Buttons
        btn_prompt = (
            "🔘 **Step 2 of 3: Inline URL Buttons (Optional)**\n\n"
            "Send buttons in this format:\n"
            "`Button 1 - https://example.com | Button 2 - https://example.com`\n\n"
            "Or send `/pass` to continue without buttons.\n"
            "*(Send /cancel to abort)*"
        )
        btn_response: Message = await message.chat.ask(btn_prompt, timeout=180)
        btn_text = btn_response.text.strip() if btn_response.text else ""

        if btn_text.lower() in ["/cancel", "/exit"]:
            await message.reply("❌ Post composition cancelled.")
            return

        custom_buttons = None
        if btn_text and btn_text.lower() != "/pass":
            try:
                custom_buttons = await string_to_buttons(btn_text, include_mandatory=False)
            except Exception as e:
                log.warning(f"Could not parse custom buttons: {e}")
                await message.reply("⚠️ Could not parse buttons format. Proceeding with no buttons.")

        # Step 3: Timer
        timer_prompt = (
            "⏱️ **Step 3 of 3: Auto-Delete Timer (Optional)**\n\n"
            "Delete the post(s) automatically after:\n"
            "• `10m` = 10 minutes\n"
            "• `1h` = 1 hour\n"
            "• `24h` = 24 hours\n\n"
            "Or send `/pass` to keep the post permanently.\n"
            "*(Send /cancel to abort)*"
        )
        timer_response: Message = await message.chat.ask(timer_prompt, timeout=180)
        timer_text = timer_response.text.strip() if timer_response.text else ""

        if timer_text.lower() in ["/cancel", "/exit"]:
            await message.reply("❌ Post composition cancelled.")
            return

        timer_seconds = parse_timer_seconds(timer_text)

        # Store draft
        draft_id = f"cmd_{owner_id}_{int(time.time())}"
        _POST_DRAFTS[draft_id] = {
            "targets": ok,
            "source_chat_id": post_response.chat.id,
            "source_message_id": post_response.id,
            "msg_type": msg_type,
            "snippet": snippet,
            "post_text": post_text,
            "post_caption": post_caption,
            "post_entities": post_entities,
            "buttons_text": btn_text if btn_text != "/pass" else None,
            "timer_seconds": timer_seconds
        }

        timer_label = format_duration(timer_seconds)
        await message.reply(
            f"👀 **Post Preview & Confirmation**\n\n"
            f"• 📢 **Target Channels:** `{len(ok)}`\n"
            f"• 📎 **Type:** `{msg_type.upper()}`\n"
            f"• {timer_label.replace('⏳', '⏱️', 1) if timer_seconds else timer_label}\n"
            f"• 🔗 **Link Preview:** Disabled ❌\n\n"
            f"👇 **Below is a preview of your post:**"
        )

        try:
            if msg_type == "text":
                await client.send_message(
                    chat_id=owner_id, text=post_text, entities=post_entities,
                    reply_markup=custom_buttons, **PREVIEW_OPTIONS
                )
            else:
                await post_response.copy(chat_id=owner_id, reply_markup=custom_buttons)
        except Exception as pe:
            log.warning(f"Could not render preview copy: {pe}")

        confirm_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ Approve & Send ({len(ok)})", callback_data=f"approve_send_{draft_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_send_{draft_id}")
            ]
        ])
        await message.reply("Review the preview above. Ready to publish?", reply_markup=confirm_markup)

    except asyncio.TimeoutError:
        await message.reply("⚠️ Post composition timed out. Please try again.")
    except Exception as e:
        log.error(f"Error in /send wizard: {e}", exc_info=True)
        await message.reply(f"⚠️ Error during post composition: `{e}`")


@Client.on_callback_query(filters.regex(r"^approve_send_(.+)$"))
async def callback_approve_send(client: Client, query):
    owner_id = query.from_user.id
    if owner_id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return

    draft_id = query.matches[0].group(1)
    draft = _POST_DRAFTS.pop(draft_id, None)

    if not draft:
        await query.answer("⚠️ Draft expired or already published.", show_alert=True)
        return

    targets = draft["targets"]
    timer_seconds = draft.get("timer_seconds")

    custom_buttons = None
    if draft.get("buttons_text"):
        try:
            custom_buttons = await string_to_buttons(draft["buttons_text"], include_mandatory=False)
        except Exception:
            pass

    status_msg = await query.message.edit_text(f"⏳ Publishing to **{len(targets)}** channel(s)... 0/{len(targets)}")

    published, failed = [], []   # published: (chat_id, title, msg_id, link)
    last_edit = 0.0

    for idx, (chat_id, title) in enumerate(targets, start=1):
        try:
            sent_msg = await _publish_once(client, chat_id, draft, custom_buttons)
            sent_msg_id = sent_msg.id

            auto_delete_at = auto_delete_ts = None
            if timer_seconds and timer_seconds > 0:
                auto_delete_ts = time.time() + timer_seconds
                auto_delete_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=timer_seconds)
                asyncio.create_task(schedule_in_memory_delete(client, chat_id, sent_msg_id, timer_seconds))

            perms = await get_bot_channel_permissions(client, chat_id)
            direct_link = perms.get("direct_link") or f"https://t.me/c/{abs(chat_id)}/1"

            await client.db.save_bot_post(
                channel_id=chat_id, message_id=sent_msg_id, channel_title=title,
                post_type=draft["msg_type"], text_snippet=draft["snippet"],
                auto_delete_at=auto_delete_at, auto_delete_ts=auto_delete_ts, direct_link=direct_link
            )
            published.append((chat_id, title, sent_msg_id, direct_link))
            log.info(f"Posted #{sent_msg_id} to {title} ({chat_id})")
        except Exception as e:
            log.error(f"Failed to post to {chat_id}: {e}")
            failed.append((chat_id, title, str(e)[:80]))

        if len(targets) > 1:
            if time.time() - last_edit > 3 or idx == len(targets):
                last_edit = time.time()
                try:
                    await status_msg.edit_text(f"⏳ Publishing... **{idx}/{len(targets)}** ✅ {len(published)} ❌ {len(failed)}")
                except Exception:
                    pass
            await asyncio.sleep(0.4)   # gentle pacing

    timer_note = format_duration(timer_seconds)

    # ---- single channel: keep the familiar result card ----
    if len(targets) == 1 and published:
        chat_id, title, msg_id, link = published[0]
        await status_msg.edit_text(
            f"🎉 **Post Successfully Published!**\n\n"
            f"• **Channel:** [{title}]({link})\n"
            f"• **Channel ID:** `{chat_id}`\n"
            f"• **Post ID:** `{msg_id}`\n"
            f"• **Auto-Delete Timer:** {timer_note}\n"
            f"• **Link Preview:** Disabled ❌\n\n"
            f"💡 Delete later: `/delete {msg_id} {chat_id}`",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑️ Delete Now", callback_data=f"del_post_{chat_id}_{msg_id}"),
                InlineKeyboardButton("🔗 View in Channel", url=f"{link}/{msg_id}")
            ]]),
            disable_web_page_preview=True
        )
        return

    # ---- multi channel summary ----
    lines = [f"🎉 **Posted to {len(published)}/{len(targets)} channel(s)**\n⏱️ Timer: {timer_note}\n"]
    for chat_id, title, msg_id, link in published:
        lines.append(f"✅ [{title}]({link}/{msg_id}) — `#{msg_id}`")
    for chat_id, title, why in failed:
        lines.append(f"❌ **{title}** (`{chat_id}`) — {why}")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3850].rsplit("\n", 1)[0] + "\n… (list shortened)"

    rows = []
    if published:
        batch_id = f"{int(time.time())}{owner_id % 1000}"
        _BATCHES[batch_id] = [(c, m) for c, _t, m, _l in published]
        while len(_BATCHES) > 20:
            _BATCHES.pop(next(iter(_BATCHES)))
        rows.append([InlineKeyboardButton(f"🗑️ Delete All ({len(published)})", callback_data=f"del_batch_{batch_id}")])
    rows.append([
        InlineKeyboardButton("📑 Posted Messages", callback_data="owner_bot_posts_1"),
        InlineKeyboardButton("👑 Owner Dashboard", callback_data="owner_panel")
    ])
    await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^del_batch_(\w+)$"))
async def callback_delete_batch(client: Client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return
    items = _BATCHES.pop(query.matches[0].group(1), None)
    if not items:
        await query.answer("⚠️ Already deleted or expired (bot restarted). Use /posts to delete individually.", show_alert=True)
        return

    await query.answer("🗑️ Deleting...")
    done = 0
    for chat_id, msg_id in items:
        try:
            await client.delete_messages(chat_id, msg_id)
            await client.db.mark_post_deleted(chat_id, msg_id)
            done += 1
        except FloodWait as e:
            await asyncio.sleep(min(e.value + 1, 60))
        except Exception as e:
            log.warning(f"Could not delete #{msg_id} in {chat_id}: {e}")
        await asyncio.sleep(0.3)
    await query.message.edit_text(f"🗑️ **Deleted {done}/{len(items)} posts.**")


@Client.on_callback_query(filters.regex(r"^cancel_send_(.+)$"))
async def callback_cancel_send(client: Client, query):
    draft_id = query.matches[0].group(1)
    _POST_DRAFTS.pop(draft_id, None)
    await query.message.edit_text("❌ Post draft cancelled and discarded.")


@Client.on_message(filters.command("delete") & filters.private)
async def cmd_delete_post(client: Client, message: Message):
    """
    /delete <post_id> <channel_id>
    Also supports:
      /delete <channel_id> <post_id>
      /delete <telegram_post_link>
    """
    if message.from_user.id not in Config.owner:
        return

    args = message.command[1:]
    if not args:
        await message.reply(
            "📖 **How to Delete a Post:**\n\n"
            "Usage: `/delete <post_id> <channel_id>`\n"
            "Example: `/delete 123 -100123456789`\n\n"
            "You can also use a post link:\n"
            "`/delete https://t.me/c/123456789/123`",
            quote=True
        )
        return

    chat_id = None
    msg_id = None

    # Check if first arg is a Telegram post link: https://t.me/c/123456789/456 or https://t.me/username/456
    link_match = re.search(r"t\.me/(?:c/)?([A-Za-z0-9_]+)/(\d+)", args[0])
    if link_match:
        part1 = link_match.group(1)
        msg_id = int(link_match.group(2))
        if part1.isdigit():
            chat_id = int(f"-100{part1}")
        else:
            try:
                chat = await client.get_chat(part1)
                chat_id = chat.id
            except Exception as e:
                await message.reply(f"⚠️ Could not resolve channel from link: `{e}`", quote=True)
                return

    # Check two arguments: /delete 123 -100123456789 or /delete -100123456789 123
    elif len(args) >= 2:
        arg1, arg2 = args[0].strip(), args[1].strip()
        # Find which is channel_id (starts with -100 or - or length > 8)
        if (arg1.startswith("-") or len(arg1) > 9) and arg2.isdigit():
            chat_id = int(arg1)
            msg_id = int(arg2)
        elif (arg2.startswith("-") or len(arg2) > 9) and arg1.isdigit():
            chat_id = int(arg2)
            msg_id = int(arg1)
        else:
            try:
                msg_id = int(arg1)
                chat_id = int(arg2)
            except ValueError:
                pass

    # Check single argument: /delete 123 -> look up in MongoDB
    elif len(args) == 1 and args[0].isdigit():
        msg_id = int(args[0])
        post_doc = await client.db.posts.find_one({'message_id': msg_id, 'deleted': {'$ne': True}})
        if post_doc:
            chat_id = post_doc.get("channel_id")
        else:
            await message.reply(
                f"⚠️ Channel ID not provided and post `#{msg_id}` not found in recent database records.\n\n"
                f"Please specify both: `/delete {msg_id} -100123456789`",
                quote=True
            )
            return

    if not chat_id or not msg_id:
        await message.reply(
            "⚠️ Could not parse post ID and channel ID.\n"
            "Usage: `/delete <post_id> <channel_id>`\n"
            "Example: `/delete 123 -100123456789`",
            quote=True
        )
        return

    # Execute deletion
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=msg_id)
        await client.db.mark_post_deleted(chat_id, msg_id)
        await message.reply(
            f"✅ **Post Successfully Deleted!**\n\n"
            f"• **Channel ID:** `{chat_id}`\n"
            f"• **Post ID:** `{msg_id}`\n"
            f"• **Status:** Removed from Telegram channel and marked deleted in database.",
            quote=True
        )
    except (ChatAdminRequired, RPCError) as e:
        await message.reply(
            f"❌ **Failed to Delete Post:** `{e}`\n\n"
            f"Ensure the bot is still an admin with **'Delete Messages'** permission in `{chat_id}`.",
            quote=True
        )
    except Exception as e:
        await message.reply(f"❌ Unexpected error deleting post: `{e}`", quote=True)


@Client.on_message(filters.command(["posts", "botposts", "posted", "postedmessages"]) & filters.private)
async def cmd_owner_bot_posts(client: Client, message: Message):
    if message.from_user.id not in Config.owner:
        return

    sent = await message.reply("🔄 Loading bot posts...", quote=True)

    class FakeMatch:
        def group(self, idx):
            return "1" if idx == 1 else None

    class FakeQuery:
        def __init__(self, msg, user):
            self.from_user = user
            self.message = msg
            self.matches = [FakeMatch()]
        async def answer(self, *args, **kwargs):
            pass

    await callback_owner_bot_posts(client, FakeQuery(sent, message.from_user))
