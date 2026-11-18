"""Per-server blocked-word filtering and strike handling."""

import asyncio
import re

import discord


class WordBlockFeature:
    """Handle blocked words using the shared store and bot runtime services."""

    STRIKE_LIMIT = 5

    def __init__(self, moderator):
        self.moderator = moderator

    def words_for_guild(self, guild_id):
        settings = self.moderator.settings
        return list(
            dict.fromkeys(
                str(word).strip().casefold()
                for word in settings.get("word_blocks", {}).get(str(guild_id), [])
                if str(word).strip()
            )
        )

    async def handle_message(self, message):
        """Delete a matching message and ban at the fifth recorded violation."""
        moderator = self.moderator
        if message.guild is None:
            return False

        content = message.content.casefold()
        matched = next(
            (
                word
                for word in self.words_for_guild(message.guild.id)
                if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", content)
            ),
            None,
        )
        if not matched:
            return False

        try:
            violation = await asyncio.to_thread(
                moderator.store.record_word_block_violation,
                message.guild.id,
                message.author.id,
                matched,
                message.id,
            )
            if violation.get("duplicate"):
                return True
            strikes = int(violation["count"])
        except Exception as exc:
            moderator.log(f"Could not record blocked-word violation in {message.guild.name}: {exc}")
            return False

        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            # Keep the configured rule active even when Discord disallows deletion.
            pass

        if strikes < self.STRIKE_LIMIT:
            moderator.log(
                f"Blocked word '{matched}' from {message.author} in "
                f"{message.guild.name} ({strikes}/{self.STRIKE_LIMIT})."
            )
            return True

        if message.author.id in moderator.protected_user_ids:
            moderator.log(
                f"Blocked-word threshold reached by protected user {message.author}; ban skipped."
            )
            return True

        try:
            await message.author.ban(
                reason=f"Permanent ban: {self.STRIKE_LIMIT} blocked-word violations (latest: {matched})",
                delete_message_seconds=0,
            )
            moderator.log(
                f"Permanently banned {message.author} in {message.guild.name} "
                f"after {strikes} blocked-word violations."
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            moderator.log(
                f"Could not ban blocked-word spammer {message.author} "
                f"in {message.guild.name}: {exc}"
            )
        return True
