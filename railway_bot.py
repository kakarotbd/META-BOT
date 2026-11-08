"""Headless Railway entry point. Uses the same ModeratorBot as gui_bot.py."""
import asyncio
import os
from dotenv import load_dotenv
from d1_store import CloudflareD1Store
from bot_core import ModeratorBot
from app_config import APP_CONFIG

load_dotenv()
if APP_CONFIG.logo_path.is_file():
    print(f"{APP_CONFIG.bot_app_name}: logo asset found at {APP_CONFIG.logo_path.relative_to(APP_CONFIG.logo_path.parent.parent)}", flush=True)
else:
    print(f"{APP_CONFIG.bot_app_name}: logo asset missing at {APP_CONFIG.logo_path}; Railway will run headlessly.", flush=True)
store = CloudflareD1Store()

def log(message):
    print(message, flush=True)
    try: store.add_log("railway", message)
    except Exception as exc: print(f"Log upload failed: {exc}", flush=True)

async def run_forever():
    delay = 5
    while True:
        try:
            # The shared encrypted token is authoritative so GUI and Railway
            # reconnect with the same bot after a token change. The env var is
            # retained only as a bootstrap fallback for older configurations.
            token = str(store.get_settings().get("token", "") or "").strip() or os.getenv("DISCORD_BOT_TOKEN", "").strip()
            if not token:
                raise RuntimeError("No Discord token in encrypted Cloudflare R2 settings or DISCORD_BOT_TOKEN.")
            delay = 5  # A successful Discord connection resets reconnect backoff.
            await ModeratorBot(store, "railway", log).run(token)
            log("Discord connection ended; reconnecting.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"Railway bot error: {exc}. Reconnecting in {delay} seconds.")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)

asyncio.run(run_forever())

