import time
import logging
import asyncio
from pyrogram import Client, enums
from pyrogram.errors import (
    FloodWait, ChatAdminRequired, ChannelPrivate,
    ChannelInvalid, ChatInvalid, PeerIdInvalid, UserNotParticipant
)

log = logging.getLogger(__name__)

# In-memory permissions cache: {chat_id: {"data": dict, "expires_at": float}}
_PERMS_CACHE = {}
CACHE_TTL = 90  # seconds


async def get_bot_channel_permissions(client: Client, chat_id: int, force_refresh: bool = False):
    """
    Checks the bot's permissions in a given channel or supergroup strictly via the Bot client.
    Returns:
        dict: {
            "is_valid": bool,
            "title": str,
            "username": str or None,
            "chat_type": str,
            "can_post": bool,
            "can_delete": bool,
            "can_invite": bool,
            "direct_link": str or None
        }
    """
    cid = int(chat_id)
    now = time.time()

    if not force_refresh and cid in _PERMS_CACHE:
        cached = _PERMS_CACHE[cid]
        if cached["expires_at"] > now:
            return cached["data"]

    result = {
        "is_valid": False,
        "title": f"Channel {cid}",
        "username": None,
        "chat_type": "channel",
        "can_post": False,
        "can_delete": False,
        "can_invite": False,
        "direct_link": None
    }

    try:
        chat = await client.get_chat(cid)
        result["title"] = chat.title or f"Channel {cid}"
        result["username"] = chat.username
        result["chat_type"] = str(chat.type).split(".")[-1].lower() if chat.type else "channel"

        bot_member = await client.get_chat_member(cid, client.me.id)
        
        if bot_member.status == enums.ChatMemberStatus.OWNER:
            result["is_valid"] = True
            result["can_post"] = True
            result["can_delete"] = True
            result["can_invite"] = True
        elif bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR:
            result["is_valid"] = True
            priv = bot_member.privileges
            if priv:
                # Channels use can_post_messages, supergroups use can_send_messages or manage_chat
                is_channel = chat.type in [enums.ChatType.CHANNEL]
                if is_channel:
                    result["can_post"] = bool(getattr(priv, "can_post_messages", False))
                else:
                    # For groups / supergroups
                    result["can_post"] = bool(getattr(priv, "can_send_messages", True))
                
                result["can_delete"] = bool(getattr(priv, "can_delete_messages", False))
                result["can_invite"] = bool(getattr(priv, "can_invite_users", False))
            else:
                result["can_post"] = False
                result["can_delete"] = False
                result["can_invite"] = False
        else:
            result["is_valid"] = False

        # Generate direct invite link if valid
        if result["is_valid"]:
            direct_link = await get_or_create_direct_invite_link(client, chat, can_invite=result["can_invite"])
            result["direct_link"] = direct_link

        _PERMS_CACHE[cid] = {
            "data": result,
            "expires_at": now + CACHE_TTL
        }
        return result

    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant, ChannelInvalid, ChatInvalid, PeerIdInvalid) as e:
        log.warning(f"Bot lacks access to channel {cid}: {e}")
        result["is_valid"] = False
        _PERMS_CACHE[cid] = {"data": result, "expires_at": now + CACHE_TTL}
        return result

    except FloodWait as e:
        log.warning(f"FloodWait {e.value}s while checking permissions for {cid}")
        if cid in _PERMS_CACHE:
            return _PERMS_CACHE[cid]["data"]
        return result

    except Exception as e:
        err_str = str(e).upper()
        if any(x in err_str for x in ["CHAT_ADMIN_REQUIRED", "CHANNEL_PRIVATE", "USER_NOT_PARTICIPANT", "PEER_ID_INVALID"]):
            result["is_valid"] = False
            return result
        log.warning(f"Error checking bot permissions for {cid}: {e}")
        return result


async def get_or_create_direct_invite_link(client: Client, chat_obj_or_id, can_invite: bool = True) -> str:
    """
    Obtains or creates a DIRECT invite link (creates_join_request=False) strictly via Bot client.
    Priority:
    1. Public username: https://t.me/{username}
    2. Cached direct_link in MongoDB
    3. client.create_chat_invite_link(cid, creates_join_request=False)
    4. chat.invite_link fallback
    5. Clean t.me/c/{id}/1 fallback
    """
    if isinstance(chat_obj_or_id, int):
        cid = chat_obj_or_id
        username = None
        existing_invite = None
    else:
        cid = chat_obj_or_id.id
        username = chat_obj_or_id.username
        existing_invite = getattr(chat_obj_or_id, "invite_link", None)

    # 1. Public username link
    if username:
        link = f"https://t.me/{username}"
        try:
            await client.db.update_channel_direct_link(cid, link)
        except Exception:
            pass
        return link

    # 2. Check cached direct link in DB
    try:
        cached_link = await client.db.get_channel_direct_link(cid)
        if cached_link and ("t.me/+" in cached_link or "t.me/joinchat/" in cached_link or "t.me/" in cached_link):
            return cached_link
    except Exception:
        pass

    # 3. Create fresh direct link (strictly creates_join_request=False)
    if can_invite:
        try:
            invite = await client.create_chat_invite_link(
                chat_id=cid,
                creates_join_request=False,
                name="Bot Direct Invite"
            )
            if invite and invite.invite_link:
                try:
                    await client.db.update_channel_direct_link(cid, invite.invite_link)
                except Exception:
                    pass
                return invite.invite_link
        except FloodWait as e:
            log.warning(f"FloodWait {e.value}s creating invite link for {cid}")
        except Exception as e:
            log.warning(f"Could not create direct invite link for {cid}: {e}")

    # 4. Fallback to existing chat invite link if present
    if existing_invite:
        return existing_invite

    # 5. Fallback clean internal ID link
    clean_id = str(cid).replace("-100", "").replace("-", "")
    return f"https://t.me/c/{clean_id}/1"


async def get_all_verified_channels(client: Client, postable_only: bool = False):
    """
    Fetches all registered channels from MongoDB, verifies bot status via Telegram API,
    prunes invalid channels from database, and filters by postable permission if requested.
    Returns: list of verified channel dicts
    """
    raw_cursor = await client.db.get_all_channels()
    raw_channels = await raw_cursor.to_list(length=1000)
    if not raw_channels:
        return []

    async def verify_single(ch):
        cid = ch.get("channel_id")
        if not cid or int(cid) == 0:
            return None
        
        perms = await get_bot_channel_permissions(client, cid)
        
        # NOTE: We NEVER delete channels from DB! We preserve all records and strictly filter in-memory.
        if not perms.get("is_valid"):
            ch["can_post"] = False
            ch["can_delete"] = False
            ch["can_invite"] = False
            ch["is_valid"] = False
            if postable_only:
                return None
            return ch

        # Update title in doc if changed
        if perms.get("title") and perms["title"] != ch.get("title"):
            ch["title"] = perms["title"]
            await client.db.update_channel(cid, title=perms["title"])

        ch["title"] = perms["title"]
        ch["username"] = perms.get("username")
        ch["chat_type"] = perms.get("chat_type", "channel")
        ch["can_post"] = perms.get("can_post", False)
        ch["can_delete"] = perms.get("can_delete", False)
        ch["can_invite"] = perms.get("can_invite", False)
        ch["direct_link"] = perms.get("direct_link")
        ch["is_valid"] = True

        if postable_only and not perms.get("can_post"):
            return None

        return ch

    # Verify concurrently in chunks of 15 to prevent flood waits
    results = []
    chunk_size = 15
    for i in range(0, len(raw_channels), chunk_size):
        chunk = raw_channels[i:i + chunk_size]
        chunk_results = await asyncio.gather(*[verify_single(ch) for ch in chunk], return_exceptions=True)
        for res in chunk_results:
            if res and not isinstance(res, Exception):
                results.append(res)

    return results

