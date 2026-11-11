"""
Pure helper functions (no Telegram / DB imports) so they are easy to unit-test
and can never fail on import in production.
"""
import re
import time
import asyncio
import logging
import datetime

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Timers
# ------------------------------------------------------------------
_PASS_WORDS = {"/pass", "pass", "no", "permanent", "0", "none", ""}


def parse_timer_seconds(text):
    """
    '30s' -> 30 | '10' -> 600 (minutes) | '10m' -> 600 | '1h' -> 3600
    '24h' / '1d' -> 86400 | '/pass' -> None.  Minimum 5 seconds.
    """
    if not text:
        return None
    s = text.strip().lower()
    if s in _PASS_WORDS:
        return None

    s = s.replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([smhd]?)$", s)
    if not m:
        m = re.match(
            r"^(\d+(?:\.\d+)?)(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)$", s
        )
        if not m:
            return None

    val = float(m.group(1))
    unit = m.group(2) or "m"
    if unit.startswith("s"):
        secs = val
    elif unit.startswith("m"):
        secs = val * 60
    elif unit.startswith("h"):
        secs = val * 3600
    elif unit.startswith("d"):
        secs = val * 86400
    else:
        secs = val * 60
    return max(5, int(secs))


def format_duration(seconds):
    if not seconds or seconds <= 0:
        return "♾️ Permanent (No auto-delete)"
    if seconds < 60:
        return f"⏳ {int(seconds)} second(s)"
    if seconds < 3600:
        return f"⏳ {int(seconds // 60)} minute(s)"
    if seconds < 86400:
        return f"⏳ {seconds / 3600:g} hour(s)"
    return f"⏳ {seconds / 86400:g} day(s)"


def format_uptime(seconds):
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    if not d:
        parts.append(f"{s}s")
    return " ".join(parts)


# ------------------------------------------------------------------
# Channel IDs  (/send -100111 -100222 -100333)
# ------------------------------------------------------------------
def normalize_channel_id(token):
    """
    '-1001234567890' -> -1001234567890
    '1234567890'     -> -1001234567890   (missing -100 prefix)
    '1001234567890'  -> -1001234567890   (missing minus)
    '-123456'        -> -123456          (basic group)
    anything else    -> None
    """
    if token is None:
        return None
    t = str(token).strip().strip(",;")
    if not re.fullmatch(r"-?\d+", t) or int(t) == 0:
        return None
    if t.startswith("-"):
        value = int(t)
    elif t.startswith("100") and len(t) >= 13:
        value = -int(t)
    else:
        value = -int(f"100{t}")
    return value if value != 0 else None


def parse_channel_ids(text):
    """
    Returns (ids, invalid_tokens, wants_all).
    Accepts spaces, commas, or new lines as separators. Duplicates removed
    (order kept). The word 'all' sets wants_all=True.
    """
    if isinstance(text, (list, tuple)):
        text = " ".join(str(x) for x in text)
    tokens = [t for t in re.split(r"[\s,;]+", text or "") if t]
    ids, invalid, wants_all = [], [], False
    for tok in tokens:
        if tok.lower() == "all":
            wants_all = True
            continue
        cid = normalize_channel_id(tok)
        if cid is None:
            invalid.append(tok)
        elif cid not in ids:
            ids.append(cid)
    return ids, invalid, wants_all


# ------------------------------------------------------------------
# Welcome text formatting
# ------------------------------------------------------------------
def format_welcome_text(raw_text, user_name, chat_title, user_id):
    """Safely formats welcome placeholders without crashing on empty values."""
    raw_text = raw_text or ""
    safe_name = str(user_name or "Friend").strip() or "Friend"
    clean_name = safe_name.replace("[", "").replace("]", "")
    safe_chat = str(chat_title or "our Channel").strip() or "our Channel"
    mention = f"[{clean_name}](tg://user?id={user_id})"
    return (
        raw_text.replace("{name}", safe_name)
        .replace("{first_name}", safe_name)
        .replace("{chat}", safe_chat)
        .replace("{title}", safe_chat)
        .replace("{chat_title}", safe_chat)
        .replace("{mention}", mention)
        .replace("{id}", str(user_id))
        .replace("{user_id}", str(user_id))
    )


# ------------------------------------------------------------------
# Media extraction (duck-typed so it can be tested without pyrogram)
# ------------------------------------------------------------------
def extract_media(message):
    """Returns (media_type, file_id) for photo/video/animation/document, else (None, None)."""
    for kind in ("photo", "video", "animation", "document"):
        obj = getattr(message, kind, None)
        if obj is not None and getattr(obj, "file_id", None):
            return kind, obj.file_id
    return None, None


MEDIA_LABELS = {
    "photo": "🖼 Photo",
    "video": "🎬 Video",
    "animation": "🎞 GIF",
    "document": "📎 Document",
}


# ------------------------------------------------------------------
# Dashboard helpers
# ------------------------------------------------------------------
def progress_bar(done, total, width=12):
    total = max(int(total or 0), 0)
    done = max(int(done or 0), 0)
    if total == 0:
        return "░" * width + " 0%"
    ratio = min(1.0, done / total)
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled) + f" {int(ratio * 100)}%"


_SPARKS = "▁▂▃▄▅▆▇█"


def sparkline(values):
    values = [max(0, int(v or 0)) for v in values]
    if not values:
        return ""
    hi = max(values)
    if hi == 0:
        return _SPARKS[0] * len(values)
    return "".join(_SPARKS[min(len(_SPARKS) - 1, int(v / hi * (len(_SPARKS) - 1)))] for v in values)


def fill_daily_stats(rows, days, today=None):
    """
    rows: list of dicts like {'date': 'YYYY-MM-DD', 'approved': 3, ...}
    Returns `days` dicts (oldest -> newest), missing days filled with {}.
    """
    today = today or datetime.datetime.utcnow().date()
    by_date = {r.get("date"): r for r in rows or []}
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        row = dict(by_date.get(d) or {})
        row["date"] = d
        out.append(row)
    return out


def safe_pct(part, whole):
    try:
        return f"{(part / whole) * 100:.0f}%" if whole else "—"
    except Exception:
        return "—"


def chunked(seq, size):
    size = max(1, int(size))
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ------------------------------------------------------------------
# Webhook helpers
# ------------------------------------------------------------------
def extract_join_request(update):
    """
    Extracts a join request from a Telegram Bot API Update dict.
    Returns dict(chat_id, user_id, name, title) or None.
    """
    if not isinstance(update, dict):
        return None
    jr = update.get("chat_join_request")
    if not isinstance(jr, dict):
        return None
    chat, user = jr.get("chat") or {}, jr.get("from") or {}
    if "id" not in chat or "id" not in user:
        return None
    try:
        return {
            "chat_id": int(chat["id"]),
            "user_id": int(user["id"]),
            "name": user.get("first_name") or "",
            "title": chat.get("title") or "",
        }
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------
# Background-task helper: never lose exceptions silently
# ------------------------------------------------------------------
_BG_TASKS = set()


def spawn(coro, name=None):
    """asyncio.create_task + keeps a reference + logs any crash."""
    task = asyncio.ensure_future(coro)
    _BG_TASKS.add(task)

    def _done(t):
        _BG_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("Background task %s crashed: %r", name or t, exc, exc_info=exc)

    task.add_done_callback(_done)
    return task


START_TIME = time.time()
