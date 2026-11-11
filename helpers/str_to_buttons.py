from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot.config import Config


async def string_to_buttons(string, include_mandatory=True):
    buttons = []
    
    if string:
        rows = string.split('\n')
        for row in rows:
            row_buttons = []
            r_b = row.split("|")
            for b in r_b:
                b = b.strip()
                if not b or b.lower() in ['/pass', '/skip', 'none', 'skip', 'no', '/cancel', '/exit']:
                    continue
                data = b.split('-', 1)
                if len(data) == 2:
                    text = data[0].strip()
                    url = data[1].strip()
                    if not url.startswith(('http://', 'https://', 'tg://')):
                        if '.' in url or url.startswith('t.me/'):
                            url = f"https://{url}"
                    if url.startswith(('http://', 'https://', 'tg://')):
                        try:
                            row_buttons.append(InlineKeyboardButton(text, url=url))
                        except Exception:
                            pass
            if row_buttons:
                buttons.append(row_buttons)

    # Always ensure the mandatory promotional button is included
    if include_mandatory and Config.mandatory_btn_text and Config.mandatory_btn_url:
        try:
            mandatory_url = Config.mandatory_btn_url.strip()
            if mandatory_url.startswith(('t.me/', 'www.')):
                mandatory_url = f"https://{mandatory_url}"
            mandatory_btn = InlineKeyboardButton(Config.mandatory_btn_text.strip(), url=mandatory_url)
            # Check if button already exists to prevent duplicate
            already_present = any(
                getattr(btn, 'url', None) == mandatory_url for row in buttons for btn in row
            )
            if not already_present:
                buttons.append([mandatory_btn])
        except Exception:
            pass

    return InlineKeyboardMarkup(buttons) if buttons else None