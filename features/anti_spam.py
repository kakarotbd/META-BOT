"""Detection and response for observable spam from newly joined bot accounts."""
from collections import defaultdict, deque
import time


class BotSpamGuard:
    """Track short message windows per guild and bot, then ban clear spam."""

    def __init__(self, *, window_seconds=10, burst_limit=7, duplicate_limit=3):
        self.window_seconds = window_seconds
        self.burst_limit = burst_limit
        self.duplicate_limit = duplicate_limit
        self.messages = defaultdict(deque)
        self.banned = set()

    async def inspect(self, message, notify_and_ban):
        author, guild = message.author, message.guild
        if guild is None or not getattr(author, "bot", False):
            return False
        key = (guild.id, author.id)
        if key in self.banned:
            return True
        now = time.monotonic()
        history = self.messages[key]
        while history and now - history[0][0] > self.window_seconds:
            history.popleft()
        content = (message.content or "").strip().casefold()
        history.append((now, content))

        reason = None
        if "@everyone" in (message.content or "").casefold() or "@here" in (message.content or "").casefold():
            reason = "Used an @everyone or @here mention."
        elif len(message.mentions) + len(message.role_mentions) >= 5:
            reason = f"Mentioned {len(message.mentions) + len(message.role_mentions)} users or roles in one message."
        elif len(history) >= self.burst_limit:
            reason = f"Sent {len(history)} messages within {self.window_seconds} seconds."
        elif content and sum(1 for _, previous in history if previous == content) >= self.duplicate_limit:
            reason = f"Repeated the same message {self.duplicate_limit} times within {self.window_seconds} seconds."

        if reason:
            self.banned.add(key)
            await notify_and_ban(message, reason)
            return True
        return False
