"""GUI-managed, per-server GIF slash commands."""

import random
import re

import discord
from discord import app_commands


class CustomSlashCommands:
    """Register and synchronize custom member-reaction commands."""

    def __init__(self, log):
        self.log = log
        self._gif_cycles = {}

    @staticmethod
    def valid_name(name):
        return bool(re.fullmatch(r"[a-z0-9_-]{1,32}", str(name).strip().lower()))

    def _next_gif(self, guild_id, command_name, urls):
        key = (str(guild_id), str(command_name))
        source = tuple(urls)
        state = self._gif_cycles.get(key)
        if not state or state["source"] != source or not state["remaining"]:
            remaining = list(source)
            random.shuffle(remaining)
            state = {"source": source, "remaining": remaining}
            self._gif_cycles[key] = state
        return state["remaining"].pop()

    def register(self, bot, settings):
        """Add all saved server-specific commands to Discord's command tree."""
        for guild_id, commands_config in settings.get("custom_commands", {}).items():
            guild = discord.Object(id=int(guild_id))
            for raw_name, config in commands_config.items():
                name = str(raw_name).strip().lower()
                urls = [
                    str(url).strip()
                    for url in config.get("gifs", [])
                    if str(url).strip().startswith(("http://", "https://"))
                ]
                if not self.valid_name(name) or not urls:
                    continue

                def make_callback(bound_config, bound_name):
                    async def callback(interaction: discord.Interaction, target: discord.Member):
                        if target.bot:
                            return await interaction.response.send_message(
                                "Please choose a real server member.", ephemeral=True
                            )
                        selected = [
                            str(url).strip()
                            for url in bound_config.get("gifs", [])
                            if str(url).strip().startswith(("http://", "https://"))
                        ]
                        if not selected:
                            return await interaction.response.send_message(
                                "This command has no GIF links configured.", ephemeral=True
                            )
                        embed = discord.Embed(
                            description=f"{interaction.user.mention} **{bound_name}** {target.mention}",
                            colour=discord.Colour.blurple(),
                        )
                        embed.set_image(
                            url=self._next_gif(interaction.guild.id, bound_name, selected)
                        )
                        await interaction.response.send_message(
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(
                                users=True, roles=False, everyone=False
                            ),
                        )

                    return callback

                command = app_commands.Command(
                    name=name,
                    description=str(config.get("description", f"{name} reaction"))[:100],
                    callback=make_callback(config, name),
                )
                bot.tree.add_command(command, guild=guild, override=True)

    async def sync(self, bot, previous_settings, current_settings):
        """Replace changed GUI commands and publish them to the active servers."""
        previous = previous_settings.get("custom_commands", {})
        current = current_settings.get("custom_commands", {})
        affected_guilds = set(previous) | set(current)

        for guild_id, commands_config in previous.items():
            guild = discord.Object(id=int(guild_id))
            for name in commands_config:
                bot.tree.remove_command(str(name), guild=guild)

        self.register(bot, current_settings)

        active_guild_ids = {str(guild.id) for guild in bot.guilds}
        for guild_id in sorted(affected_guilds & active_guild_ids):
            await bot.tree.sync(guild=discord.Object(id=int(guild_id)))

        self.log("Custom GIF slash commands synchronized with Discord.")
