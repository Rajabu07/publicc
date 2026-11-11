import asyncio
import logging

# FIX: Explicitly create and set the event loop for modern Python BEFORE importing pyrogram
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*link_preview_options.*")

from .logger import setup_logging, install_loop_exception_handler
setup_logging()
install_loop_exception_handler(loop)
log = logging.getLogger("bot")

from pyromod import listen
from .config import Config
from pyrogram import Client
from .database import Database
from helpers.utils import spawn


class Bot(Client, Config):
    def __init__(self):
        super().__init__(
            "Acceptor",
            api_id=self.api_id,
            api_hash=self.api_hash,
            bot_token=self.bot_token,
            plugins=dict(root="bot/plugins"),
            workers=100,
            sleep_threshold=10
        )
        self.db = Database(self.database_url, 'approve')
        self._web_runner = None

    async def start(self):
        await super().start()
        # Clean duplicates and create database indexes to prevent high MongoDB read costs
        try:
            await self.db.clean_duplicates()
        except Exception as e:
            log.error(f"Failed to create indexes: {e}")

        self.me = await self.get_me()
        log.info(f"Bot started as {self.me.first_name} (@{self.me.username}) - ready to accept requests")

        # Background workers (each one is isolated: a failure in one never stops the bot)
        self._start_worker("bot.plugins.owner_session_manager", "monitor_userbot_session", "userbot-monitor")
        self._start_worker("bot.plugins.owner_channel_post", "auto_delete_worker", "auto-delete")
        self._start_worker("helpers.followup_helper", "followup_worker", "followup")

        # Web server for Railway health checks + optional webhook
        try:
            from .webserver import start_web_server
            self._web_runner = await start_web_server(self)
        except Exception as e:
            log.warning(f"Web server could not start: {e}")

    def _start_worker(self, module_name, func_name, task_name):
        try:
            module = __import__(module_name, fromlist=[func_name])
            spawn(getattr(module, func_name)(self), name=task_name)
        except Exception as e:
            log.warning(f"Could not start worker {task_name}: {e}")

    async def stop(self, *args):
        if self._web_runner:
            try:
                await self._web_runner.cleanup()
            except Exception:
                pass
        await super().stop()
        log.info("Bot stopped. Bye.")


if __name__ == '__main__':
    Bot().run()
