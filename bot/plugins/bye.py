"""
Bye message: when a member leaves a connected channel/group, the bot DMs
the owner-defined goodbye message (best effort - only works if the user has
started the bot / welcome DM was delivered before).
"""
import time
import logging
from pyrogram import Client, enums

from helpers.str_to_buttons import string_to_buttons
from helpers.utils import format_welcome_text
from helpers.followup_helper import DEFAULT_BYE_TEXT

log = logging.getLogger(__name__)

_RECENT = {}      # (chat_id, user_id) -> timestamp, avoids double-send on duplicate updates
_DEDUP_SECONDS = 120


def _is_recent(key):
    now = time.time()
    for k in [k for k, t in _RECENT.items() if now - t > _DEDUP_SECONDS]:
        _RECENT.pop(k, None)
    if key in _RECENT:
        return True
    _RECENT[key] = now
    return False


# NOTE: group=1 on purpose. Pyrogram runs only the FIRST matching handler per group,
# and auto_channel_connect (group 0) matches every chat-member update.
@Client.on_chat_member_updated(group=1)
async def bye_handler(client, update):
    try:
        old, new = update.old_chat_member, update.new_chat_member
        if not old:
            return

        member_user = old.user
        left_status = new.status if new else enums.ChatMemberStatus.LEFT
        if old.status != enums.ChatMemberStatus.MEMBER or left_status != enums.ChatMemberStatus.LEFT:
            return
        if not member_user or member_user.is_bot or member_user.id == client.me.id:
            return

        chat = update.chat
        if not await client.db.get_channel(chat.id):
            return  # not one of our connected chats

        # The user left -> a pending follow-up would look silly
        await client.db.delete_followup(member_user.id)

        if not await client.db.get_setting("bye_enabled", False):
            return
        if _is_recent((chat.id, member_user.id)):
            return

        from helpers.approver_helper import _send_dm_safe

        text = await client.db.get_setting("bye_text") or DEFAULT_BYE_TEXT
        buttons = await client.db.get_setting("bye_buttons")
        formatted = format_welcome_text(text, member_user.first_name, chat.title, member_user.id)
        markup = await string_to_buttons(buttons, include_mandatory=True)

        sent, _ = await _send_dm_safe(client, member_user.id, formatted, markup)
        if sent:
            await client.db.inc_stat("byes_sent")
            log.info(f"Bye message sent to {member_user.id} (left {chat.id})")
    except Exception as e:
        log.error(f"bye_handler error: {e}", exc_info=True)
