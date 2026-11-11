"""
Safe broadcast engine.

- Owner sees a preview and must confirm before anything is sent.
- Messages go out in small parallel batches (~20/sec, under Telegram's limits).
- Live progress message with a Stop button.
- FloodWait handled, dead users (blocked/deactivated) are cleaned from the DB.
- Every step is wrapped so one bad user can never crash the whole broadcast.
"""
import io
import time
import uuid
import asyncio
import datetime
import logging
from collections import OrderedDict

from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import Config
from helpers.utils import progress_bar, format_uptime, spawn

log = logging.getLogger(__name__)

try:
    from pyrogram.types import LinkPreviewOptions
    PREVIEW_OPTIONS = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except (ImportError, AttributeError):
    PREVIEW_OPTIONS = {"disable_web_page_preview": True}

_DRAFTS = OrderedDict()   # token -> Message (waiting for owner confirmation)
_ACTIVE = {}              # broadcast_id -> state dict
_LOCK = None
MAX_DRAFTS = 20
PROGRESS_EVERY = 4        # seconds between progress edits


def _get_lock():
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


# ----------------------------------------------------------------------
# Drafts
# ----------------------------------------------------------------------
def make_draft(message):
    token = uuid.uuid4().hex[:8]
    _DRAFTS[token] = message
    while len(_DRAFTS) > MAX_DRAFTS:
        _DRAFTS.popitem(last=False)
    return token


def pop_draft(token):
    return _DRAFTS.pop(token, None)


def request_cancel(broadcast_id):
    state = _ACTIVE.get(broadcast_id)
    if state:
        state["cancel"] = True
        return True
    return False


def is_running():
    return _get_lock().locked()


# ----------------------------------------------------------------------
# Delivery
# ----------------------------------------------------------------------
async def _deliver_message_no_preview(message, chat_id):
    """Copies/sends a message with link preview disabled (text) and buttons kept."""
    client = getattr(message, "_client", None)
    if message.text and client:
        try:
            return await client.send_message(
                chat_id=chat_id, text=message.text, entities=message.entities,
                reply_markup=message.reply_markup, **PREVIEW_OPTIONS
            )
        except (TypeError, AttributeError):
            return await client.send_message(
                chat_id=chat_id, text=message.text, entities=message.entities,
                reply_markup=message.reply_markup, disable_web_page_preview=True
            )
    try:
        return await message.copy(chat_id=chat_id, **PREVIEW_OPTIONS)
    except (TypeError, AttributeError):
        return await message.copy(chat_id=chat_id)


async def send_msg(user_id, message):
    """
    Returns (code, info):
      (200, sent_message)
      (400, 'blocked' | 'deactivated' | 'invalid')   -> user is unreachable, remove from DB
      (500, 'error text')                             -> temporary / unknown failure
    """
    for _ in range(3):
        try:
            return 200, await _deliver_message_no_preview(message, user_id)
        except FloodWait as e:
            wait = min(e.value + 1, 120)
            log.warning(f"FloodWait {wait}s while broadcasting to {user_id}")
            await asyncio.sleep(wait)
        except UserIsBlocked:
            return 400, "blocked"
        except InputUserDeactivated:
            return 400, "deactivated"
        except PeerIdInvalid:
            return 400, "invalid"
        except Exception as e:
            return 500, f"{e.__class__.__name__}: {str(e)[:200]}"
    return 500, "FloodWait retries exhausted"


# ----------------------------------------------------------------------
# Preview + confirmation
# ----------------------------------------------------------------------
async def send_broadcast_preview(client, chat_id, message):
    """Shows the owner exactly what will be sent and asks for confirmation."""
    token = make_draft(message)
    total = await client.db.total_users_count()

    await client.send_message(
        chat_id,
        f"👀 **Broadcast Preview**\n\n👇 This is exactly what **{total}** users will receive:"
    )
    try:
        await _deliver_message_no_preview(message, chat_id)
    except Exception as e:
        log.warning(f"Broadcast preview failed: {e}")
        await client.send_message(chat_id, f"⚠️ Could not render preview: `{e}`")

    hours = Config.broadcast_auto_delete_hours
    auto_del = f"🗑 Auto-delete after **{hours:g}h**" if hours > 0 else "♾️ Messages stay (no auto-delete)"
    await client.send_message(
        chat_id,
        f"📣 **Ready to broadcast?**\n\n"
        f"👥 **Recipients:** `{total}`\n"
        f"{auto_del}\n"
        f"🔗 Link preview: Disabled\n\n"
        f"Tap **🚀 Start Broadcast** to begin.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Start Broadcast", callback_data=f"bc_go_{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"bc_cancel_{token}"),
        ]])
    )
    return token


# ----------------------------------------------------------------------
# Main engine
# ----------------------------------------------------------------------
def _status_text(state, final=False):
    done = state["success"] + state["failed"] + state["blocked"]
    head = "✅ **Broadcast Finished**" if final and not state["cancel"] else (
        "🛑 **Broadcast Stopped**" if final else "📣 **Broadcast in Progress...**")
    elapsed = time.time() - state["start"]
    speed = done / elapsed if elapsed > 0 else 0
    return (
        f"{head}\n\n"
        f"{progress_bar(done, state['total'])}\n"
        f"📊 **{done} / {state['total']}** users\n\n"
        f"✅ Delivered: `{state['success']}`\n"
        f"🚫 Blocked/Deactivated: `{state['blocked']}`\n"
        f"⚠️ Failed: `{state['failed']}`\n\n"
        f"⚡ Speed: `{speed:.1f}/s` • ⏱ `{format_uptime(elapsed)}`"
    )


async def _safe_edit(msg, text, markup=None):
    try:
        await msg.edit_text(text, reply_markup=markup)
    except FloodWait as e:
        await asyncio.sleep(min(e.value, 5))
    except Exception:
        pass  # MessageNotModified etc. - progress display only


async def run_broadcast(client, broadcast_msg, report_chat_id):
    """Runs a full broadcast. Never raises."""
    lock = _get_lock()
    if lock.locked():
        await client.send_message(report_chat_id, "⏳ Another broadcast is already running. Please wait for it to finish.")
        return None

    async with lock:
        broadcast_id = uuid.uuid4().hex[:6]
        state = {"total": 0, "success": 0, "failed": 0, "blocked": 0, "cancel": False, "start": time.time()}
        _ACTIVE[broadcast_id] = state
        sent_refs, error_lines = [], []
        status = None

        try:
            state["total"] = await client.db.total_users_count()
            stop_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop Broadcast", callback_data=f"bc_stop_{broadcast_id}")]])
            status = await client.send_message(report_chat_id, _status_text(state), reply_markup=stop_markup)
            log.info(f"Broadcast [{broadcast_id}] started. Recipients: {state['total']}")

            cursor = await client.db.get_all_users()
            batch, last_edit = [], 0.0

            async def flush(ids):
                nonlocal last_edit
                t0 = time.time()
                results = await asyncio.gather(*[send_msg(u, broadcast_msg) for u in ids], return_exceptions=True)
                for uid, res in zip(ids, results):
                    if isinstance(res, Exception):
                        state["failed"] += 1
                        error_lines.append(f"{uid} : {res!r}")
                        continue
                    code, info = res
                    if code == 200:
                        state["success"] += 1
                        try:
                            sent_refs.append((info.chat.id, info.id))
                        except Exception:
                            pass
                    elif code == 400:
                        state["blocked"] += 1
                        try:
                            await client.db.delete_user(uid)
                        except Exception:
                            pass
                    else:
                        state["failed"] += 1
                        error_lines.append(f"{uid} : {info}")
                # stay below ~20 msgs/second
                spent = time.time() - t0
                if spent < 1.0:
                    await asyncio.sleep(1.0 - spent)
                if time.time() - last_edit >= PROGRESS_EVERY:
                    last_edit = time.time()
                    await _safe_edit(status, _status_text(state), stop_markup)

            async for user in cursor:
                if state["cancel"]:
                    break
                uid = user.get("id")
                if uid is None:
                    continue
                batch.append(int(uid))
                if len(batch) >= Config.broadcast_batch:
                    await flush(batch)
                    batch = []
            if batch and not state["cancel"]:
                await flush(batch)

            try:
                await cursor.close()
            except Exception:
                pass

        except Exception as e:
            log.error(f"Broadcast [{broadcast_id}] crashed: {e}", exc_info=True)
            error_lines.append(f"CRASH : {e!r}")
            try:
                await client.send_message(report_chat_id, f"⚠️ Broadcast stopped because of an error:\n`{e}`")
            except Exception:
                pass
        finally:
            _ACTIVE.pop(broadcast_id, None)

        # ---- wrap up ----
        elapsed = int(time.time() - state["start"])
        log.info(
            f"Broadcast [{broadcast_id}] done in {elapsed}s. total={state['total']} "
            f"ok={state['success']} blocked={state['blocked']} failed={state['failed']}"
        )
        await client.db.inc_stat("broadcast_sent", state["success"])
        if status:
            await _safe_edit(status, _status_text(state, final=True))

        if error_lines:
            try:
                buf = io.BytesIO("\n".join(error_lines).encode())
                buf.name = f"broadcast_{broadcast_id}_errors.txt"
                await client.send_document(
                    report_chat_id, buf,
                    caption=f"📄 Error log • {len(error_lines)} failed (blocked/deactivated users are not listed)"
                )
            except Exception as e:
                log.warning(f"Could not send broadcast error log: {e}")

        hours = Config.broadcast_auto_delete_hours
        if sent_refs and hours > 0:
            spawn(_auto_delete(client, sent_refs, hours * 3600), name=f"bc-autodelete-{broadcast_id}")
        return state


async def _auto_delete(client, refs, delay):
    await asyncio.sleep(delay)
    for chat_id, msg_id in refs:
        for _ in range(2):
            try:
                await client.delete_messages(chat_id, msg_id)
                break
            except FloodWait as e:
                await asyncio.sleep(min(e.value + 1, 60))
            except Exception:
                break
        await asyncio.sleep(0.05)


# Backwards compatible entry point
async def broadcast_handler(c, m):
    msg = m.reply_to_message if m.reply_to_message else m
    return await run_broadcast(c, msg, m.chat.id)
