import os
import time
import logging
import asyncio
from bot.config import Config
from helpers.approver_helper import approve_and_welcome, send_welcome
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatPrivileges
from pyrogram import enums
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, RightForbidden

log = logging.getLogger(__name__)

# Reusable client, cache, and state
_user_client_instance = None
_user_client_session_val = None
_userbot_info = None  # {"id": int, "username": str, "first_name": str}
_pending_cache = {}   # {chat_id: (timestamp, count)}
_channel_entity_cache = {}  # {chat_id: (timestamp, entity)}
_userbot_admin_verified = set()  # {chat_id}
CACHE_TTL = 30        # Cache backlog count for 30s to prevent Telegram FloodWait


def reset_user_client():
    """Resets the cached userbot instance so newly generated sessions are picked up."""
    global _user_client_instance, _user_client_session_val, _userbot_info, _pending_cache, _channel_entity_cache, _userbot_admin_verified
    if _user_client_instance and _user_client_instance.is_connected():
        try:
            asyncio.create_task(_user_client_instance.disconnect())
        except Exception:
            pass
    _user_client_instance = None
    _user_client_session_val = None
    _userbot_info = None
    _pending_cache.clear()
    _channel_entity_cache.clear()
    _userbot_admin_verified.clear()


def invalidate_pending_cache(chat_id=None):
    """Invalidates the pending request count cache for a channel or entirely."""
    global _pending_cache
    if chat_id is not None:
        cid = int(chat_id)
        _pending_cache.pop(cid, None)
        _pending_cache.pop(str(cid), None)
    else:
        _pending_cache.clear()


async def get_user_client(bot_client):
    """
    Returns an authorized, connected Telethon TelegramClient and userbot info dict.
    Reuses connection across calls to minimize connect latency.
    """
    global _user_client_instance, _user_client_session_val, _userbot_info

    session_val = await bot_client.db.get_setting("userbot_session")
    if not session_val:
        session_val = Config.user_session

    if not session_val:
        return None, None

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # If session changed or instance is None, re-instantiate
    if _user_client_instance is None or _user_client_session_val != session_val:
        if _user_client_instance and _user_client_instance.is_connected():
            try:
                await _user_client_instance.disconnect()
            except Exception:
                pass

        if len(session_val) > 40 and not os.path.exists(session_val):
            _user_client_instance = TelegramClient(StringSession(session_val), Config.api_id, Config.api_hash)
        elif os.path.exists(session_val) or os.path.exists(f"{session_val}.session"):
            _user_client_instance = TelegramClient(session_val, Config.api_id, Config.api_hash)
        else:
            return None, None

        _user_client_session_val = session_val
        _userbot_info = None

    if not _user_client_instance.is_connected():
        await _user_client_instance.connect()

    if not await _user_client_instance.is_user_authorized():
        await notify_owners_session_expired(bot_client, "Session unauthorized or revoked")
        return None, None

    if _userbot_info is None:
        me = await _user_client_instance.get_me()
        _userbot_info = {
            "id": me.id,
            "username": me.username or "",
            "first_name": me.first_name or ""
        }
        # Pre-populate dialogs and entity cache so entity lookups succeed immediately
        try:
            await _user_client_instance.get_dialogs(limit=100)
        except Exception as e:
            log.warning(f"Could not pre-fetch userbot dialogs: {e}")

    return _user_client_instance, _userbot_info


_last_expired_notice = 0.0
EXPIRED_NOTICE_COOLDOWN = 6 * 3600  # at most one alert per 6h, no matter who triggers it


async def notify_owners_session_expired(bot_client, error_msg="Session expired or invalid"):
    """Notifies all bot owners when the Userbot session expires (rate limited)."""
    global _last_expired_notice
    now = time.time()
    if now - _last_expired_notice < EXPIRED_NOTICE_COOLDOWN:
        return
    _last_expired_notice = now
    for owner_id in Config.owner:
        try:
            buttons = [[InlineKeyboardButton("📱 Generate New Session via OTP", callback_data="owner_gen_session")]]
            await bot_client.send_message(
                chat_id=int(owner_id),
                text=(
                    f"🚨 **ALERT: Userbot Session Expired / Invalid!**\n\n"
                    f"**Details:** `{error_msg}`\n\n"
                    f"The bot cannot approve old pre-existing backlog requests until a new session is generated.\n"
                    f"👇 Tap below to generate a new session right now with Phone & OTP:"
                ),
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            log.warning(f"Could not notify owner {owner_id} about session expiry: {e}")


async def _prime_userbot_peer(bot_client, chat_id, userbot_info):
    """
    Pyrogram can only use a user id it has seen before. The userbot is a stranger to the
    bot, so promote/get_chat_member fail with PeerIdInvalid. Resolve it by username, or by
    searching the channel's members, so the bot's peer cache learns it.
    """
    uid = userbot_info["id"]
    try:
        await bot_client.resolve_peer(uid)
        return True
    except Exception:
        pass
    if userbot_info.get("username"):
        try:
            await bot_client.get_users(userbot_info["username"])
            return True
        except Exception:
            pass
    try:
        async for m in bot_client.get_chat_members(chat_id, query=userbot_info.get("first_name") or "", limit=200):
            if m.user and m.user.id == uid:
                return True
    except Exception as e:
        log.debug(f"Could not prime userbot peer in {chat_id}: {e}")
    return False


async def ensure_userbot_admin(bot_client, chat_id):
    """
    Ensures that the Userbot account is added to the channel/group and promoted
    as an Administrator with invite/join-request permissions.
    """
    chat_id = int(chat_id)
    if chat_id in _userbot_admin_verified:
        return True

    user_client, userbot_info = await get_user_client(bot_client)
    if not user_client or not userbot_info:
        log.warning("Userbot session not active; cannot add userbot to channel")
        return False

    userbot_id = userbot_info["id"]
    await _prime_userbot_peer(bot_client, chat_id, userbot_info)

    # 1. Already an admin with invite rights?
    try:
        member = await bot_client.get_chat_member(chat_id, userbot_id)
        if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            if getattr(member.privileges, "can_invite_users", False):
                _userbot_admin_verified.add(chat_id)
                return True
    except Exception:
        pass

    # 2. Try direct promotion
    for priv in [
        ChatPrivileges(can_manage_chat=True, can_invite_users=True),
        ChatPrivileges(can_invite_users=True)
    ]:
        try:
            await bot_client.promote_chat_member(chat_id, userbot_id, privileges=priv)
            _userbot_admin_verified.add(chat_id)
            log.info(f"Promoted Userbot ({userbot_id}) to admin in {chat_id} directly.")
            return True
        except (ChatAdminRequired, RightForbidden) as e:
            log.warning(f"Bot lacks promote members permission in {chat_id}: {e}")
            break
        except Exception:
            continue

    # 3. Join (username / invite link), then promote
    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.errors import UserAlreadyParticipantError, InviteRequestSentError

        joined = False
        try:
            chat = await bot_client.get_chat(chat_id)
            if chat.username:
                await user_client(JoinChannelRequest(chat.username))
                joined = True
        except Exception:
            pass

        if not joined:
            invite_link = None
            try:
                invite = await bot_client.create_chat_invite_link(chat_id, creates_join_request=False)
                invite_link = invite.invite_link
            except Exception as e:
                log.warning(f"Could not generate invite link for Userbot in {chat_id}: {e}")

            if invite_link:
                if "+" in invite_link:
                    inv_hash = invite_link.split("+")[-1].strip()
                elif "joinchat/" in invite_link:
                    inv_hash = invite_link.split("joinchat/")[-1].strip()
                else:
                    inv_hash = invite_link.split("/")[-1].strip()

                try:
                    await user_client(ImportChatInviteRequest(inv_hash))
                    joined = True
                except UserAlreadyParticipantError:
                    joined = True
                except InviteRequestSentError:
                    try:
                        await bot_client.approve_chat_join_request(chat_id, userbot_id)
                        joined = True
                        log.info(f"Approved Userbot ({userbot_id}) join request in {chat_id}")
                    except Exception as ae:
                        log.warning(f"Could not approve Userbot join request: {ae}")
                except Exception as e:
                    log.warning(f"Userbot ImportChatInviteRequest failed in {chat_id}: {e}")

        # Now that the userbot is a member the bot can see it -> prime again, then promote
        await _prime_userbot_peer(bot_client, chat_id, userbot_info)
        for priv in [
            ChatPrivileges(can_manage_chat=True, can_invite_users=True),
            ChatPrivileges(can_invite_users=True)
        ]:
            try:
                await bot_client.promote_chat_member(chat_id, userbot_id, privileges=priv)
                _userbot_admin_verified.add(chat_id)
                log.info(f"Userbot joined and was promoted to admin in {chat_id}")
                try:
                    await user_client.get_dialogs(limit=50)
                except Exception:
                    pass
                return True
            except Exception as pe:
                log.warning(f"Promote attempt failed in {chat_id}: {pe}")
                continue

    except Exception as e:
        log.warning(f"ensure_userbot_admin failed for chat {chat_id}: {e}")

    return False


async def remove_userbot_from_channel(bot_client, chat_id):
    """
    Demotes the userbot and removes it from the channel so it does not remain a member or admin.
    """
    chat_id = int(chat_id)
    _userbot_admin_verified.discard(chat_id)
    _channel_entity_cache.pop(chat_id, None)
    invalidate_pending_cache(chat_id)

    user_client, userbot_info = await get_user_client(bot_client)
    userbot_id = userbot_info["id"] if userbot_info else None

    # 1. Demote admin rights via bot client
    if userbot_id:
        try:
            await bot_client.promote_chat_member(
                chat_id,
                userbot_id,
                privileges=ChatPrivileges(
                    can_manage_chat=False,
                    can_post_messages=False,
                    can_edit_messages=False,
                    can_delete_messages=False,
                    can_restrict_members=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                    can_promote_members=False,
                    can_change_info=False
                )
            )
            log.info(f"Demoted userbot {userbot_id} privileges in {chat_id}")
        except Exception as e:
            log.debug(f"Demote userbot failed in {chat_id} (may not be admin): {e}")

    # 2. Leave channel via Telethon (userbot)
    left = False
    if user_client:
        try:
            from telethon.tl.functions.channels import LeaveChannelRequest
            entity = await get_telethon_channel_entity(user_client, chat_id)
            await user_client(LeaveChannelRequest(entity))
            log.info(f"Userbot left channel {chat_id} via Telethon LeaveChannelRequest")
            left = True
        except Exception as e:
            log.debug(f"Userbot LeaveChannelRequest error in {chat_id}: {e}")

    # 3. Fallback: If still in channel, bot kicks & unbans to ensure departure
    if userbot_id:
        try:
            member = await bot_client.get_chat_member(chat_id, userbot_id)
            if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.RESTRICTED]:
                await bot_client.ban_chat_member(chat_id, userbot_id)
                await bot_client.unban_chat_member(chat_id, userbot_id)
                log.info(f"Userbot removed via bot ban/unban fallback in {chat_id}")
        except (UserNotParticipant, ChatAdminRequired):
            pass
        except Exception as e:
            log.debug(f"Bot ban/unban check in {chat_id}: {e}")

    return True


async def get_telethon_channel_entity(user_client, chat_id):
    """
    Robustly resolves a Telethon channel entity from a Bot-API style chat_id (-100xxxxxxxxxx).
    Handles StringSession (no on-disk cache) by warming dialogs when needed.
    """
    from telethon import utils as tl_utils

    chat_id = int(chat_id)
    real_id, peer_cls = tl_utils.resolve_id(chat_id)   # -1001234 -> (1234, PeerChannel)

    cached = _channel_entity_cache.get(chat_id)
    if cached and time.time() - cached[0] < 300:
        return cached[1]

    async def _try():
        for target in (peer_cls(real_id), chat_id):    # typed peer first; bare positive ids are users
            try:
                entity = await user_client.get_entity(target)
                _channel_entity_cache[chat_id] = (time.time(), entity)
                return entity
            except Exception:
                pass
        return None

    entity = await _try()
    if entity:
        return entity

    try:
        dialogs = await user_client.get_dialogs(limit=200)
    except Exception as e:
        log.warning(f"get_dialogs error in entity resolver: {e}")
        dialogs = []

    entity = await _try()
    if entity:
        return entity
    for d in dialogs:
        if getattr(d.entity, "id", None) == real_id:
            _channel_entity_cache[chat_id] = (time.time(), d.entity)
            return d.entity

    raise ValueError(f"Could not resolve Telegram entity for channel {chat_id}")


# ---------------------------------------------------------------------------
# MTProto backlog access
# ---------------------------------------------------------------------------
async def _fetch_importers(user_client, entity, limit, cursor=None):
    """
    One page of PENDING join requests. offset_date / offset_user are REQUIRED by Telegram's
    messages.getChatInviteImporters (the old code omitted them -> TypeError -> always 0).
    cursor = (date, InputUser) of the last row of the previous page.
    """
    from telethon.tl.functions.messages import GetChatInviteImportersRequest
    from telethon.tl.types import InputUserEmpty

    offset_date, offset_user = cursor if cursor else (None, InputUserEmpty())
    return await user_client(GetChatInviteImportersRequest(
        peer=entity,
        offset_date=offset_date,
        offset_user=offset_user,
        limit=limit,
        requested=True,
    ))


async def get_channel_pending_count(bot_client, chat_id, force_refresh=False, allow_add_userbot=False):
    """
    Number of pending join requests: max(Telegram backlog via userbot, local DB queue).
    Read-only for list views. Only adds/connects userbot if allow_add_userbot=True or userbot is already verified admin.
    """
    chat_id = int(chat_id)
    now = time.time()

    if not force_refresh and chat_id in _pending_cache:
        cached_time, cached_val = _pending_cache[chat_id]
        if now - cached_time < CACHE_TTL:
            return cached_val

    try:
        db_count = await bot_client.db.total_pending_count(chat_id)
    except Exception:
        db_count = 0

    # Do NOT add userbot if not explicitly authorized and not already in channel
    if chat_id not in _userbot_admin_verified and not allow_add_userbot:
        _pending_cache[chat_id] = (now, db_count)
        return db_count

    user_client, _ = await get_user_client(bot_client)
    if not user_client:
        _pending_cache[chat_id] = (now, db_count)
        return db_count

    try:
        async def _fetch():
            if allow_add_userbot and chat_id not in _userbot_admin_verified:
                await ensure_userbot_admin(bot_client, chat_id)
            entity = await get_telethon_channel_entity(user_client, chat_id)
            res = await _fetch_importers(user_client, entity, limit=1)
            count = getattr(res, "count", 0) or 0
            if count == 0 and getattr(res, "importers", None):
                count = len(res.importers)
            return count

        telegram_count = await asyncio.wait_for(_fetch(), timeout=15.0)
        final_count = max(telegram_count, db_count)
        _pending_cache[chat_id] = (now, final_count)
        return final_count

    except Exception as e:
        log.warning(f"Could not get Telegram backlog count for {chat_id}: {type(e).__name__}: {e}")
        _pending_cache[chat_id] = (now, db_count)
        return db_count


# ---------------------------------------------------------------------------
# Batch approval
# ---------------------------------------------------------------------------
_approval_locks = {}   # {chat_id: asyncio.Lock}  - stops double-tap / overlapping runs
_last_issue = {}       # {chat_id: str}           - why the last run came up short


def get_last_issue(chat_id):
    return _last_issue.get(int(chat_id))


def issue_suffix(chat_id):
    issue = get_last_issue(chat_id)
    return f"\n\n⚠️ {issue}" if issue else ""


def _rpc_text(e):
    return f"{type(e).__name__}: {getattr(e, 'message', None) or e}"


async def approve_batch_requests(bot_client, chat_id, count=100):
    """
    Approves up to `count` pending join requests.
      1. Drain the local DB queue (manual-mode requests) using the bot.
      2. Sweep Telegram's real backlog with the userbot, which ALSO approves them
         (the bot cannot: it has never 'seen' those users, so Pyrogram raises PeerIdInvalid).
    Welcome DMs are best effort (a bot cannot DM someone who never started it).
    The reason for a short result is available via get_last_issue(chat_id).
    """
    chat_id = int(chat_id)
    lock = _approval_locks.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        _last_issue[chat_id] = "An approval run is already in progress for this channel. Wait for it to finish."
        return 0
    async with lock:
        try:
            return await _approve_batch_locked(bot_client, chat_id, count)
        finally:
            invalidate_pending_cache(chat_id)


async def _approve_batch_locked(bot_client, chat_id, count):
    _last_issue.pop(chat_id, None)

    chat_title = ""
    try:
        chat = await bot_client.get_chat(chat_id)
        chat_title = chat.title or ""
    except Exception as e:
        log.warning(f"Could not fetch chat title for {chat_id}: {e}")

    approved_count = 0
    bot_could_not = 0

    # ---- 1. local DB queue, approved by the bot ----
    for item in await bot_client.db.get_pending_requests(chat_id, limit=count):
        if approved_count >= count:
            break
        u_id = item["user_id"]
        if await approve_and_welcome(bot_client, chat_id, u_id, item.get("name", ""), chat_title):
            approved_count += 1
        else:
            bot_could_not += 1   # stays pending on Telegram; the userbot sweep below picks it up
        await asyncio.sleep(0.4)

    if approved_count >= count:
        if chat_id in _userbot_admin_verified:
            try:
                await remove_userbot_from_channel(bot_client, chat_id)
            except Exception:
                pass
        return approved_count

    # ---- 2. Telegram backlog, approved by the userbot ----
    user_client, _ = await get_user_client(bot_client)
    if not user_client:
        _last_issue[chat_id] = (
            "Userbot session is not active, so old backlog requests can't be read. "
            "Open Owner Panel → Userbot Session Manager and generate a session."
            + (f" ({bot_could_not} queued request(s) could not be approved by the bot alone.)" if bot_could_not else "")
        )
        return approved_count

    try:
        if chat_id not in _userbot_admin_verified:
            await ensure_userbot_admin(bot_client, chat_id)
        approved_count += await _userbot_sweep(
            bot_client, user_client, chat_id, chat_title, count - approved_count
        )
    except Exception as e:
        text = _rpc_text(e)
        log.error(f"Error sweeping MTProto backlog for {chat_id}: {text}")
        if "CHAT_ADMIN_REQUIRED" in text.upper() or "ChatAdminRequired" in text:
            _last_issue[chat_id] = ("The userbot is not an admin with 'Invite Users' rights in this chat. "
                                    "Make the bot able to add admins, or promote the userbot manually.")
        else:
            _last_issue[chat_id] = f"Userbot error: {text[:150]}"
        if "AuthKey" in text or "session" in text.lower():
            await notify_owners_session_expired(bot_client, text)
    finally:
        # After completing the batch approval, userbot MUST leave the channel
        try:
            await remove_userbot_from_channel(bot_client, chat_id)
        except Exception as le:
            log.warning(f"Error removing userbot after batch approval in {chat_id}: {le}")

    return approved_count


async def _userbot_sweep(bot_client, user_client, chat_id, chat_title, remaining):
    """Page through pending requests with a cursor and approve each via the userbot."""
    from telethon import utils as tl_utils
    from telethon.tl.functions.messages import HideChatJoinRequestRequest
    from telethon.errors import FloodWaitError, RPCError

    entity = await get_telethon_channel_entity(user_client, chat_id)
    approved, skipped, cursor = 0, 0, None

    while approved < remaining:
        res = await _fetch_importers(user_client, entity, min(100, max(1, remaining - approved)), cursor)
        importers = list(getattr(res, "importers", None) or [])
        if not importers:
            break
        users = {u.id: u for u in (getattr(res, "users", None) or [])}

        for imp in importers:
            if approved >= remaining:
                break
            user_obj = users.get(imp.user_id)
            if user_obj is None:
                skipped += 1
                continue

            for attempt in range(3):
                try:
                    await user_client(HideChatJoinRequestRequest(
                        peer=entity, user_id=tl_utils.get_input_user(user_obj), approved=True
                    ))
                    approved += 1
                    await bot_client.db.remove_pending(chat_id, imp.user_id)
                    # best-effort welcome; never lets a failed DM undo an approval
                    try:
                        await send_welcome(bot_client, chat_id, imp.user_id, user_obj.first_name or "", chat_title)
                    except Exception as we:
                        log.debug(f"Welcome failed for {imp.user_id}: {we}")
                    break
                except FloodWaitError as fw:
                    log.warning(f"FloodWait {fw.seconds}s while approving in {chat_id}")
                    await asyncio.sleep(fw.seconds + 1)
                except RPCError as re_:
                    msg = (getattr(re_, "message", "") or type(re_).__name__).upper()
                    if "HIDE_REQUESTER_MISSING" in msg:
                        await bot_client.db.remove_pending(chat_id, imp.user_id)  # request is gone
                    elif "CHAT_ADMIN_REQUIRED" in msg:
                        raise
                    else:
                        log.warning(f"Could not approve {imp.user_id} in {chat_id}: {msg}")
                    skipped += 1
                    break
            await asyncio.sleep(0.4)

        last = importers[-1]
        last_user = users.get(last.user_id)
        if last_user is None:
            break
        cursor = (last.date, tl_utils.get_input_user(last_user))   # failed rows are skipped, not retried forever

    return approved
