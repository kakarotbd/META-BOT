"""Shared persistent Discord worker used by Railway."""
import asyncio
import os

from config.app_config import APP_CONFIG
from runtime.moderator import ModeratorBot
from storage.d1_store import CloudflareD1Store


async def run_forever(platform):
    store = CloudflareD1Store()
    if APP_CONFIG.logo_path.is_file():
        asset = APP_CONFIG.logo_path.relative_to(APP_CONFIG.logo_path.parent.parent)
        print(f"{APP_CONFIG.bot_app_name}: logo asset found at {asset}", flush=True)
    else:
        print(f"{APP_CONFIG.bot_app_name}: logo asset missing; {platform} will run headlessly.", flush=True)

    def log(message):
        print(message, flush=True)
        try:
            store.add_log(platform, message)
        except Exception as exc:
            print(f"Log upload failed: {exc}", flush=True)

    delay = 5
    while True:
        try:
            token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise RuntimeError(f"DISCORD_BOT_TOKEN is required in {platform} environment variables.")
            delay = 5
            await ModeratorBot(store, platform, log).run(token)
            log("Discord connection ended; reconnecting.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"{platform} bot error: {exc}. Reconnecting in {delay} seconds.")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)
