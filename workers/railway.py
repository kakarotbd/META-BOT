"""Railway entry point for the shared persistent Discord worker."""
import asyncio

from workers.runner import run_forever


asyncio.run(run_forever("railway"))
