"""Birthdays feature module."""

import asyncio
from datetime import datetime

import discord

from features.formatters import format_birthday


class BirthdayDashboardView(discord.ui.View):
    """Persistent 'Add Birthday' dashboard button posted in the configured birthday channel."""
    def __init__(self, moderator):
        super().__init__(timeout=None); self.moderator = moderator
        button = discord.ui.Button(label="Add Birthday", style=discord.ButtonStyle.primary, emoji="\U0001f382", custom_id="birthday_dashboard:add")
        button.callback = self.open_birthday_flow; self.add_item(button)

    async def open_birthday_flow(self, interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in the configured server.", ephemeral=True)
        configured_channel = self.moderator.settings.get("birthdays", {}).get(str(interaction.guild.id), {}).get("channel_id")
        if not configured_channel or str(interaction.channel_id) != str(configured_channel):
            return await interaction.response.send_message("Setting a birthday is not available in this channel anymore.", ephemeral=True)
        try:
            existing = await asyncio.to_thread(self.moderator.store.get_birthday, interaction.guild.id, interaction.user.id)
        except Exception as exc:
            self.moderator.log(f"Could not check saved birthday for {interaction.user}: {exc}")
            return await interaction.response.send_message("Could not check your saved birthday. Please try again in a moment.", ephemeral=True)
        if existing:
            return await interaction.response.send_message(
                f"You already have a birthday saved: **{format_birthday(existing)}**. Do you want to change it?",
                view=BirthdayChangeConfirmView(self.moderator, interaction.guild.id, interaction.user.id),
                ephemeral=True,
            )
        await interaction.response.send_modal(BirthdayModal(self.moderator, interaction.guild.id))


class BirthdayChangeConfirmView(discord.ui.View):
    def __init__(self, moderator, guild_id, user_id):
        super().__init__(timeout=120); self.moderator, self.guild_id, self.user_id = moderator, guild_id, user_id
        change = discord.ui.Button(label="Yes, Change Date", style=discord.ButtonStyle.danger)
        keep = discord.ui.Button(label="No, Keep It", style=discord.ButtonStyle.secondary)
        change.callback = self.change_date; keep.callback = self.keep_date
        self.add_item(change); self.add_item(keep)

    async def change_date(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Only the member who opened this form can change this birthday.", ephemeral=True)
        await interaction.response.send_modal(BirthdayModal(self.moderator, self.guild_id))

    async def keep_date(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Only the member who opened this form can use these buttons.", ephemeral=True)
        await interaction.response.edit_message(content="Your saved birthday was not changed.", view=None)


class BirthdayModal(discord.ui.Modal, title="Set Your Birthday"):
    def __init__(self, moderator, guild_id):
        super().__init__(); self.moderator, self.guild_id = moderator, guild_id
        self.day = discord.ui.TextInput(label="Day (1-31)", placeholder="e.g. 25", max_length=2)
        self.month = discord.ui.TextInput(label="Month (1-12)", placeholder="e.g. 12", max_length=2)
        self.year = discord.ui.TextInput(label="Year (optional)", placeholder="e.g. 2000", max_length=4, required=False)
        self.add_item(self.day); self.add_item(self.month); self.add_item(self.year)

    async def on_submit(self, interaction):
        try:
            day, month = int(self.day.value.strip()), int(self.month.value.strip())
            year_text = self.year.value.strip()
            year = int(year_text) if year_text else None
            if month == 2 and day > 28:
                raise ValueError("February has at most 28 days")
            datetime(year or 2000, month, day)
        except (ValueError, TypeError):
            return await interaction.response.send_message("Enter a valid day and month, and a valid year if provided.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(self.moderator.store.save_birthday, self.guild_id, interaction.user.id, str(interaction.user), day, month, year)
        except Exception as exc:
            self.moderator.log(f"Could not save birthday: {exc}")
            return await interaction.followup.send("Could not save your birthday. Please try again in a moment.", ephemeral=True)
        shown = format_birthday({"day": day, "month": month, "year": year})
        self.moderator.log(f"{interaction.user} set their birthday to {shown}.")
        await interaction.followup.send(f"\U0001f382 Your birthday has been set to **{shown}**. Only you can see this confirmation.", ephemeral=True)
