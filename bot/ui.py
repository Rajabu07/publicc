"""Shared UI pieces (home screen) so /start and the Back button always match."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot.config import Config


def home_text(user_name):
    return (
        f"👋 Hello **{user_name or 'Friend'}**! 🌟\n\n"
        f"🤖 I am your **Join Request Manager Bot** 🚀\n\n"
        f"✨ **Key Features:**\n"
        f"• ⚡ Instant Auto-Approval & Old Backlog Processing\n"
        f"• 💌 Custom Welcome DMs with 🖼 Photo / 🎬 Video / 🎞 GIF\n"
        f"• 🔘 100% Inline Buttons — Zero commands to type!\n\n"
        f"👇 Tap **➕ Add to Channel** or **➕ Add to Group** to get started:"
    )


def home_markup(bot_username, user_id):
    buttons = [
        [
            InlineKeyboardButton("➕ Add to Channel 📢", url=Config.get_add_channel_url(bot_username)),
            InlineKeyboardButton("➕ Add to Group 👥", url=Config.get_add_group_url(bot_username)),
        ],
        [
            InlineKeyboardButton("📢 My Channels", callback_data="user_channels"),
            InlineKeyboardButton("⚡ Batch Approve", callback_data="batch_panel"),
        ],
        [InlineKeyboardButton("❓ Help & Usage", callback_data="help_info")],
    ]
    if user_id in Config.owner:
        buttons.append([InlineKeyboardButton("👑 Owner Dashboard", callback_data="owner_panel")])
    else:
        buttons.append([InlineKeyboardButton("🤖 Developer / Updates", url="https://t.me/+9b9PYr2PPZ9lODI1")])
    return InlineKeyboardMarkup(buttons)
