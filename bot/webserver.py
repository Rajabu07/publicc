"""
Tiny HTTP server for Railway.

  GET  /, /health          -> health check (200 OK + uptime)
  POST /webhook/<secret>   -> optional Telegram Bot API webhook for `chat_join_request`
                              (enable with WEBHOOK_SECRET; register with WEBHOOK_URL)

NOTE: The bot itself talks to Telegram over MTProto (Pyrogram) and needs NO webhook to work.
The webhook endpoint is an extra, redundant path for join requests - duplicates are ignored.
"""
import hmac
import time
import logging
from aiohttp import web, ClientSession, ClientTimeout

from bot.config import Config
from helpers.utils import extract_join_request, format_uptime, START_TIME, spawn
from helpers.join_helper import handle_join_request

log = logging.getLogger(__name__)


def web_server_enabled():
    mode = Config.web_server
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return bool(__import__("os").getenv("PORT"))  # auto: Railway always sets PORT


def build_app(client):
    app = web.Application()

    async def health(request):
        return web.json_response({
            "status": "ok",
            "bot": getattr(getattr(client, "me", None), "username", None),
            "uptime": format_uptime(time.time() - START_TIME),
        })

    async def webhook(request):
        secret = Config.webhook_secret
        if not secret or not hmac.compare_digest(request.match_info.get("secret", ""), secret):
            return web.Response(status=403, text="forbidden")
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header is not None and not hmac.compare_digest(header, secret):
            return web.Response(status=403, text="forbidden")
        try:
            update = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        jr = extract_join_request(update)
        if jr:
            spawn(
                handle_join_request(client, jr["chat_id"], jr["user_id"], jr["name"], jr["title"]),
                name="webhook-join-request",
            )
        return web.Response(text="ok")  # always answer fast so Telegram does not retry

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook/{secret}", webhook)
    return app


async def _register_webhook():
    if not (Config.webhook_url and Config.webhook_secret and Config.bot_token):
        return
    url = f"{Config.webhook_url}/webhook/{Config.webhook_secret}"
    try:
        async with ClientSession(timeout=ClientTimeout(total=15)) as session:
            async with session.post(
                f"https://api.telegram.org/bot{Config.bot_token}/setWebhook",
                json={"url": url, "secret_token": Config.webhook_secret,
                      "allowed_updates": ["chat_join_request"], "drop_pending_updates": False},
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    log.info("Webhook registered for chat_join_request")
                else:
                    log.warning(f"setWebhook failed: {data.get('description')}")
    except Exception as e:
        log.warning(f"Could not register webhook: {e}")


async def start_web_server(client):
    """Returns the AppRunner (call .cleanup() on shutdown) or None when disabled."""
    if not web_server_enabled():
        log.info("Web server disabled (set WEB_SERVER=true or deploy on Railway to enable)")
        return None
    runner = web.AppRunner(build_app(client), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", Config.port)
    await site.start()
    log.info(f"Web server listening on 0.0.0.0:{Config.port} (/health)")
    await _register_webhook()
    return runner
