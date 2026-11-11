import os
import sys
import logging
from logging.handlers import RotatingFileHandler

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"


def setup_logging():
    """
    One place for logging config. Railway captures stdout, so we log to stdout.
    Optional: set LOG_FILE=bot.log for a rotating file (5 MB x 3).
    LOG_LEVEL=DEBUG/INFO/WARNING/ERROR controls verbosity.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        try:
            handlers.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"))
        except OSError as e:
            print(f"Could not open LOG_FILE {log_file}: {e}", file=sys.stderr)

    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)

    # Silence noisy libraries
    for name, lvl in (
        ("pyrogram", logging.ERROR),
        ("telethon", logging.WARNING),
        ("asyncio", logging.WARNING),
        ("pymongo", logging.WARNING),
        ("motor", logging.WARNING),
        ("aiohttp.access", logging.WARNING),
    ):
        logging.getLogger(name).setLevel(lvl)

    return logging.getLogger("bot")


def install_loop_exception_handler(loop):
    """Log unhandled exceptions from asyncio tasks instead of losing them."""
    log = logging.getLogger("bot.loop")

    def handler(_loop, context):
        exc = context.get("exception")
        msg = context.get("message", "Unhandled asyncio error")
        if exc:
            log.error("%s: %r", msg, exc, exc_info=exc)
        else:
            log.error(msg)

    loop.set_exception_handler(handler)
