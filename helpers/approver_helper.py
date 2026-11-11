import logging
import asyncio
import inspect
from bot.config import Config
from pyrogram.errors import (
    UserAlreadyParticipant, UserIsBlocked, PeerIdInvalid,
    FloodWait, UserChannelsTooMuch, InputUserDeactivated,
    UserDeactivated, UserDeactivatedBan, ChatWriteForbidden,
    Forbidden, UserBlocked, YouBlockedUser, BadRequest
)
from helpers.str_to_buttons import string_to_buttons
from helpers.utils import format_welcome_text  # noqa: F401  (re-exported for other modules)

# Safe link preview configuration across Pyrogram versions
def _get_preview_options():
    try:
        from pyrogram import Client
        sig = inspect.signature(Client.send_message)
        if "link_preview_options" in sig.parameters:
            from pyrogram.types import LinkPreviewOptions
            return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        pass
    return {"disable_web_page_preview": True}

PREVIEW_OPTIONS = _get_preview_options()

log = logging.getLogger(__name__)

# Default hardcoded preset
PRESET_TEXT = "Hello {name},\n\n**Join Our Daily Deals Channel to Save your Hard earned Money:**\n\nhttps://t.me/+9b9PYr2PPZ9lODI1\nhttps://t.me/+9b9PYr2PPZ9lODI1"
PRESET_BUTTONS = "📢 Join Our All Channels - https://t.me/addlist/-QPHAwpJSb9hNTZl"

# Errors indicating Telegram does not allow messaging this user (never started bot, blocked, deactivated)
DM_BLOCKED_ERRORS = (
    UserIsBlocked, PeerIdInvalid, InputUserDeactivated,
    UserDeactivated, UserDeactivatedBan, ChatWriteForbidden,
    Forbidden, UserBlocked, YouBlockedUser, KeyError, ValueError
)


_MEDIA_SENDERS = {
    "photo": "send_photo",
    "video": "send_video",
    "animation": "send_animation",
    "document": "send_document",
}
CAPTION_LIMIT = 1024


async def _deliver(client, user_id, text, markup, media_type, media_file_id, plain):
    """One delivery attempt (text or media+caption)."""
    extra = {"parse_mode": None} if plain else {}
    uid = int(user_id)
    sender = _MEDIA_SENDERS.get(media_type) if (media_type and media_file_id) else None

    if sender:
        fits = bool(text) and len(text) <= CAPTION_LIMIT
        await getattr(client, sender)(
            uid,
            media_file_id,
            caption=text if fits else None,
            reply_markup=markup if (fits or not text) else None,
            **extra
        )
        if text and not fits:
            # Caption too long for Telegram -> send the text as a second message
            await client.send_message(uid, text=text, reply_markup=markup, **PREVIEW_OPTIONS, **extra)
        return

    await client.send_message(uid, text=text, reply_markup=markup, **PREVIEW_OPTIONS, **extra)


async def _send_dm_safe(client, user_id: int, text: str, markup=None, media_type=None, media_file_id=None):
    """
    Safely delivers a DM (optionally with media) to user_id.
    - Handles FloodWait (max 2 waits).
    - Catches blocked/forbidden errors -> (False, False): bot cannot DM this user.
    - Falls back step by step: drop media -> drop buttons -> plain text.
    Returns: (sent: bool, can_dm: bool)
    """
    use_media = bool(media_type and media_file_id)
    use_markup = markup
    plain = False
    flood_waits = 0

    for _ in range(6):
        try:
            await _deliver(client, user_id, text, use_markup, media_type if use_media else None,
                           media_file_id if use_media else None, plain)
            return True, True
        except DM_BLOCKED_ERRORS as e:
            log.info(f"Cannot DM user {user_id}: {e.__class__.__name__}")
            return False, False
        except FloodWait as e:
            flood_waits += 1
            if flood_waits > 2:
                log.warning(f"Too many FloodWaits while DMing {user_id}")
                return False, True
            wait_time = min(e.value + 1, 60)
            log.warning(f"FloodWait {wait_time}s while sending DM to {user_id}")
            await asyncio.sleep(wait_time)
        except Exception as e:
            err_str = str(e).upper()
            if use_media and any(k in err_str for k in ("MEDIA", "FILE", "DOCUMENT", "PHOTO", "VIDEO", "WEBPAGE")):
                log.warning(f"Media error DMing {user_id}: {e}; retrying without media")
                use_media = False
                continue
            if use_markup and ("BUTTON" in err_str or "URL" in err_str):
                urls = [getattr(b, "url", None) for row in (getattr(use_markup, "inline_keyboard", None) or []) for b in row]
                log.warning(f"Markup error DMing {user_id}: {e}; button URLs: {urls}; retrying without buttons")
                use_markup = None
                continue
            if not plain:
                log.warning(f"Formatting error DMing {user_id}: {e}; retrying as plain text")
                plain = True
                continue
            log.error(f"Failed to DM {user_id}: {e}")
            return False, True
    return False, True


async def approve_and_welcome(client, chat_id, user_id, user_name="", chat_title=""):
    """
    Approves a join request with the BOT, then sends the welcome DMs (best effort).
    Returns True only if the request is now approved.
    """
    chat_id, user_id = int(chat_id), int(user_id)

    for attempt in range(3):
        try:
            await client.approve_chat_join_request(chat_id, user_id)
            log.info(f"Approved join request for {user_name} ({user_id}) in chat {chat_title} ({chat_id})")
            await client.db.inc_stat("approved")
            break
        except UserAlreadyParticipant:
            break
        except (UserChannelsTooMuch, InputUserDeactivated, UserDeactivated, UserDeactivatedBan) as e:
            log.info(f"Dropping {user_id} from queue in {chat_id}: {e.__class__.__name__}")
            await client.db.remove_pending(chat_id, user_id)
            return False
        except BadRequest as e:
            err_msg = str(e).upper()
            if any(x in err_msg for x in ["HIDE_REQUESTER_MISSING", "USER_ALREADY_PARTICIPANT"]):
                log.info(f"Join request already handled for {user_id} in {chat_id}: {e}")
                await client.db.remove_pending(chat_id, user_id)
                return True
            if "USER_CHANNELS_TOO_MUCH" in err_msg:
                log.info(f"User {user_id} in too many channels; dropping: {e}")
                await client.db.remove_pending(chat_id, user_id)
                return False
            log.error(f"BadRequest approving join request {user_id} in {chat_id}: {e}")
            return False
        except (PeerIdInvalid, KeyError, ValueError) as e:
            log.warning(f"Bot cannot resolve user {user_id} for {chat_id} ({e.__class__.__name__}); leaving in queue")
            return False
        except FloodWait as e:
            log.warning(f"FloodWait in approver: sleeping {e.value}s")
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            log.error(f"Error approving join request {user_id} in {chat_id}: {e}")
            return False
    else:
        return False

    await client.db.remove_pending(chat_id, user_id)

    try:
        await send_welcome(client, chat_id, user_id, user_name, chat_title)
    except Exception as e:
        # The user IS approved; a failed DM must never turn that into a failure
        log.error(f"Unexpected error sending welcome to {user_id}: {e}")
    return True


async def send_welcome(client, chat_id, user_id, user_name="", chat_title=""):
    """
    Delivers channel welcome (with optional per-channel media) + global/preset welcome + log-channel note.
    Returns True if a DM was delivered.
    """
    chat_id, user_id = int(chat_id), int(user_id)
    welcome_sent = False
    can_dm = True
    global_done = False

    # 1. Channel custom welcome + per-channel media
    channel_text, channel_buttons = await client.db.get_welcome(chat_id)
    media_type, media_file_id = await client.db.get_welcome_media(chat_id)
    has_custom = bool(channel_text and channel_text.strip())

    # 2. Owner global welcome (or hardcoded preset)
    if hasattr(client.db, "get_global_welcome"):
        owner_text, owner_buttons = await client.db.get_global_welcome()
    else:
        owner_text, owner_buttons = await client.db.get_welcome(0)

    has_owner_global = bool(owner_text and owner_text.strip())
    global_text = owner_text if has_owner_global else PRESET_TEXT
    global_buttons = owner_buttons if has_owner_global else PRESET_BUTTONS

    # 3a. Channel welcome (text + media)
    if has_custom and can_dm:
        formatted_text = format_welcome_text(channel_text, user_name, chat_title, user_id)
        channel_markup = await string_to_buttons(channel_buttons, include_mandatory=True)
        custom_sent, can_dm = await _send_dm_safe(
            client, user_id, formatted_text, channel_markup, media_type, media_file_id
        )
        if custom_sent:
            welcome_sent = True
            log.info(f"Delivered custom welcome DM to {user_id} in {chat_id}")

    # 3b. Channel has media but no custom text -> media + global text as caption
    elif media_type and media_file_id and can_dm and global_text:
        formatted_text = format_welcome_text(global_text, user_name, chat_title, user_id)
        global_markup = await string_to_buttons(global_buttons, include_mandatory=True)
        media_sent, can_dm = await _send_dm_safe(
            client, user_id, formatted_text, global_markup, media_type, media_file_id
        )
        global_done = True
        if media_sent:
            welcome_sent = True
            log.info(f"Delivered media welcome DM to {user_id} in {chat_id}")

    # 3c. Global welcome (alongside custom welcome, or standalone)
    if global_text and can_dm and not global_done:
        if not (has_custom and channel_text.strip() == global_text.strip()):
            if welcome_sent:
                await asyncio.sleep(0.5)

            formatted_global = format_welcome_text(global_text, user_name, chat_title, user_id)
            global_markup = await string_to_buttons(global_buttons, include_mandatory=True)
            global_sent, can_dm = await _send_dm_safe(client, user_id, formatted_global, global_markup)
            if global_sent:
                welcome_sent = True
                log.info(f"Delivered global welcome DM ({'owner' if has_owner_global else 'preset'}) to {user_id} for chat {chat_id}")

    # Stats + follow-up scheduling
    if welcome_sent:
        await client.db.inc_stat("dm_sent")
        try:
            from helpers.followup_helper import schedule_followup
            await schedule_followup(client, user_id, user_name, chat_title)
        except Exception as e:
            log.warning(f"Could not schedule follow-up for {user_id}: {e}")
    elif not can_dm:
        await client.db.inc_stat("dm_failed")

    # 3. Add to DB if successfully welcomed
    if welcome_sent:
        try:
            if not await client.db.is_user_exist(user_id):
                await client.db.add_user(user_id)
        except Exception as e:
            log.warning(f"Failed to record user {user_id} in DB: {e}")

    # 4. Notify log channel if configured
    if Config.log_channel and str(Config.log_channel).strip() not in ["", "None", "-100"]:
        try:
            bot_username = getattr(getattr(client, 'me', None), 'username', '') or 'bot'
            safe_name = str(user_name or "Friend").strip() or "Friend"
            clean_name = safe_name.replace('[', '').replace(']', '')
            safe_chat = str(chat_title or "our Channel").strip() or "our Channel"
            mention = f"[{clean_name}](tg://user?id={user_id})"
            log_tag = "#WELCOME_SENT" if welcome_sent else "#APPROVED"
            await client.send_message(
                int(Config.log_channel),
                f"{log_tag}: \n\n{mention} joined **{safe_chat}**\n\n@{bot_username} 😊",
                **PREVIEW_OPTIONS
            )
        except Exception as e:
            log.warning(f"Log channel notification failed: {e}")

    return welcome_sent
