import asyncio
import re
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

LINK_PATTERN = re.compile(r"(https?://\S+|www\.\S+|discord\.gg/\S+|\S+\.(com|net|org|io|xyz|gg)\S*)", re.I)
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, manage_messages=True, read_message_history=True, moderate_members=True)


class TicketModal(discord.ui.Modal, title="Create Support Token"):
    def __init__(self, moderator):
        super().__init__(); self.moderator = moderator
        self.category = discord.ui.TextInput(label="Category", placeholder="Technical, Billing, Account, or Other", max_length=30)
        self.other_category = discord.ui.TextInput(label="Other category (only if Other)", required=False, max_length=80)
        self.contact = discord.ui.TextInput(label="Contact information", placeholder="Email, Telegram, or another contact", max_length=150)
        self.problem = discord.ui.TextInput(label="Describe your problem", style=discord.TextStyle.paragraph, max_length=1000)
        for item in (self.category, self.other_category, self.contact, self.problem): self.add_item(item)

    async def on_submit(self, interaction):
        category = self.category.value.strip().title()
        allowed = {"Technical", "Billing", "Account", "Other"}
        if category not in allowed:
            return await interaction.response.send_message("Category must be Technical, Billing, Account, or Other.", ephemeral=True)
        if category == "Other" and not self.other_category.value.strip():
            return await interaction.response.send_message("Please fill in the Other category field.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            permitted = await asyncio.to_thread(self.moderator.store.claim_rate_limit, "ticket", interaction.user.id, 300)
            if not permitted:
                return await interaction.followup.send("You can create one support token every 5 minutes.", ephemeral=True)
            ticket = await asyncio.to_thread(self.moderator.store.create_ticket, interaction.user.id, str(interaction.user), category, self.other_category.value.strip(), self.contact.value.strip(), self.problem.value.strip())
        except Exception as exc:
            self.moderator.log(f"Ticket save failed: {exc}")
            return await interaction.followup.send("Ticket could not be saved. Please try again later.", ephemeral=True)
        confirmation = f"Support token **#{ticket['ticket_id']}** was created. Category: **{category}**. We will contact you using the information you supplied."
        try: await interaction.user.send(confirmation)
        except discord.Forbidden: pass
        self.moderator.log(f"Ticket #{ticket['ticket_id']} created by {interaction.user}.")
        await interaction.followup.send(confirmation, ephemeral=True)


class ModeratorBot:
    """The single moderation implementation shared by GUI and Railway."""
    def __init__(self, store, source, log=print, event=None):
        self.store, self.source, self.log = store, source, log
        self.event = event or (lambda *_: None)
        self.settings = store.get_settings()
        self.bot = None
        self.loop = None
        self.last_action, self.actions = {}, []

    def allowed(self, member):
        allowed = set(self.settings.get("allowed_roles", {}).get(str(member.guild.id), []))
        return bool(allowed & {str(role.id) for role in member.roles})

    def guild_info(self, guild):
        return {"id": guild.id, "name": guild.name, "member_count": guild.member_count,
                "text_channels": [c.name for c in guild.text_channels], "voice_channels": [c.name for c in guild.voice_channels],
                "roles": [{"id": str(r.id), "name": r.name} for r in guild.roles if not r.is_default()]}

    async def refresh_settings(self):
        while not self.bot.is_closed():
            await asyncio.sleep(10)
            try:
                fresh = self.store.get_settings()
                if fresh["settings_revision"] != self.settings["settings_revision"]:
                    token_changed = fresh.get("token") != self.settings.get("token")
                    self.settings = fresh
                    self.log("Settings synchronized from Firebase.")
                    if token_changed:
                        self.log("Discord token changed in Firebase; reconnecting with the new token.")
                        await self.bot.close()
                        return
            except Exception as exc:
                self.log(f"Firebase settings refresh failed: {exc}")

    def build(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        bot = commands.Bot(command_prefix="!", intents=intents)
        self.bot = bot

        @bot.event
        async def on_ready():
            self.loop = asyncio.get_running_loop()
            self.log(f"Logged in as {bot.user} ({self.source})")
            self.event("invite", discord.utils.oauth_url(bot.user.id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands")))
            self.event("guilds", [(g.id, g.name) for g in bot.guilds])
            for guild in bot.guilds:
                self.event("guild", self.guild_info(guild))
            asyncio.create_task(self.refresh_settings())
            if not getattr(self, "commands_synced", False):
                try:
                    await bot.tree.sync(); self.commands_synced = True; self.log("Slash commands synchronized.")
                except Exception as exc: self.log(f"Slash command sync failed: {exc}")

        @bot.tree.command(name="token", description="Create an encrypted support token")
        async def token_command(interaction: discord.Interaction):
            await interaction.response.send_modal(TicketModal(self))

        @bot.event
        async def on_guild_join(guild):
            self.log(f"Joined {guild.name}")
            self.event("guilds", [(g.id, g.name) for g in bot.guilds])
            self.event("guild", self.guild_info(guild))

        @bot.event
        async def on_message(message):
            if message.author.bot or not message.guild or not LINK_PATTERN.search(message.content):
                return await bot.process_commands(message)
            member, now = message.author, time.time()
            self.actions = [stamp for stamp in self.actions if now - stamp < 60]
            if self.allowed(member) or now - self.last_action.get(member.id, 0) < 4 or len(self.actions) >= 20:
                return await bot.process_commands(message)
            self.last_action[member.id] = now; self.actions.append(now)
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound) as exc:
                return self.log(f"Cannot delete message: {exc}")
            try:
                count = self.store.increment_warning(member.id)
            except Exception as exc:
                return self.log(f"Warning was not saved to Firebase: {exc}")
            try:
                await member.send(f"Links are not allowed in **{member.guild.name}**.")
            except discord.Forbidden:
                pass
            limit, minutes = int(self.settings["max_warnings_before_timeout"]), int(self.settings["timeout_minutes"])
            self.log(f"Deleted link from {member} ({count}/{limit}).")
            if count >= limit:
                try:
                    await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes), reason="Repeated link spam")
                    self.store.reset_warning(member.id)
                    self.log(f"Timed out {member} for {minutes} minutes.")
                except discord.Forbidden:
                    self.log("Missing permission to timeout member.")
            await bot.process_commands(message)
        return bot

    async def run(self, token):
        self.build()
        await self.bot.start(token)

    async def notify_ticket_resolved(self, ticket):
        user = self.bot.get_user(int(ticket["user_id"])) or await self.bot.fetch_user(int(ticket["user_id"]))
        await user.send(f"Your support token **#{ticket['ticket_id']}** is resolved. Note: {ticket.get('resolution_note') or 'No additional note.'}")

    async def moderate_member(self, action, guild_id, user_id, reason):
        if not self.store.claim_rate_limit(f"gui_{action}", user_id, 10):
            raise RuntimeError("This member was moderated recently. Wait 10 seconds.")
        guild = self.bot.get_guild(int(guild_id))
        if not guild: raise RuntimeError("Guild is not available to this GUI bot.")
        member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        if action == "kick": await member.kick(reason=reason or "GUI moderation action")
        elif action == "ban": await member.ban(reason=reason or "GUI moderation action", delete_message_seconds=0)
        else: raise RuntimeError("Unsupported moderation action.")
        self.log(f"{action.title()}ed {member} from {guild.name}.")
