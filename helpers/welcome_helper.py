import html
import logging

log = logging.getLogger(__name__)

MEDIA_TYPES = ("photo", "video", "animation", "document")


def extract_welcome(msg):
    """
    Reads a message the owner sent while configuring a welcome and returns
    dict(text, media_type, media_file_id, parse_mode).
    Text/caption is stored as HTML so bold/italic/links/spoilers (and custom emoji tags) survive.
    """
    media_type, file_id = None, None
    if msg.photo:
        media_type, file_id = "photo", msg.photo.file_id
    elif msg.video:
        media_type, file_id = "video", msg.video.file_id
    elif msg.animation:
        media_type, file_id = "animation", msg.animation.file_id
    elif msg.document:
        media_type, file_id = "document", msg.document.file_id

    raw = msg.caption if media_type else msg.text
    text = None
    if raw:
        try:
            text = raw.html
        except Exception:
            text = str(raw)
        text = (text or "").strip() or None

    return {
        "text": text,
        "media_type": media_type,
        "media_file_id": file_id,
        "parse_mode": "html",
    }


def has_content(wc: dict) -> bool:
    return bool(wc and ((wc.get("text") or "").strip() or wc.get("media_file_id")))


def describe_welcome(wc: dict) -> str:
    """Short plain-text description for owner-facing menus."""
    kind = (wc.get("media_type") or "text").upper()
    text = (wc.get("text") or "").strip()
    if wc.get("parse_mode") == "html":
        # keep menus readable: show a stripped, escaped snippet
        import re
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    snippet = text[:300] + ("…" if len(text) > 300 else "")
    return f"Type: `{kind}`\n\n{snippet}" if snippet else f"Type: `{kind}` (no caption)"
