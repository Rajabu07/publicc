"""Single place that handles a join request (used by the MTProto handler AND the webhook)."""
import time
import logging

from helpers.approver_helper import approve_and_welcome
from helpers.old_request_helper import invalidate_pending_cache

log = logging.getLogger(__name__)

_SEEN = {}          # (chat_id, user_id) -> timestamp; stops double handling if both paths fire
_SEEN_TTL = 120


def _already_seen(chat_id, user_id):
    now = time.time()
    if len(_SEEN) > 2000:
        for k in [k for k, t in _SEEN.items() if now - t > _SEEN_TTL]:
            _SEEN.pop(k, None)
    key = (int(chat_id), int(user_id))
    if key in _SEEN and now - _SEEN[key] < _SEEN_TTL:
        return True
    _SEEN[key] = now
    return False


async def handle_join_request(client, chat_id, user_id, user_name="", chat_title=""):
    if _already_seen(chat_id, user_id):
        log.debug(f"Duplicate join request ignored: {user_id} in {chat_id}")
        return False
    try:
        ch = await client.db.get_channel(chat_id)
        if not ch:
            await client.db.new_channel(chat_id, title=chat_title)

        mode = await client.db.get_channel_mode(chat_id)
        if mode == 'manual':
            await client.db.add_pending(chat_id, user_id, user_name)
            invalidate_pending_cache(chat_id)
            log.info(f"Queued join request for {user_name} ({user_id}) in {chat_title} [Manual Mode]")
            return True

        ok = await approve_and_welcome(
            client=client, chat_id=chat_id, user_id=user_id,
            user_name=user_name, chat_title=chat_title
        )
        invalidate_pending_cache(chat_id)
        return ok
    except Exception as e:
        log.error(f"Error handling join request {user_id} in {chat_id}: {e}", exc_info=True)
        return False
