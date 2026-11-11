import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

def _env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


class Config:
    api_hash = os.getenv("API_HASH", "0540168c2c5ef173522ac027349e87f6")
    api_id = int(os.getenv("API_ID", 37561073))
    bot_token = os.getenv("BOT_TOKEN", "")
    database_url = os.getenv("DATABASE_URL", "mongodb://localhost:27017")
    force_sub_env = os.getenv("FORCE_SUB", None)
    force_sub = int(force_sub_env) if force_sub_env and (force_sub_env.isdigit() or (force_sub_env.startswith('-') and force_sub_env[1:].isdigit())) else force_sub_env
    log_channel = os.getenv("LOG_CHANNEL", None)
    
    # Safe parsing for OWNER_ID (defaults to configured owner ID 7652736472 if not specified in env)
    owner_id_env = os.getenv("OWNER_ID", "7652736472").strip()
    owner = [int(user) for user in owner_id_env.replace(",", " ").split() if user.isdigit()] if owner_id_env else [7652736472]

    # Mandatory permanent button on all welcome messages (Owner sponsor/promotion)
    mandatory_btn_text = os.getenv("MANDATORY_BTN_TEXT", "💲💲 Join Now 💲💲")
    mandatory_btn_url = os.getenv("MANDATORY_BTN_URL", "https://t.me/+9b9PYr2PPZ9lODI1")

    # ---------------- Logging ----------------
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", "")  # optional rotating file, e.g. bot.log

    # ---------------- Web server / webhook (Railway) ----------------
    # WEB_SERVER: "auto" (default) = start only when Railway provides PORT
    web_server = os.getenv("WEB_SERVER", "auto").strip().lower()
    port = _env_int("PORT", 8080)
    webhook_url = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")   # https://your-app.up.railway.app
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()         # enables /webhook/<secret>

    # ---------------- Follow-up DM ----------------
    followup_days = _env_float("FOLLOWUP_DAYS", 3)                   # default: 3 days after approval
    followup_default_enabled = _env_bool("FOLLOWUP_ENABLED", True)

    # ---------------- Broadcast ----------------
    broadcast_batch = max(1, _env_int("BROADCAST_BATCH", 20))        # parallel sends per second
    broadcast_auto_delete_hours = _env_float("BROADCAST_AUTO_DELETE_HOURS", 8)  # 0 = never

    # Pagination settings for Owner Channel Manager
    channels_per_page = int(os.getenv("CHANNELS_PER_PAGE", 10))
    posts_per_page = int(os.getenv("POSTS_PER_PAGE", 10))

    # Optional Userbot Session for approving old/pre-existing backlog requests
    user_session = os.getenv("USER_SESSION", None)

    # Telegram Admin permissions for deep links (including Ban Users & Add Admins)
    # restrict_members = Ban Users
    # promote_members = Add New Admins
    admin_permissions = "change_info+post_messages+edit_messages+delete_messages+restrict_members+invite_users+pin_messages+promote_members+manage_chat"

    @classmethod
    def get_add_channel_url(cls, bot_username):
        return f"https://t.me/{bot_username}?startchannel=true&admin={cls.admin_permissions}"

    @classmethod
    def get_add_group_url(cls, bot_username):
        return f"https://t.me/{bot_username}?startgroup=true&admin={cls.admin_permissions}"
