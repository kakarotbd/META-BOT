"""Headless Railway entry point. Uses the same ModeratorBot as gui_bot.py."""
import asyncio
import os
from dotenv import load_dotenv
from firebase_store import EncryptedFirestore
from bot_core import ModeratorBot

load_dotenv()
store = EncryptedFirestore()

def log(message):
    print(message, flush=True)
    try: store.add_log("railway", message)
    except Exception as exc: print(f"Log upload failed: {exc}", flush=True)

async def run_forever():
    delay = 5
    while True:
        try:
            token = os.getenv("DISCORD_BOT_TOKEN", "").strip() or store.get_settings()["token"]
            if not token:
                raise RuntimeError("No Discord token in encrypted Firebase settings or DISCORD_BOT_TOKEN.")
            await ModeratorBot(store, "railway", log).run(token)
            log("Discord connection ended; reconnecting.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log(f"Railway bot error: {exc}. Reconnecting in {delay} seconds.")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)

asyncio.run(run_forever())
