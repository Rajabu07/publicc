import logging
from pyrogram import Client
from helpers.join_helper import handle_join_request

log = logging.getLogger(__name__)


@Client.on_chat_join_request()
async def approver(client, joinRequest):
    if not getattr(joinRequest, "from_user", None) or not getattr(joinRequest, "chat", None):
        return

    await handle_join_request(
        client,
        chat_id=joinRequest.chat.id,
        user_id=joinRequest.from_user.id,
        user_name=joinRequest.from_user.first_name or "",
        chat_title=joinRequest.chat.title or "",
    )
