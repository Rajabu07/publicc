"""
Follow-up DM: sent N days (default 3) after a user was approved + welcomed.
The text/buttons are set by the bot owner from the Owner Dashboard.
"""
import asyncio
import logging

from bot.config import Config
from helpers.str_to_buttons import string_to_buttons
from helpers.utils import format_welcome_text

log = logging.getLogger(__name__)

DEFAULT_FOLLOWUP_TEXT = (
    "👋 Hey {name}!\n\n"
    "🔥 Just checking in — **today's best deals** are live in **{chat}**!\n\n"
    "💰 Don't miss out, tap below and save more 👇"
)
DEFAULT_BYE_TEXT = (
    "👋 Bye {name}!\n\n"
    "😢 Sorry to see you leave **{chat}**.\n"
    "🎁 Come back anytime — new deals are posted every day!"
)
MAX_ATTEMPTS = 3


def followup_delay_seconds():
    return max(60.0, float(Config.followup_days) * 86400.0)


async def is_followup_enabled(db):
    return bool(await db.get_setting("followup_enabled", Config.followup_default_enabled))


async def get_followup_message(db):
    text = await db.get_setting("followup_text") or DEFAULT_FOLLOWUP_TEXT
    buttons = await db.get_setting("followup_buttons")
    return text, buttons


async def schedule_followup(client, user_id, user_name, chat_title):
    """Queue a follow-up for this user (no-op when the feature is switched off)."""
    if not await is_followup_enabled(client.db):
        return False
    return await client.db.add_followup(user_id, user_name, chat_title, followup_delay_seconds())


async def _process_followup(client, item):
    from helpers.approver_helper import _send_dm_safe

    user_id = int(item["user_id"])
    text, buttons = await get_followup_message(client.db)
    formatted = format_welcome_text(text, item.get("name"), item.get("chat_title"), user_id)
    markup = await string_to_buttons(buttons, include_mandatory=True)

    sent, can_dm = await _send_dm_safe(client, user_id, formatted, markup)
    if sent:
        await client.db.inc_stat("followups_sent")
        await client.db.delete_followup(user_id)
        log.info(f"Follow-up delivered to {user_id}")
    elif not can_dm or int(item.get("attempts", 0)) + 1 >= MAX_ATTEMPTS:
        # Blocked / deactivated, or too many failures -> give up
        await client.db.delete_followup(user_id)
        log.info(f"Follow-up dropped for {user_id} (can_dm={can_dm})")
    else:
        await client.db.retry_followup_later(user_id)


async def followup_worker(client):
    """Background loop. Safe across restarts because the queue lives in MongoDB."""
    await asyncio.sleep(30)
    log.info("Follow-up worker started (delay: %s days)", Config.followup_days)

    while True:
        sleep_for = 60
        try:
            if await is_followup_enabled(client.db):
                due = await client.db.get_due_followups(50)
                for item in due:
                    try:
                        await _process_followup(client, item)
                    except Exception as e:
                        log.error(f"Follow-up failed for {item.get('user_id')}: {e}")
                        try:
                            await client.db.retry_followup_later(item["user_id"])
                        except Exception:
                            pass
                    await asyncio.sleep(0.1)  # ~10 msgs/sec max
                if len(due) >= 50:
                    sleep_for = 5  # more are waiting
            else:
                sleep_for = 300
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"Follow-up worker error: {e}", exc_info=True)
        await asyncio.sleep(sleep_for)
