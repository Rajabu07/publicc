import logging
import datetime
import shutil
import psutil
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import MessageNotModified
from bot.config import Config
from helpers.humanbytes import humanbytes

log = logging.getLogger(__name__)

RANGES = {
    "today": ("Today", 1),
    "7d": ("7 Days", 7),
    "30d": ("30 Days", 30),
    "90d": ("90 Days", 90),
}
DEFAULT_RANGE = "7d"
BLOCKS = "▁▂▃▄▅▆▇█"


# ==========================================
# HELPERS
# ==========================================
def _since_utc(range_key: str):
    """Start of the selected window (UTC), aligned to the configured local midnight."""
    days = RANGES[range_key][1]
    off = datetime.timedelta(minutes=Config.stats_utc_offset_min)
    now_local = datetime.datetime.utcnow() + off
    start_local = (now_local - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local - off


def _tz_label() -> str:
    m = Config.stats_utc_offset_min
    sign = "+" if m >= 0 else "-"
    hh, mm = divmod(abs(m), 60)
    return "UTC" if m == 0 else f"UTC{sign}{hh:02d}:{mm:02d}"


def _pct(part: int, whole: int) -> str:
    if not whole:
        return "—"
    return f"{min(100, round(part * 100 / whole))}%"


def _daily_chart(rows) -> str:
    """rows: [(YYYY-MM-DD, count)] -> compact text bar chart."""
    if not rows:
        return "_No data yet_"
    peak = max((n for _, n in rows), default=0)
    lines = []
    for date_str, n in rows:
        try:
            label = datetime.datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b")
        except Exception:
            label = date_str
        if peak == 0:
            bar = "·"
        else:
            width = max(1, round(n * 10 / peak)) if n else 0
            bar = "█" * width if width else "·"
        lines.append(f"`{label}` {bar} **{n}**")
    return "\n".join(lines)


def _range_buttons(prefix: str, current: str):
    row = []
    for key, (label, _) in RANGES.items():
        text = f"• {label}" if key == current else label
        row.append(InlineKeyboardButton(text, callback_data=f"{prefix}_{key}"))
    return row


async def _channel_title(client, chat_id: int) -> str:
    try:
        ch = await client.db.get_channel(chat_id)
        if ch and ch.get("title"):
            return ch["title"]
    except Exception:
        pass
    return f"Channel {chat_id}"


async def _safe_edit(query, text, markup):
    try:
        await query.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except MessageNotModified:
        await query.answer("✅ Already up to date")


# ==========================================
# OWNER DASHBOARD
# ==========================================
async def build_owner_dashboard(client, range_key: str):
    label, days = RANGES[range_key]
    since = _since_utc(range_key)

    summary = await client.db.stats_summary(since=since)
    requests = summary.get("request", 0)
    approved = summary.get("approved", 0)
    welcome_ok = summary.get("welcome_ok", 0)
    welcome_fail = summary.get("welcome_fail", 0)

    pending_now = await client.db.total_pending_all()
    total_users = await client.db.total_users_count()
    all_channels = await (await client.db.get_all_channels()).to_list(length=1000)
    channel_count = len([c for c in all_channels if int(c.get("channel_id") or 0) != 0])

    daily_rows = await client.db.stats_daily("approved", days=7, tz_offset_min=Config.stats_utc_offset_min)
    top = await client.db.stats_top_channels(since=since, limit=5)

    top_lines = []
    for idx, (cid, n) in enumerate(top, start=1):
        title = await _channel_title(client, cid)
        top_lines.append(f"{idx}. {title[:28]} — **{n}**")
    top_text = "\n".join(top_lines) if top_lines else "_No approvals in this period_"

    try:
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        total, used, _free = (humanbytes(x) for x in shutil.disk_usage("."))
        disk = psutil.disk_usage(".").percent
        health = f"🖥 CPU `{cpu}%` • RAM `{ram}%` • Disk `{disk}%` ({used}/{total})"
    except Exception:
        health = "🖥 Server health unavailable"

    try:
        from bot.plugins.owner_session_manager import check_session_validity
        session_status = await check_session_validity(client)
    except Exception:
        session_status = "Unknown"

    text = (
        f"📊 **Stats Dashboard — {label}**\n"
        f"🕐 _Timezone: {_tz_label()}_\n\n"
        f"📥 **Join requests:** `{requests}`\n"
        f"✅ **Approved:** `{approved}` ({_pct(approved, requests)})\n"
        f"⏳ **Pending now:** `{pending_now}`\n"
        f"💌 **Welcome DM:** `{welcome_ok}` delivered • `{welcome_fail}` failed/blocked\n\n"
        f"👥 **Bot users:** `{total_users}`\n"
        f"📢 **Connected chats:** `{channel_count}`\n"
        f"🔐 **Userbot:** {session_status}\n\n"
        f"📈 **Approvals — last 7 days**\n{_daily_chart(daily_rows)}\n\n"
        f"🏆 **Top channels ({label})**\n{top_text}\n\n"
        f"{health}\n\n"
        f"_Tracking starts from the day this update was deployed._"
    )

    buttons = [
        _range_buttons("owner_stats", range_key),
        [
            InlineKeyboardButton("📢 Channel-wise", callback_data=f"stats_channels_{range_key}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"owner_stats_{range_key}")
        ],
        [InlineKeyboardButton("🔙 Back to Owner Panel", callback_data="owner_panel")]
    ]
    return text, InlineKeyboardMarkup(buttons)


@Client.on_callback_query(filters.regex(r"^owner_stats(?:_(today|7d|30d|90d))?$"))
async def callback_owner_stats(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return
    range_key = query.matches[0].group(1) or DEFAULT_RANGE
    try:
        text, markup = await build_owner_dashboard(client, range_key)
        await _safe_edit(query, text, markup)
    except Exception as e:
        log.exception(e)
        await query.answer("⚠️ Could not load stats. Check logs.", show_alert=True)


@Client.on_message(filters.command("stats") & filters.private)
async def cmd_stats(client, message):
    if message.from_user.id not in Config.owner:
        return
    try:
        text, markup = await build_owner_dashboard(client, DEFAULT_RANGE)
        await message.reply(text, reply_markup=markup, quote=True, disable_web_page_preview=True)
    except Exception as e:
        log.exception(e)
        await message.reply(f"⚠️ Could not load stats: `{e}`")


# ==========================================
# CHANNEL-WISE LIST (owner)
# ==========================================
@Client.on_callback_query(filters.regex(r"^stats_channels_(today|7d|30d|90d)$"))
async def callback_stats_channels(client, query):
    if query.from_user.id not in Config.owner:
        await query.answer("⛔ Access denied: Owner only.", show_alert=True)
        return
    range_key = query.matches[0].group(1)
    label = RANGES[range_key][0]
    since = _since_utc(range_key)

    approved_rows = await client.db.stats_top_channels(since=since, limit=15, event_type="approved")
    request_rows = dict(await client.db.stats_top_channels(since=since, limit=200, event_type="request"))

    lines = []
    buttons = []
    for cid, n in approved_rows:
        title = await _channel_title(client, cid)
        lines.append(f"• **{title[:30]}**\n   ✅ `{n}` approved • 📥 `{request_rows.get(cid, 0)}` requests")
        buttons.append([InlineKeyboardButton(f"📈 {title[:34]}", callback_data=f"ch_stats_{cid}_{range_key}")])

    text = (
        f"📢 **Channel-wise Stats — {label}**\n\n"
        + ("\n".join(lines) if lines else "_No approvals in this period_")
        + "\n\nTap a channel for its full dashboard 👇"
    )
    buttons.append(_range_buttons("stats_channels", range_key))
    buttons.append([InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"owner_stats_{range_key}")])
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))


# ==========================================
# PER-CHANNEL STATS (owner or the channel's admin)
# ==========================================
@Client.on_callback_query(filters.regex(r"^ch_stats_(-?\d+)(?:_(today|7d|30d|90d))?$"))
async def callback_channel_stats(client, query):
    chat_id = int(query.matches[0].group(1))
    range_key = query.matches[0].group(2) or DEFAULT_RANGE
    user_id = query.from_user.id

    channel = await client.db.get_channel(chat_id)
    is_owner = user_id in Config.owner
    is_admin = bool(channel) and str(channel.get("admin_id")) == str(user_id)
    if not (is_owner or is_admin):
        await query.answer("⛔ Access denied.", show_alert=True)
        return

    label, days = RANGES[range_key]
    since = _since_utc(range_key)
    title = (channel or {}).get("title") or f"Channel {chat_id}"
    mode = (channel or {}).get("mode", "auto")
    mode_display = "🟢 Auto-Approve" if mode == "auto" else "🟡 Manual Approval"

    summary = await client.db.stats_summary(since=since, chat_id=chat_id)
    requests = summary.get("request", 0)
    approved = summary.get("approved", 0)
    welcome_ok = summary.get("welcome_ok", 0)
    welcome_fail = summary.get("welcome_fail", 0)
    pending_now = await client.db.total_pending_count(chat_id)
    daily_rows = await client.db.stats_daily(
        "approved", days=7, tz_offset_min=Config.stats_utc_offset_min, chat_id=chat_id
    )

    text = (
        f"📈 **{title}** — {label}\n"
        f"⚙️ Mode: {mode_display}\n"
        f"🕐 _Timezone: {_tz_label()}_\n\n"
        f"📥 **Join requests:** `{requests}`\n"
        f"✅ **Approved:** `{approved}` ({_pct(approved, requests)})\n"
        f"⏳ **Pending now:** `{pending_now}`\n"
        f"💌 **Welcome DM:** `{welcome_ok}` delivered • `{welcome_fail}` failed/blocked\n\n"
        f"📈 **Approvals — last 7 days**\n{_daily_chart(daily_rows)}"
    )

    buttons = [
        _range_buttons(f"ch_stats_{chat_id}", range_key),
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"ch_stats_{chat_id}_{range_key}")],
        [InlineKeyboardButton("🔙 Back to Channel", callback_data=f"manage_ch_{chat_id}")]
    ]
    if is_owner:
        buttons.append([InlineKeyboardButton("📊 Owner Dashboard Stats", callback_data=f"owner_stats_{range_key}")])
    await _safe_edit(query, text, InlineKeyboardMarkup(buttons))
